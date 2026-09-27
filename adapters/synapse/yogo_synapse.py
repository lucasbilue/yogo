"""Show Synapse work-item status on the Yogo display. Read-only sidecar.

Polls Synapse's local dashboard API (GET /api/work) and publishes the result
on the yogo signal bus. It changes nothing in Synapse: no code, no config, no
writes. If this process stops, its signals are dropped (they carry its pid)
and Synapse carries on exactly as before.

Two bus sources are used, so a completion can flash over ongoing work:

  synapse         the ongoing state: waiting (needs you) > thinking > idle
  synapse-events  one-off "done" / "error" when a work item finishes; the
                  daemon shows it for ~5 s, then the ongoing state resumes

Work items that were already finished when the sidecar started are ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root
from yogo import bus  # noqa: E402  (pure stdlib, no hidapi needed)

DEFAULT_URL = "http://127.0.0.1:3000/api/work"

# Synapse work status -> what it means for the display. Anything not listed
# (queued, cancelled, a status added later) is ignored. Edit to taste.
ONGOING = {
    "assigned": "thinking",
    "starting": "thinking",
    "working": "thinking",
    "waiting": "waiting",
    "blocked": "waiting",
}
FINISHED = {"done": "done", "failed": "error"}

SOURCE, EVENTS = "synapse", "synapse-events"


def work_items(payload) -> dict[str, str]:
    """{work id: status} from whatever shape /api/work returns.

    Walks the JSON for objects with a WORK-... id and a string status, so it
    copes with a bare list, {items: [...]}, {work: [...]}, pagination wrappers
    and so on without knowing which.
    """
    found: dict[str, str] = {}
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            wid, status = node.get("id"), node.get("status")
            if isinstance(wid, str) and wid.startswith("WORK-") and isinstance(status, str):
                found[wid] = status
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def ongoing_state(items: dict[str, str], blocked: bool = True) -> str:
    states = {ONGOING.get(s) for s in items.values()
              if blocked or s != "blocked"}
    if "waiting" in states:
        return "waiting"
    if "thinking" in states:
        return "thinking"
    return "idle"


def finished_since(before: dict[str, str], now: dict[str, str]) -> str | None:
    """'error' if anything newly failed, else 'done' if anything newly finished."""
    newly = {FINISHED[s] for wid, s in now.items()
             if s in FINISHED and before.get(wid) != s}
    if "error" in newly:
        return "error"
    return "done" if newly else None


def fetch(url: str, timeout: float = 3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run(url: str, interval: float, blocked: bool, verbose: bool) -> int:
    pid = os.getpid()
    last_items: dict[str, str] | None = None     # None until the first good poll
    last_state, last_emit, warned = None, 0.0, None
    try:
        while True:
            try:
                items = work_items(fetch(url))
            except urllib.error.HTTPError as e:
                msg = f"Synapse answered HTTP {e.code} for {url}"
                if e.code in (401, 403):
                    msg += " (the API wants a login; is identity mode 'local'?)"
                items = None
            except (urllib.error.URLError, OSError, ValueError) as e:
                msg = f"can't reach Synapse at {url}: {getattr(e, 'reason', e)}"
                items = None

            if items is None:
                if msg != warned:
                    print(f"[yogo-synapse] {msg}; retrying", file=sys.stderr, flush=True)
                    warned = msg
                if last_state not in (None, "idle"):
                    bus.clear(SOURCE)                # don't pin a stale state
                    last_state = None
                time.sleep(max(interval, 3.0))
                continue
            warned = None

            if last_items is not None:
                event = finished_since(last_items, items)
                if event:
                    bus.emit(EVENTS, event, ttl=60, pid=pid, label="synapse")
                    if verbose:
                        print(f"[yogo-synapse] {event}", flush=True)
            last_items = items

            state = ongoing_state(items, blocked)
            now = time.monotonic()
            # Write only on change (plus a slow refresh inside the TTL): each
            # write restarts the animation, so rewriting every poll would stutter.
            if state != last_state or now - last_emit > 300:
                bus.emit(SOURCE, state, ttl=900, pid=pid, label="synapse")
                if verbose and state != last_state:
                    counts = {}
                    for s in items.values():
                        counts[s] = counts.get(s, 0) + 1
                    print(f"[yogo-synapse] {state}  {counts}", flush=True)
                last_state, last_emit = state, now
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        bus.clear(SOURCE)
        bus.clear(EVENTS)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("SYNAPSE_WORK_URL", DEFAULT_URL))
    ap.add_argument("--interval", type=float, default=1.0, help="poll seconds (default 1)")
    ap.add_argument("--no-blocked", dest="blocked", action="store_false",
                    help="don't treat 'blocked' as needing you")
    ap.add_argument("--dump", action="store_true",
                    help="print what the sidecar sees once, touch nothing, and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.dump:
        try:
            payload = fetch(args.url)
        except Exception as e:
            print(f"can't read {args.url}: {e}", file=sys.stderr)
            return 1
        items = work_items(payload)
        print(f"{len(items)} work item(s) at {args.url}")
        for wid, s in sorted(items.items()):
            shown = ONGOING.get(s) or FINISHED.get(s) or "(ignored)"
            print(f"  {wid}  {s:<10} -> {shown}")
        print(f"display would show: {ongoing_state(items, args.blocked)}")
        if not items:
            print("\nno WORK- items found; first 600 chars of the response:\n"
                  + json.dumps(payload)[:600])
        return 0
    return run(args.url, args.interval, args.blocked, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
