"""The render loop: owns the display and animates whatever the bus arbitrates.

Why a daemon rather than each harness writing pixels: the HID interface is
exclusive-access, so only one process can hold it, and a streamed frame is
volatile - something must keep refreshing at ~15 fps or the firmware takes the
screen back. Producers are short-lived (or are other applications entirely),
so they publish a state to the bus and this process renders it.

Animation phase comes from the winning signal's own age, so a bloom or flash
is timed from when the harness actually reported the event, not from when this
daemon happened to notice it.
"""

from __future__ import annotations

import colorsys
import errno
import math
import os
import signal
import sys
import time

from . import bus
from . import protocol as p
from .device import YogoDisplay
from .frame import Frame

RUNTIME_DIR = bus.RUNTIME_DIR
PID_FILE = bus.PID_FILE
STATES = bus.STATES

# A streamed frame is 72 bytes: two reports over USB, three or more over
# Bluetooth, each waiting on a reply that rides the BLE connection interval.
# 10 fps keeps comfortably inside the ~1 s before the firmware reclaims the
# screen while leaving the radio (and the battery) some slack.
BLUETOOTH_MAX_FPS = 10.0


def _hsv(h: float, s: float, v: float):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)),
                                  max(0.0, min(1.0, v)))
    return int(r * 255), int(g * 255), int(b * 255)


def _perimeter():
    """The 20 edge cells, clockwise from the top-left."""
    cells = [(x, 0) for x in range(6)]
    cells += [(5, y) for y in range(1, 6)]
    cells += [(x, 5) for x in range(4, -1, -1)]
    cells += [(0, y) for y in range(4, 0, -1)]
    return cells


RING = _perimeter()


# --- renderers: each takes seconds-since-this-state-began -----------------
def render_idle(t: float, style: str = "off") -> Frame:
    if style == "off":
        return Frame()
    v = 0.04 + 0.05 * (0.5 + 0.5 * math.sin(t * 1.1))     # slow breathe
    return Frame.solid(_hsv(0.58, 0.75, v))


def _comet(t: float, hue: float, speed: float, tail: int) -> Frame:
    f = Frame()
    n = len(RING)
    head = (t * speed) % n
    for i, (x, y) in enumerate(RING):
        d = (head - i) % n
        if d < tail:
            v = (1.0 - d / tail) ** 1.6
            f[x, y] = _hsv(hue, 0.85, v)
    for x in (2, 3):                                       # faint core
        for y in (2, 3):
            f[x, y] = _hsv(hue + 0.03, 0.9, 0.09)
    return f


def render_thinking(t: float) -> Frame:
    return _comet(t, hue=0.52, speed=9.0, tail=6)          # cyan, steady


def render_tool(t: float) -> Frame:
    return _comet(t, hue=0.74, speed=15.0, tail=5)         # violet, faster


def render_waiting(t: float) -> Frame:
    # Steady dim amber, with the brighter half swapping left/right at ~2 Hz.
    # Motion rather than a full-screen flash: full brightness was too harsh.
    left = (t * 2.2) % 1.0 < 0.5
    f = Frame.solid(_hsv(0.11, 1.0, 0.10))
    lit = _hsv(0.11, 1.0, 0.35)
    for x in range(0, 3) if left else range(3, p.WIDTH):
        for y in range(p.HEIGHT):
            f[x, y] = lit
    return f


def render_done(t: float) -> Frame:
    f = Frame()
    for idx in range(p.PIXELS):
        x, y = idx % p.WIDTH, idx // p.WIDTH
        d = math.hypot(x - 2.5, y - 2.5)
        if t < 0.8:                                        # bloom outward
            v = 1.0 - abs(d - (t / 0.8) * 4.6) * 0.8
        else:                                              # settle and fade
            k = min(1.0, (t - 0.8) / 4.0)
            v = (1.0 - k) * (0.85 - d * 0.05)
        f.pixels[idx] = _hsv(0.34, 0.9, v)
    return f


def render_error(t: float) -> Frame:
    if t < 0.9:
        v = 1.0 if (t * 6.0) % 1.0 < 0.5 else 0.05         # double flash
    else:
        v = (1.0 - min(1.0, (t - 0.9) / 4.0)) * 0.5
    return Frame.solid(_hsv(0.0, 0.95, v))


RENDERERS = {
    "thinking": render_thinking,
    "tool": render_tool,
    "waiting": render_waiting,
    "done": render_done,
    "error": render_error,
}


# --- process management ---------------------------------------------------
def daemon_pid() -> int | None:
    """PID of a live daemon, or None. Clears a stale pid file."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            PID_FILE.unlink(missing_ok=True)
            return None
        if e.errno == errno.EPERM:
            return pid               # alive, owned by someone else
        return None
    return pid


def stop_daemon() -> bool:
    pid = daemon_pid()
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if daemon_pid() is None:
            return True
        time.sleep(0.1)
    return True


def run(fps: float = 15.0, idle_style: str = "off", brightness: float = 1.0,
        verbose: bool = False) -> int:
    if daemon_pid() is not None:
        print("a yogo daemon is already running", file=sys.stderr)
        return 1

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    stopping = {"flag": False}

    def _stop(_sig, _frm):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    dev = None
    base_dt = dt = 1.0 / max(fps, 1.0)
    next_retry = 0.0
    last_desc = None
    idle_since = time.monotonic()

    try:
        while not stopping["flag"]:
            now = time.monotonic()
            state, elapsed, win = bus.arbitrate()

            # idle has no producer, so give it its own clock for the breathe
            if state == "idle":
                elapsed = now - idle_since
            else:
                idle_since = now

            if verbose:
                desc = f"{state} <- {win.source if win else 'none'}"
                if desc != last_desc:
                    print(f"[{time.strftime('%H:%M:%S')}] {desc}", flush=True)
                    last_desc = desc

            # (re)open the device if needed - it may be held by ATK HUB
            if dev is None:
                if now < next_retry:
                    time.sleep(min(dt, next_retry - now))
                    continue
                try:
                    dev = YogoDisplay.open_first()
                    dt = base_dt
                    if dev.link == p.LINK_BLUETOOTH:
                        dt = max(base_dt, 1.0 / BLUETOOTH_MAX_FPS)
                    if verbose:
                        print(f"opened {dev!r} at {1 / dt:.0f} fps", flush=True)
                except Exception as e:
                    # Broad on purpose: hid.HIDException is not an OSError, and
                    # "exclusive access and device already open" (ATK HUB, or
                    # another daemon) is the common case. Retry, never die.
                    next_retry = now + 3.0
                    if verbose:
                        print(f"waiting for device: {e}", flush=True)
                    continue

            renderer = RENDERERS.get(state)

            try:
                if renderer is None and idle_style == "firmware":
                    # Hand the screen back so the keyboard's own built-in
                    # animation plays between sessions. Streaming resumes on
                    # the first frame of the next state.
                    dev.end_stream()
                    time.sleep(dt)
                    continue
                frame = renderer(elapsed) if renderer else render_idle(elapsed, idle_style)
                if brightness < 1.0:
                    frame = frame.scaled(brightness)
                dev.show(frame.pixels)
            except Exception as e:                # device yanked or claimed
                if verbose:
                    print(f"lost device: {e}", flush=True)
                try:
                    dev.close()
                except Exception:
                    pass
                dev = None
                next_retry = now + 2.0
                continue

            # Sleep only what is left of the frame: a slow link (Bluetooth)
            # already spent part of it waiting on replies.
            time.sleep(max(0.0, dt - (time.monotonic() - now)))
    finally:
        if dev is not None:
            try:
                if idle_style == "firmware":
                    dev.end_stream()          # give the screen straight back
                else:
                    dev.show(Frame().pixels)  # leave it dark
                    dev._streaming = False    # don't hand back mid-frame
                dev.close()
            except Exception:
                pass
        PID_FILE.unlink(missing_ok=True)
    return 0
