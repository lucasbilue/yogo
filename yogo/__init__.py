"""Drive the 6x6 RGB dot-matrix display on an ATK Yogo 75 PRO keyboard.

The device layer is imported lazily so that `yogo.bus` and `yogo.protocol` -
both pure stdlib - can be used by a harness adapter that has no hidapi
installed and no business opening the USB device. Only the process that
actually renders needs the driver.
"""

from .protocol import HEIGHT, PIXELS, WIDTH, rgb565_to_rgb, rgb_to_565

__all__ = [
    "YogoDisplay", "YogoNotFound", "ProtocolError",
    "WIDTH", "HEIGHT", "PIXELS",
    "rgb_to_565", "rgb565_to_rgb",
]

_LAZY = {"YogoDisplay": "device", "YogoNotFound": "device", "ProtocolError": "device"}


def __getattr__(name):
    """PEP 562 lazy attributes: importing hid is deferred until first use."""
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    return getattr(importlib.import_module(f".{mod}", __name__), name)
