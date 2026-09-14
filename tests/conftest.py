"""CI runs without hidapi, on purpose: the bus, protocol and CLI plumbing are
testable with no keyboard attached. `yogo.cli` imports the device layer, which
imports `hid` at module level, so stand in an empty module when the real one
is missing. No test here ever opens the device."""

import sys
import types

try:
    import hid  # noqa: F401
except ImportError:
    sys.modules["hid"] = types.ModuleType("hid")
