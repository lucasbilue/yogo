"""
Wire protocol for the ATK Yogo 75 PRO 6x6 RGB "dot screen".

The keyboard speaks a VIA-style protocol over a QMK-style raw-HID interface;
the dot-screen commands are vendor extensions. Everything here was verified
against a Yogo 75 PRO over USB; the 2.4 GHz dongle and Bluetooth values are
untested.

Bluetooth
---------
Over Bluetooth LE the keyboard is one HID-over-GATT device with a single
report map, so if the vendor 0xFF60 collection is exposed at all it normally
carries a report ID and may use a smaller report than the wired 64 bytes.
Neither is guessed: `parse_vendor_reports` reads both from the report
descriptor, and the device layer falls back to a harmless handshake probe
when the OS won't hand the descriptor over.

Transport
---------
Interface : usage page 0xFF60, usage 0x61, unnumbered reports
Report    : 64 bytes wired / 32 bytes via the 2.4GHz dongle

Packet layout (wired)
---------------------
    off  size  field
    0    1     0xAA magic (0x00 for bare commands that carry no payload)
    1    1     command id
    2    2     payload offset, little endian
    4    1     payload length
    5    3     reserved, zero
    8    56    payload
"""

VENDOR_ID = 0x373B

# How the keyboard is attached. The dongle and Bluetooth are both wireless,
# but they frame reports differently, so they are kept apart.
LINK_USB = "usb"
LINK_DONGLE = "2.4GHz"
LINK_BLUETOOTH = "bluetooth"
LINK_ORDER = (LINK_USB, LINK_DONGLE, LINK_BLUETOOTH)   # preferred first

# productId -> (label, link)
PRODUCTS = {
    0x119B: ("YOGO 75 PRO", LINK_USB),
    0x11FF: ("YOGO 75 PRO 2.4G", LINK_DONGLE),   # untested
}

# Over Bluetooth the VID/PID come from the GATT PnP ID characteristic and are
# not guaranteed to match the USB ones, so the product string is a fallback.
PRODUCT_NAME_HINT = "yogo"

RAW_USAGE_PAGE = 0xFF60
RAW_USAGE = 0x61

MAGIC = 0xAA
BASE_OFFSET = 1     # command id lives at byte 1
SEQ_OFFSET = 5      # rolling transaction counter
STATUS_OFFSET = 7   # 0x00 outbound; device answers here
DATA_OFFSET = 8     # payload starts at byte 8

# Verified on the hardware: the device ACKs with 0x55 and rejects malformed
# framing with 0x0F.
STATUS_OK = 0x55
STATUS_REJECT = 0x0F

# Wired vs. dongle report geometry.
WIRED_BUFFER_LEN, WIRED_DATA_LEN = 64, 56
DONGLE_BUFFER_LEN, DONGLE_DATA_LEN = 32, 24
HEADER_LEN = DATA_OFFSET                   # 8 bytes before the payload

# Framings tried, in order, when a Bluetooth link's report descriptor is not
# available: (report id, report length). 0 means unnumbered.
BLUETOOTH_PROBE_FRAMINGS = (
    (0, 64), (0, 32),
    *((rid, n) for rid in range(1, 9) for n in (64, 32)),
)

# --- Command ids (only the ones this project needs) ----------------------
CMD_START = 0x10            # open a correspondence session; expires after ~5s
CMD_GET_KEYBOARD_FUNCTION = 0x14   # read the 64-byte device config
CMD_SET_KEYBOARD_FUNCTION = 0x15   # write it back (read-modify-write only!)
CMD_END = 0x11              # close the session
CMD_POWER_INFO = 0x30       # battery level + charge state
CMD_START_SYNC_DOT = 0x3C   # stream a volatile frame to the dot screen
CMD_END_SYNC_DOT = 0x3D     # leave streaming mode
CMD_SET_DOTSCREEN_MATRIX = 0x3B  # persist an image (writes flash - use sparingly)

# Deliberately NOT wrapped, to keep them un-sendable by accident. These
# either rewrite key maps / stored config or drop the MCU into its
# bootloader, and a malformed one can brick the keyboard:
#   0x31 reset_device, 0x23..0x2B write_led_matrix,
#   0x2D write_macro, 0xA0..0xA4 bootloader START/FLASH_WRITE/REBOOT/SWITCH_APP,
#   0xC0 SWITCH_BOOT
UNSAFE_COMMANDS = frozenset(
    {0x31, 0x23, 0x25, 0x27, 0x29, 0x2B, 0x2D, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xC0}
)

# --- Device status codes ------------------------------------------------
STATUS = {
    0x55: "OK",
    0x0F: "REJECTED (bad framing - check magic byte and sequence)",
    0x00: "SUCCESS",
    0xE0: "UNKNOWN_CMD",
    0xE1: "LENGTH_ERROR",
    0xE2: "CRC_ERROR",
    0xE3: "BLOCK_NUM_ERROR",
    0xE4: "BLOCK_SIZE_ERROR",
    0xE5: "WRITE_OFFSET_ERROR",
    0xE6: "READ_OFFSET_ERROR",
    0xE7: "ARGUMENT_ERROR",
    0xE8: "FLASH_OPERATION_FAILED",
    0xE9: "STATUS_ERROR",
}

# --- Display geometry ---------------------------------------------------
WIDTH = HEIGHT = 6
PIXELS = WIDTH * HEIGHT              # 36
FRAME_BYTES_565 = PIXELS * 2         # 72, streaming path
FRAME_BYTES_888 = PIXELS * 3         # 108, persistent path


def rgb_to_565(r: int, g: int, b: int) -> int:
    """Pack 8-bit RGB into RGB565."""
    return ((r & 0xFF) >> 3) << 11 | ((g & 0xFF) >> 2) << 5 | ((b & 0xFF) >> 3)


def rgb565_to_rgb(v: int) -> tuple[int, int, int]:
    """Inverse of rgb_to_565, expanding each channel by bit-replication."""
    r, g, b = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def build_packet(cmd: int, payload: bytes = b"", offset: int = 0,
                 data_len: int | None = None, seq: int = 0,
                 buffer_len: int = WIRED_BUFFER_LEN) -> bytes:
    """Assemble one raw-HID report.

    Byte 0 carries the 0xAA magic on *every* packet, including bare commands
    that have no payload, and byte 5 is a rolling sequence counter. Omitting
    either makes the device answer 0x0F instead of 0x55 - which is what made
    an earlier version of this look like a dumb echo endpoint.
    """
    if cmd in UNSAFE_COMMANDS:
        raise ValueError(f"refusing to build unsafe command 0x{cmd:02X}")
    if len(payload) > buffer_len - DATA_OFFSET:
        raise ValueError(f"payload {len(payload)}B exceeds {buffer_len - DATA_OFFSET}B")

    pkt = bytearray(buffer_len)
    pkt[0] = MAGIC
    pkt[BASE_OFFSET] = cmd
    pkt[2] = offset & 0xFF
    pkt[3] = (offset >> 8) & 0xFF
    if payload:
        pkt[4] = len(payload) if data_len is None else data_len
        pkt[DATA_OFFSET:DATA_OFFSET + len(payload)] = payload
    elif data_len:
        pkt[4] = data_len
    pkt[SEQ_OFFSET] = seq & 0xFF
    return bytes(pkt)


def frame_to_565_bytes(pixels) -> bytes:
    """Flatten 36 (r,g,b) tuples into 72 bytes of little-endian RGB565.

    Order is row-major from the top-left, i.e. index = row * 6 + col, verified
    on the hardware.
    """
    if len(pixels) != PIXELS:
        raise ValueError(f"expected {PIXELS} pixels, got {len(pixels)}")
    out = bytearray()
    for r, g, b in pixels:
        v = rgb_to_565(r, g, b)
        out.append(v & 0xFF)
        out.append((v >> 8) & 0xFF)
    return bytes(out)


def frame_to_888_bytes(pixels) -> bytes:
    """Flatten 36 (r,g,b) tuples into 108 bytes of plain RGB888.

    This is the layout the persistent custom-image command (0x3B) expects,
    as opposed to the RGB565 used by the volatile streaming command.
    """
    if len(pixels) != PIXELS:
        raise ValueError(f"expected {PIXELS} pixels, got {len(pixels)}")
    out = bytearray()
    for r, g, b in pixels:
        out += bytes((r & 0xFF, g & 0xFF, b & 0xFF))
    return bytes(out)


# --- Device config blob (command 0x14 read / 0x15 write) -----------------
# 64 bytes: the firmware returns 56 on a single read; pad the rest with zeros
# and write back as 56 + 8. Only ever modify a field of
# a blob you have just read - never build one from scratch.
CONFIG_LEN = 64
CONFIG_READ_LEN = 56

# Verified on the hardware: switching between an animation mode and Custom
# mode changes exactly this one byte of the config.
CONFIG_SCREEN_MODE = 30
SCREEN_MODE_CUSTOM = 0x06     # static image from set_dotscreen_matrix


# --- HID report descriptor ---------------------------------------------
def parse_vendor_reports(descriptor: bytes, usage_page: int = RAW_USAGE_PAGE) -> dict:
    """Find the report ID and sizes the vendor collection uses.

    A minimal HID report-descriptor walker: it tracks the global items that
    matter (usage page, report size/count/ID, push/pop) and totals the bits of
    every Input and Output main item declared while `usage_page` is current.

    Returns {"report_id": int, "output_len": int|None, "input_len": int|None},
    with lengths in bytes excluding the report-ID byte, or {} when the
    descriptor declares nothing on that page. Where several report IDs use
    the page, the first one with an Output wins.
    """
    g = {"page": 0, "size": 0, "count": 0, "rid": 0}
    stack: list[dict] = []
    bits: dict[tuple[str, int], int] = {}
    order: list[int] = []
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:                           # long item: skip it
            if i + 1 >= len(descriptor):
                break
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        data = descriptor[i + 1:i + 1 + size]
        value = int.from_bytes(data, "little") if data else 0
        tag = prefix & 0xFC
        i += 1 + size

        if tag == 0x04:                              # Usage Page
            g["page"] = value
        elif tag == 0x74:                            # Report Size
            g["size"] = value
        elif tag == 0x94:                            # Report Count
            g["count"] = value
        elif tag == 0x84:                            # Report ID
            g["rid"] = value
        elif tag == 0xA4:                            # Push
            stack.append(dict(g))
        elif tag == 0xB4 and stack:                  # Pop
            g = stack.pop()
        elif tag in (0x80, 0x90) and g["page"] == usage_page:   # Input / Output
            kind = "in" if tag == 0x80 else "out"
            key = (kind, g["rid"])
            bits[key] = bits.get(key, 0) + g["size"] * g["count"]
            if g["rid"] not in order:
                order.append(g["rid"])

    if not order:
        return {}
    rid = next((r for r in order if ("out", r) in bits), order[0])

    def _bytes(kind):
        b = bits.get((kind, rid))
        return None if b is None else (b + 7) // 8

    return {"report_id": rid, "output_len": _bytes("out"), "input_len": _bytes("in")}
