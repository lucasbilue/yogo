"""Force Custom mode, then write the orientation test image."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from yogo import PIXELS, YogoDisplay
from yogo import protocol as p

OFF, W, R, B = (0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 0, 255)
px = [OFF] * PIXELS
px[0], px[1], px[6] = W, R, B

dev = YogoDisplay.open_first()
print(f"{dev!r}")
with dev:
    cfg = dev.read_config()
    print(f"screen mode before: 0x{cfg[p.CONFIG_SCREEN_MODE]:02X}")
    changed = dev.set_screen_mode(p.SCREEN_MODE_CUSTOM)
    print(f"set Custom (0x06): {'changed' if changed else 'already set'}")
    dev.write_image(px)
    print(f"screen mode after:  0x{dev.read_config()[p.CONFIG_SCREEN_MODE]:02X}")
    print("wrote white@0, red@1, blue@6")
