"""Command line interface for the Yogo 75 PRO dot screen."""

from __future__ import annotations

import argparse
import colorsys
import os
import pathlib
import random
import subprocess
import sys
import time

from . import bus
from . import daemon as dmn
from . import protocol as p
from .device import ProtocolError, YogoDisplay, YogoNotFound
from .font import lit_indices
from .frame import Frame, parse_color


def _open(args, need_custom_mode: bool = False) -> YogoDisplay:
    """Open the keyboard.

    Custom mode is only forced when we are about to write a *persistent*
    image, because that write is what has to survive on its own. Animations
    refresh continuously, so they need no config write and touch no flash.
    """
    pid = dmn.daemon_pid()
    if pid is not None:
        raise SystemExit(
            f"the yogo daemon (pid {pid}) is holding the display.\n"
            f"  stop it:      ./bin/yogo daemon --stop\n"
            f"  or drive it:  ./bin/yogo state done")
    dev = YogoDisplay.open_first()
    if need_custom_mode and not getattr(args, "no_mode_set", False):
        dev.set_screen_mode(p.SCREEN_MODE_CUSTOM)
    return dev


def _hsv(h: float, s: float = 1.0, v: float = 1.0) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _present_static(frame: Frame, args) -> None:
    """Show a still image.

    A streamed frame is volatile - the firmware takes the screen back after
    about a second - so a still image defaults to the persistent path.
    """
    volatile = getattr(args, "stream", False)
    with _open(args, need_custom_mode=not volatile) as dev:
        if args.brightness < 1.0:
            frame = frame.scaled(args.brightness)
        if volatile:
            dev.show(frame.pixels)
            dev._streaming = False
        else:
            dev.write_image(frame.pixels)
    if not args.quiet:
        print(frame.render_ansi())
        print(f"  [{'streamed (volatile)' if volatile else 'written to flash'}]")


# -- subcommands ----------------------------------------------------------
def cmd_info(args) -> int:
    devices = YogoDisplay.discover()
    if not devices:
        print("no ATK Yogo found", file=sys.stderr)
        return 1
    for d in devices:
        print(d)
        with d.open() as dev:
            mode = dev.read_config()[p.CONFIG_SCREEN_MODE]
            print(f"  screen mode : 0x{mode:02X}  (live status: 0x06 while the "
                  f"host drives it, 0x00 at rest)")
            print(f"  geometry    : {p.WIDTH}x{p.HEIGHT} = {p.PIXELS} RGB pixels, "
                  f"row-major from top-left")
            print(f"  report      : {dev.buffer_len}B, {dev.max_data}B payload")
            info = dev.power_info()
            if info:
                print(f"  battery     : {info['percent']}%"
                      f"{' charging' if info['charging'] else ''}")
    return 0


def cmd_solid(args) -> int:
    _present_static(Frame.solid(parse_color(args.color)), args)
    return 0


def cmd_clear(args) -> int:
    _present_static(Frame.solid((0, 0, 0)), args)
    return 0


def cmd_pixels(args) -> int:
    colors = [parse_color(c) for c in args.colors]
    if len(colors) == 1:
        colors *= p.PIXELS
    if len(colors) != p.PIXELS:
        print(f"need 1 or {p.PIXELS} colours, got {len(colors)}", file=sys.stderr)
        return 2
    _present_static(Frame.from_pixels(colors), args)
    return 0


def cmd_image(args) -> int:
    try:
        frame = Frame.from_image(args.path)
    except ImportError:
        print("image support needs Pillow:  pip install Pillow", file=sys.stderr)
        return 3
    _present_static(frame, args)
    return 0


def cmd_mode(args) -> int:
    with _open(args) as dev:
        if args.value is None:
            print(f"0x{dev.read_config()[p.CONFIG_SCREEN_MODE]:02X}")
        else:
            v = p.SCREEN_MODE_CUSTOM if args.value == "custom" else int(args.value, 0)
            print("changed" if dev.set_screen_mode(v) else "already set")
    return 0


def cmd_rainbow(args) -> int:
    with _open(args) as dev:
        t0 = time.time()
        try:
            while args.duration <= 0 or time.time() - t0 < args.duration:
                phase = ((time.time() - t0) / max(args.period, 0.1)) % 1.0
                dev.show(Frame.rainbow(phase).scaled(args.brightness).pixels)
                time.sleep(1 / args.fps)
        except KeyboardInterrupt:
            pass
        dev._streaming = False
    return 0


def cmd_cpu(args) -> int:
    """Scrolling CPU-load history: one column per sample, newest on the right."""
    import psutil

    history = [0.0] * p.WIDTH
    with _open(args) as dev:
        try:
            psutil.cpu_percent()          # prime the counter
            t0 = time.time()
            while args.duration <= 0 or time.time() - t0 < args.duration:
                load = psutil.cpu_percent(interval=args.interval) / 100.0
                history = history[1:] + [load]
                f = Frame()
                for x, v in enumerate(history):
                    col = (0, 255, 60) if v < 0.5 else (255, 170, 0) if v < 0.8 else (255, 30, 0)
                    f.column(x, round(v * p.HEIGHT), col)
                dev.show(f.scaled(args.brightness).pixels)
        except KeyboardInterrupt:
            pass
        dev._streaming = False
    return 0


def cmd_text(args) -> int:
    """Spell a string out one character at a time.

    Each character is drawn on stroke by stroke with a bright leading pixel,
    then held while a rainbow flows diagonally through it.
    """
    fps = max(args.fps, 1.0)
    dt = 1.0 / fps
    bright = args.brightness

    with _open(args) as dev:
        try:
            for ch in args.text:
                lit = lit_indices(ch)
                base = random.random()
                draw_for = args.hold * args.draw_frac
                hold_for = args.hold - draw_for

                # --- draw the character on ---
                t0 = time.time()
                while lit and (el := time.time() - t0) < draw_for:
                    n = max(1, round(len(lit) * el / draw_for))
                    f = Frame()
                    for k, idx in enumerate(lit[:n]):
                        head = k == n - 1
                        # the leading pixel flares white, the rest settle to hue
                        f.pixels[idx] = _hsv(base + k / max(len(lit), 1) * 0.5,
                                             0.0 if head else 0.95,
                                             1.0 if head else 0.85)
                    dev.show(f.scaled(bright).pixels)
                    time.sleep(dt)

                # --- hold, with a rainbow flowing through the strokes ---
                t0 = time.time()
                while (el := time.time() - t0) < hold_for:
                    f = Frame()
                    for idx in lit:
                        x, y = idx % p.WIDTH, idx // p.WIDTH
                        f.pixels[idx] = _hsv(base + (x + y) / 14.0 + el * args.flow)
                    dev.show(f.scaled(bright).pixels)
                    time.sleep(dt)

                # brief gap so repeated letters read as two
                if args.gap > 0:
                    blank = Frame().pixels
                    t0 = time.time()
                    while time.time() - t0 < args.gap:
                        dev.show(blank)
                        time.sleep(dt)

            # --- finale: a hue sweep radiating from the centre ---
            if args.finale:
                t0 = time.time()
                while (el := time.time() - t0) < 2.0:
                    f = Frame()
                    for idx in range(p.PIXELS):
                        x, y = idx % p.WIDTH, idx // p.WIDTH
                        d = ((x - 2.5) ** 2 + (y - 2.5) ** 2) ** 0.5
                        f.pixels[idx] = _hsv(el * 0.9 - d / 7.0, 1.0,
                                             max(0.0, min(1.0, 1.2 - abs(d - el * 3.0) / 2.0)))
                    dev.show(f.scaled(bright).pixels)
                    time.sleep(dt)
                for _ in range(6):        # fade out
                    dev.show(Frame().pixels)
                    time.sleep(dt)
        except KeyboardInterrupt:
            pass
        dev._streaming = False
    return 0



def cmd_demo(args) -> int:
    """Cycle through every notification state so you can see each animation.

    Two routes to the glass, chosen automatically:

    - daemon running: publish each state to the bus as source ``demo`` and let
      the daemon render it, exactly as a harness would. Another producer in a
      higher-priority state (say Claude Code asking for permission) will win
      over the demo, and that is reported rather than fought.
    - no daemon: open the device and render with the daemon's own renderers,
      so the picture is identical either way.
    """
    states = args.states or list(bus.STATES)
    hold = max(args.hold, 0.0)
    via_bus = dmn.daemon_pid() is not None

    def announce(state: str, note: str = "") -> None:
        if not args.quiet:
            print(f"{state:9}{note}", flush=True)

    if via_bus:
        try:
            while True:
                for st in states:
                    bus.emit("demo", st, ttl=max(hold, 1.0) + 2.0)
                    shown, _, win = bus.arbitrate()
                    note = ""
                    if st == "idle" and shown != "idle":
                        note = f"   (bus not idle: display shows {shown} from {win.source})"
                    elif st != "idle" and (win is None or win.source != "demo"):
                        note = f"   (pre-empted: display shows {shown} from {win.source if win else '?'})"
                    announce(st, note)
                    time.sleep(hold)
                if not args.loop:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            bus.clear("demo")
        return 0

    dt = 1.0 / max(args.fps, 1.0)
    with _open(args) as dev:
        try:
            while True:
                for st in states:
                    announce(st)
                    renderer = dmn.RENDERERS.get(st)
                    t0 = time.time()
                    while (el := time.time() - t0) < hold:
                        frame = renderer(el) if renderer else dmn.render_idle(el, args.idle)
                        dev.show(frame.scaled(args.brightness).pixels)
                        time.sleep(dt)
                if not args.loop:
                    break
        except KeyboardInterrupt:
            pass
        dev.show(Frame().pixels)
        dev._streaming = False
    return 0


def cmd_daemon(args) -> int:
    """Own the display and animate whatever state the state file names."""
    if args.stop:
        print("stopped" if dmn.stop_daemon() else "no daemon running")
        return 0
    if args.status:
        pid = dmn.daemon_pid()
        state, elapsed, win = bus.arbitrate()
        print(f"daemon    : running (pid {pid})" if pid else "daemon    : not running")
        print(f"showing   : {state}" + (f"  (from {win.source}, {elapsed:.1f}s ago)" if win else ""))
        sigs = bus.read_all()
        print(f"sources   : {len(sigs)}" if sigs else "sources   : none")
        for sig in sorted(sigs, key=lambda x: -bus.PRIORITY.get(x.state, 0)):
            live = "live " if not sig.expired() else "stale"
            mark = "*" if win and sig.source == win.source else " "
            print(f"  {mark} {sig.source:16} {sig.state:9} {live}  {sig.age():6.1f}s ago")
        return 0
    if args.foreground:
        return dmn.run(fps=args.fps, idle_style=args.idle,
                       brightness=args.brightness, verbose=not args.quiet)
    # Detach by exec'ing a fresh process, NOT by forking: hidapi talks to
    # IOKit/CoreFoundation, which macOS forbids using after fork() without an
    # exec(). A forked child dies with "The process has forked and you cannot
    # use this CoreFoundation functionality safely" - and each death raises a
    # crash report dialog.
    if dmn.daemon_pid() is not None:
        print("already running")
        return 0
    dmn.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log = open(dmn.RUNTIME_DIR / "daemon.log", "ab")
    subprocess.Popen(
        [sys.executable, "-m", "yogo.cli", "daemon", "--foreground",
         "--fps", str(args.fps), "--idle", args.idle,
         "--brightness", str(args.brightness)],
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, env=os.environ.copy(),
    )
    for _ in range(40):
        pid = dmn.daemon_pid()
        if pid is not None:
            print(f"daemon started (pid {pid})")
            return 0
        time.sleep(0.1)
    print(f"daemon did not come up - see {dmn.RUNTIME_DIR / 'daemon.log'}", file=sys.stderr)
    return 1


def cmd_state(args) -> int:
    """Publish a state to the bus. Never opens the device, so it is cheap."""
    if args.name is None:
        state, elapsed, win = bus.arbitrate()
        print(f"{state}" + (f"  (from {win.source})" if win else ""))
        return 0
    try:
        bus.emit(args.source, args.name, ttl=args.ttl)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not args.quiet:
        state, _, win = bus.arbitrate()
        note = "" if dmn.daemon_pid() else "   (no daemon running - nothing will show)"
        extra = ""
        if win and win.source != bus.normalise_source(args.source):
            extra = f"   (display shows {state} from {win.source} - higher priority)"
        print(f"{args.source} -> {args.name}{extra}{note}")
    return 0


def cmd_sources(args) -> int:
    """List producers on the bus, or drop one."""
    if args.clear:
        bus.clear(args.clear)
        print(f"cleared {args.clear}")
        return 0
    if args.prune:
        print(f"pruned {bus.prune()} stale source(s)")
        return 0
    sigs = bus.read_all()
    if not sigs:
        print("no sources on the bus")
        return 0
    state, _, win = bus.arbitrate()
    for sig in sorted(sigs, key=lambda x: -bus.PRIORITY.get(x.state, 0)):
        mark = "*" if win and sig.source == win.source else " "
        live = "live " if not sig.expired() else "stale"
        print(f" {mark} {sig.source:16} {sig.state:9} {live}  {sig.age():7.1f}s ago  "
              f"ttl={sig.ttl:.0f}s  pid={sig.pid or '-'}")
    print(f"\n   * = winning; display shows: {state}")
    return 0


def _add_common(ap, suppress: bool) -> None:
    """Shared flags. On subparsers the defaults are suppressed, so a flag given
    before the subcommand isn't clobbered by the subparser's own default."""
    d = argparse.SUPPRESS
    ap.add_argument("--stream", action="store_true",
                    default=(d if suppress else False),
                    help="write a still image volatilely instead of to flash "
                         "(it will fade after about a second)")
    ap.add_argument("--brightness", type=float, metavar="F",
                    default=(d if suppress else 1.0),
                    help="scale all channels by F (0.0-1.0)")
    ap.add_argument("--no-mode-set", action="store_true",
                    default=(d if suppress else False),
                    help="never write the config, even for a persistent image")
    ap.add_argument("-q", "--quiet", action="store_true",
                    default=(d if suppress else False),
                    help="no terminal preview")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="yogo", description="Drive the 6x6 RGB display on an ATK Yogo 75 PRO keyboard.")
    _add_common(ap, suppress=False)
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="show device, screen mode and battery",
                   parents=[common]).set_defaults(fn=cmd_info)
    sub.add_parser("clear", help="turn every pixel off",
                   parents=[common]).set_defaults(fn=cmd_clear)

    s = sub.add_parser("solid", help="fill the screen with one colour", parents=[common])
    s.add_argument("color", help="name, #rrggbb, or r,g,b")
    s.set_defaults(fn=cmd_solid)

    s = sub.add_parser("pixels", help="set all 36 pixels, row-major from top-left",
                       parents=[common])
    s.add_argument("colors", nargs="+", metavar="COLOR")
    s.set_defaults(fn=cmd_pixels)

    s = sub.add_parser("image", help="downscale an image file to 6x6", parents=[common])
    s.add_argument("path")
    s.set_defaults(fn=cmd_image)

    s = sub.add_parser("mode", help="get or set the screen mode", parents=[common])
    s.add_argument("value", nargs="?", help="'custom' or a raw value like 0x0c")
    s.set_defaults(fn=cmd_mode)

    s = sub.add_parser("text", help="spell a string out, one character at a time",
                       parents=[common])
    s.add_argument("text")
    s.add_argument("--hold", type=float, default=1.0, help="seconds per character")
    s.add_argument("--draw-frac", type=float, default=0.45,
                   help="fraction of --hold spent drawing the character on")
    s.add_argument("--gap", type=float, default=0.09, help="blank seconds between characters")
    s.add_argument("--flow", type=float, default=0.55, help="rainbow flow speed")
    s.add_argument("--fps", type=float, default=20)
    s.add_argument("--no-finale", dest="finale", action="store_false",
                   help="skip the closing flourish")
    s.set_defaults(fn=cmd_text)

    s = sub.add_parser("demo", help="cycle through every notification state, idle to error",
                       parents=[common])
    s.add_argument("states", nargs="*", choices=list(bus.STATES), metavar="STATE",
                   help="which states, in order (default: all, idle first)")
    s.add_argument("--hold", type=float, default=2.0, help="seconds per state (default 2)")
    s.add_argument("--loop", action="store_true", help="repeat until Ctrl-C")
    s.add_argument("--idle", choices=["off", "breathe"], default="breathe",
                   help="idle look when rendering directly (a running daemon "
                        "uses its own --idle setting)")
    s.add_argument("--fps", type=float, default=15.0)
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("daemon", help="run the display daemon for Claude Code hooks",
                       parents=[common])
    s.add_argument("--stop", action="store_true", help="stop a running daemon")
    s.add_argument("--status", action="store_true", help="report daemon state")
    s.add_argument("--foreground", action="store_true", help="do not detach")
    s.add_argument("--fps", type=float, default=15.0)
    s.add_argument("--idle", choices=["off", "breathe", "firmware"], default="off",
                   help="what to show when nothing is happening: off, a dim blue "
                        "breathe, or 'firmware' to hand the screen back to the "
                        "keyboard's own built-in animation (default: off)")
    s.set_defaults(fn=cmd_daemon)

    s = sub.add_parser("state", help="publish a display state to the bus", parents=[common])
    s.add_argument("name", nargs="?", choices=list(bus.STATES),
                   help="omit to print what the display is showing")
    s.add_argument("--source", default="cli", help="producer name (default: cli)")
    s.add_argument("--ttl", type=float, default=None,
                   help="seconds before this signal is ignored (default 900)")
    s.set_defaults(fn=cmd_state)

    s = sub.add_parser("sources", help="inspect producers on the signal bus",
                       parents=[common])
    s.add_argument("--clear", metavar="SOURCE", help="remove one source's signal")
    s.add_argument("--prune", action="store_true", help="remove signals older than a day")
    s.set_defaults(fn=cmd_sources)

    s = sub.add_parser("rainbow", help="animated hue sweep (Ctrl-C to stop)", parents=[common])
    s.add_argument("--duration", type=float, default=0, help="seconds, 0 = forever")
    s.add_argument("--period", type=float, default=3.0, help="seconds per full cycle")
    s.add_argument("--fps", type=float, default=15)
    s.set_defaults(fn=cmd_rainbow)

    s = sub.add_parser("cpu", help="live scrolling CPU meter (Ctrl-C to stop)", parents=[common])
    s.add_argument("--interval", type=float, default=0.5, help="sample seconds")
    s.add_argument("--duration", type=float, default=0, help="seconds, 0 = forever")
    s.set_defaults(fn=cmd_cpu)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    for attr, default in (("stream", False), ("brightness", 1.0),
                          ("no_mode_set", False), ("quiet", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    try:
        return args.fn(args)
    except YogoNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ProtocolError as e:
        print(f"protocol error: {e}", file=sys.stderr)
        return 4
    except OSError as e:
        if "already open" in str(e):
            print("error: the keyboard is held by another app - quit ATK HUB and retry",
                  file=sys.stderr)
            return 5
        raise


if __name__ == "__main__":
    sys.exit(main())
