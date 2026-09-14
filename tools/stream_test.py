"""Does the volatile streaming path (0x3C) work now the framing is correct?"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from yogo import PIXELS, YogoDisplay
from yogo import protocol as p

COLORS = [((255,0,0),"RED"), ((0,255,0),"GREEN"), ((0,0,255),"BLUE"),
          ((255,255,0),"YELLOW"), ((255,255,255),"WHITE")]

dev = YogoDisplay.open_first()
print(f"{dev!r}")
try:
    dev.set_screen_mode(p.SCREEN_MODE_CUSTOM)
    for n in (3, 2, 1):
        print(f"  starting in {n}...", flush=True); time.sleep(1)
    print("\nWATCH THE SCREEN - whole display should flash solid colours:\n", flush=True)
    frames = 0
    for cycle in range(2):
        for rgb, name in COLORS:
            print(f"    {name}", flush=True)
            end = time.time() + 0.9
            while time.time() < end:
                dev.show([rgb] * PIXELS)
                frames += 1
                time.sleep(0.06)
    print(f"\nsent {frames} frames, all ACKed")
    dev._streaming = False      # leave the screen alone, keep Custom mode
finally:
    dev.close()
