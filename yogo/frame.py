"""A 6x6 RGB frame buffer with convenience constructors."""

from __future__ import annotations

import colorsys

from .protocol import HEIGHT, PIXELS, WIDTH

Color = tuple[int, int, int]
BLACK: Color = (0, 0, 0)

# A few names so the CLI can take words as well as hex.
NAMED: dict[str, Color] = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "orange": (255, 110, 0),
    "purple": (150, 0, 255), "pink": (255, 60, 150), "off": (0, 0, 0),
}


def parse_color(text: str) -> Color:
    """Accept a name, '#rrggbb', 'rrggbb', 'rgb', or 'r,g,b'."""
    s = text.strip().lower()
    if s in NAMED:
        return NAMED[s]
    if "," in s:
        parts = [int(x) for x in s.split(",")]
        if len(parts) != 3:
            raise ValueError(f"expected 3 components, got {len(parts)}: {text!r}")
        return tuple(max(0, min(255, v)) for v in parts)  # type: ignore[return-value]
    h = s.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"cannot parse colour {text!r}")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


class Frame:
    """36 pixels, row-major from the top-left - verified against hardware."""

    __slots__ = ("pixels",)

    def __init__(self, fill: Color = BLACK):
        self.pixels: list[Color] = [fill] * PIXELS

    # -- construction -----------------------------------------------------
    @classmethod
    def solid(cls, color: Color) -> Frame:
        return cls(color)

    @classmethod
    def from_pixels(cls, pixels) -> Frame:
        f = cls()
        pixels = list(pixels)
        if len(pixels) != PIXELS:
            raise ValueError(f"expected {PIXELS} pixels, got {len(pixels)}")
        f.pixels = pixels
        return f

    @classmethod
    def from_image(cls, path: str) -> Frame:
        """Downscale any image file to 6x6, compositing alpha onto black."""
        from PIL import Image  # optional dependency

        img = Image.open(path)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
            img = Image.alpha_composite(bg, img)
        img = img.convert("RGB").resize((WIDTH, HEIGHT), Image.LANCZOS)
        return cls.from_pixels(list(img.getdata()))

    @classmethod
    def rainbow(cls, phase: float = 0.0) -> Frame:
        f = cls()
        for i in range(PIXELS):
            r, g, b = colorsys.hsv_to_rgb((phase + i / PIXELS) % 1.0, 1.0, 1.0)
            f.pixels[i] = (int(r * 255), int(g * 255), int(b * 255))
        return f

    # -- access -----------------------------------------------------------
    def __setitem__(self, xy: tuple[int, int], color: Color) -> None:
        x, y = xy
        self.pixels[y * WIDTH + x] = color

    def __getitem__(self, xy: tuple[int, int]) -> Color:
        x, y = xy
        return self.pixels[y * WIDTH + x]

    def fill(self, color: Color) -> Frame:
        self.pixels = [color] * PIXELS
        return self

    def column(self, x: int, height: int, on: Color, off: Color = BLACK) -> Frame:
        """Light the bottom `height` cells of column x - handy for meters."""
        for y in range(HEIGHT):
            self[x, y] = on if (HEIGHT - y) <= height else off
        return self

    def scaled(self, factor: float) -> Frame:
        """Uniform brightness scale, for a global dimmer."""
        factor = max(0.0, min(1.0, factor))
        return Frame.from_pixels(
            [(int(r * factor), int(g * factor), int(b * factor))
             for r, g, b in self.pixels])

    def __repr__(self) -> str:
        rows = []
        for y in range(HEIGHT):
            rows.append(" ".join(
                "".join(f"{c:02x}" for c in self[x, y]) for x in range(WIDTH)))
        return "Frame(\n  " + "\n  ".join(rows) + "\n)"

    def render_ansi(self) -> str:
        """Preview the frame in a truecolour terminal."""
        out = []
        for y in range(HEIGHT):
            row = "".join(
                f"\033[48;2;{r};{g};{b}m  \033[0m" for r, g, b in
                (self[x, y] for x in range(WIDTH)))
            out.append("  " + row)
        return "\n".join(out)
