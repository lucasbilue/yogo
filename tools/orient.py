"""Determine the pixel-index -> physical-position mapping of the dot screen.

Put the screen in "Custom" mode in ATK HUB first, otherwise a built-in effect
will keep redrawing over these frames.

Each phase lights a known set of indices; what you see on the keyboard tells
us the origin corner and whether indices run along rows or columns.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from yogo import PIXELS, YogoDisplay

OFF = (0, 0, 0)
W, R, G, B = (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255)

def frame(mapping):
    return [mapping.get(i, OFF) for i in range(PIXELS)]

ALL_W = frame({i: W for i in range(PIXELS)})
ALL_OFF = frame({})

def hold(dev, pixels, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        dev.show(pixels)          # refreshing also keeps the session alive
        time.sleep(0.05)

def main():
    dev = YogoDisplay.open_first()
    print(f"{dev!r}\n", flush=True)
    with dev:
        print(">>> WAKE-UP: flashing the whole screen white 3x", flush=True)
        print("    (if you see nothing here, the host isn't driving the screen)\n", flush=True)
        for _ in range(3):
            hold(dev, ALL_W, 0.35)
            hold(dev, ALL_OFF, 0.35)

        phases = [
            ("PHASE 1 - every pixel RED", frame({i: R for i in range(PIXELS)}), 3.0,
             "the whole 6x6 grid should be solid red"),
            ("PHASE 2 - index 0 only, WHITE", frame({0: W}), 6.0,
             "ONE white dot. Which corner? (compare to your red dot's corner)"),
            ("PHASE 3 - indices 0-5, GREEN at index 0", 
             frame({**{i: W for i in range(6)}, 0: G}), 6.0,
             "a line of 6. Is it HORIZONTAL or VERTICAL? Where's the green end?"),
            ("PHASE 4 - indices 0,6,12,18,24,30, BLUE at index 0",
             frame({**{i * 6: W for i in range(6)}, 0: B}), 6.0,
             "the other axis. HORIZONTAL or VERTICAL? Where's the blue end?"),
        ]
        for name, px, secs, what in phases:
            print(f">>> {name}   [{secs:.0f}s]", flush=True)
            print(f"    look for: {what}\n", flush=True)
            hold(dev, px, secs)

        print(">>> done - releasing the screen back to the firmware", flush=True)

main()
