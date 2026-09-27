"""Show claude.ai chat activity on the Yogo display. Local listener.

A small Chrome extension (./extension) watches claude.ai tabs and reports
when Claude starts and stops replying. This process receives those reports
on 127.0.0.1 and publishes them on the yogo signal bus, one source per tab:

  claude-web-<tab>   thinking while a reply streams, done when it finishes

POST /signal   {"tab": <int>, "state": "thinking" | "done" | "idle" | "clear"}
GET  /health   {"ok": true}

Any web page can send requests to localhost, so /signal only accepts ones
that carry the header X-Yogo: 1 and have no Origin, or a chrome-extension://
one. A page could only add that header after a CORS preflight, and this
server never answers preflights, so a page can't forge a signal.

Every signal carries this process's pid, so stopping the listener drops them
at once; it also removes its files on Ctrl-C or SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root
from yogo import bus  # noqa: E402  (pure stdlib, no hidapi needed)

DEFAULT_PORT = 7437
PREFIX = "claude-web-"
STATES = ("thinking", "done", "idle", "clear")

# thinking gets a TTL longer than the extension's 60 s heartbeat, so a long
# reply stays lit but a tab that stops reporting goes dark on its own.
TTL = {"thinking": 120}
DEFAULT_TTL = 60

MAX_BODY = 4096


class Listener(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, verbose: bool = False):
        super().__init__(addr, Handler)
        self.verbose = verbose
        self.pid = os.getpid()
        self.sources: set[str] = set()
        self.lock = threading.Lock()

    def apply(self, tab: int, state: str) -> None:
        source = f"{PREFIX}{tab}"
        with self.lock:
            if state == "clear":
                bus.clear(source)
                self.sources.discard(source)
            else:
                bus.emit(source, state, ttl=TTL.get(state, DEFAULT_TTL),
                         pid=self.pid, label="claude.ai")
                self.sources.add(source)
        if self.verbose:
            print(f"[yogo-web] tab {tab}: {state}", flush=True)

    def clear_all(self) -> None:
        with self.lock:
            for source in self.sources:
                bus.clear(source)
            self.sources.clear()


class Handler(BaseHTTPRequestHandler):
    server: Listener

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/signal":
            return self._reply(404, {"error": "not found"})
        if not self._trusted():
            return self._reply(403, {"error": "forbidden"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._reply(400, {"error": "bad body"})
        try:
            msg = json.loads(self.rfile.read(length))
        except ValueError:
            return self._reply(400, {"error": "malformed JSON"})

        tab = msg.get("tab") if isinstance(msg, dict) else None
        state = msg.get("state") if isinstance(msg, dict) else None
        # bool is an int subclass; True is not a tab id
        if not isinstance(tab, int) or isinstance(tab, bool) or tab < 0:
            return self._reply(400, {"error": "tab must be a non-negative integer"})
        if state not in STATES:
            return self._reply(400, {"error": f"state must be one of {', '.join(STATES)}"})

        try:
            self.server.apply(tab, state)
        except OSError as e:
            return self._reply(500, {"error": str(e)})
        self._reply(200, {"ok": True})

    def _trusted(self) -> bool:
        if self.headers.get("X-Yogo") != "1":
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.startswith("chrome-extension://")

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        srv = Listener(("127.0.0.1", args.port), verbose=args.verbose)
    except OSError as e:
        print(f"[yogo-web] can't listen on 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, _interrupt)
    print(f"[yogo-web] listening on http://127.0.0.1:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.clear_all()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
