"""Write one persistent custom image, arranged to reveal pixel orientation.

  index 0            -> WHITE  (the origin corner)
  indices 1..5       -> RED    (direction of index + 1)
  indices 6,12,18,24,30 -> BLUE (direction of index + 6)

Requires the screen to be in "Custom" mode, with ATK HUB closed.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from yogo import PIXELS, YogoDisplay

OFF, W, R, B = (0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 0, 255)

px = [OFF] * PIXELS
for i in range(1, 6):
    px[i] = R
for i in range(1, 6):
    px[i * 6] = B
px[0] = W

dev = YogoDisplay.open_first()
print(f"{dev!r}")
with dev:
    dev.write_image(px)
    print("wrote custom image: white origin, red = index+1 axis, blue = index+6 axis")
