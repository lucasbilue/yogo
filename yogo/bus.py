"""The signal bus: how any harness tells the display what is happening.

Design notes
------------
One display, potentially several producers (Claude Code, Hermes, a CI script).
That forces three decisions:

1. **Per-source files, not one shared file.** Each producer owns
   ``~/.yogo/sources/<source>.json`` and never writes anyone else's, so two
   harnesses can signal concurrently with no locking and no lost writes.

2. **Arbitration by state priority, not last-writer-wins.** If Hermes needs
   input while Claude Code is still thinking, the amber flash must win - the
   attention-demanding state is the useful one to show. Ties break on recency.

3. **Liveness via TTL (and optionally pid).** A harness that dies mid-turn
   must not pin the display in "thinking" forever, and no producer can be
   trusted to always send a closing signal.

The wire format is a small JSON object per source, deliberately writable by a
three-line shell script, so a harness needs no library to participate.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

RUNTIME_DIR = Path(os.environ.get("YOGO_HOME", os.path.expanduser("~/.yogo")))
SOURCES_DIR = RUNTIME_DIR / "sources"
PID_FILE = RUNTIME_DIR / "daemon.pid"
LOG_FILE = RUNTIME_DIR / "daemon.log"

STATES = ("idle", "thinking", "tool", "waiting", "done", "error")

# Higher wins when several sources are active at once. Attention-demanding
# states outrank ambient ones; `done` sits above `thinking` so a completion
# still gets its moment even while another harness keeps working.
PRIORITY = {"idle": 0, "thinking": 1, "tool": 2, "done": 3, "waiting": 4, "error": 5}

# "Something just happened" states: they stop competing once this elapses,
# which is what lets a lower-priority ongoing state resume underneath them.
TRANSIENT_HOLD = {"done": 5.0, "error": 5.0}

# Fallback expiry when a producer does not declare one. Generous, because a
# legitimate agent turn can run for many minutes.
DEFAULT_TTL = 900.0

_SOURCE_RE = re.compile(r"[^a-z0-9_-]+")


def normalise_source(name: str) -> str:
    s = _SOURCE_RE.sub("-", (name or "unknown").strip().lower()).strip("-")
    return s or "unknown"


@dataclass(frozen=True)
class Signal:
    source: str
    state: str
    ts: float                    # epoch seconds, when the producer emitted it
    ttl: float = DEFAULT_TTL
    pid: int | None = None
    label: str = ""

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.ts)

    def expired(self, now: float | None = None) -> bool:
        if self.ttl and self.age(now) > self.ttl:
            return True
        if self.pid is not None and not _pid_alive(self.pid):
            return True
        # a transient signal stops competing once its hold elapses
        hold = TRANSIENT_HOLD.get(self.state)
        return bool(hold and self.age(now) > hold)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --- producing -----------------------------------------------------------
def emit(source: str, state: str, ttl: float | None = None,
         pid: int | None = None, label: str = "") -> Signal:
    """Publish this source's current state. Atomic, so a reader never tears."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}; expected one of {', '.join(STATES)}")
    src = normalise_source(source)
    sig = Signal(src, state, time.time(),
                 DEFAULT_TTL if ttl is None else float(ttl), pid, label or src)
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    dest = SOURCES_DIR / f"{src}.json"
    tmp = SOURCES_DIR / f".{src}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({
        "state": sig.state, "ts": sig.ts, "ttl": sig.ttl,
        "pid": sig.pid, "label": sig.label,
    }))
    tmp.replace(dest)
    return sig


def clear(source: str) -> None:
    (SOURCES_DIR / f"{normalise_source(source)}.json").unlink(missing_ok=True)


# --- consuming -----------------------------------------------------------
def read_all() -> list[Signal]:
    out: list[Signal] = []
    try:
        entries = sorted(SOURCES_DIR.glob("*.json"))
    except OSError:
        return out
    for f in entries:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue                      # mid-write or corrupt; skip this tick
        state = d.get("state")
        if state not in STATES:
            continue
        pid = d.get("pid")
        # A producer may omit ts: POSIX sh cannot get sub-second epoch time on
        # macOS, and animation phase needs better than 1s granularity. File
        # mtime is nanosecond-precise on APFS and costs nothing.
        ts = float(d.get("ts") or 0.0)
        if not ts:
            try:
                ts = f.stat().st_mtime
            except OSError:
                ts = time.time()
        out.append(Signal(
            source=f.stem, state=state, ts=ts,
            ttl=float(d.get("ttl") or DEFAULT_TTL),
            pid=int(pid) if isinstance(pid, int) else None,
            label=str(d.get("label") or f.stem),
        ))
    return out


def arbitrate(signals: list[Signal] | None = None,
              now: float | None = None) -> tuple[str, float, Signal | None]:
    """Decide what the display should show.

    Returns (state, elapsed_since_that_signal, winning_signal). Falls back to
    idle when nothing is live, and the elapsed value is what drives animation
    phase - taken from the winning signal, so a bloom or flash is timed from
    when the harness actually reported it.
    """
    now = time.time() if now is None else now
    live = [s for s in (signals if signals is not None else read_all())
            if not s.expired(now) and s.state != "idle"]
    if not live:
        return "idle", 0.0, None
    live.sort(key=lambda s: (PRIORITY.get(s.state, 0), s.ts))
    win = live[-1]
    return win.state, win.age(now), win


def prune(max_age: float = 86400.0) -> int:
    """Drop source files nothing has touched in a day. Housekeeping only."""
    now, removed = time.time(), 0
    for s in read_all():
        if now - s.ts > max_age:
            clear(s.source)
            removed += 1
    return removed
