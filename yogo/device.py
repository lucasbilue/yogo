"""HID transport for the Yogo 75 PRO dot screen."""

from __future__ import annotations

import time

import hid

from . import protocol as p


class YogoNotFound(RuntimeError):
    pass


class ProtocolError(RuntimeError):
    pass


class YogoDisplay:
    """Talks to the 6x6 dot screen over the 0xFF60/0x61 raw-HID interface.

    The keyboard requires a "correspondence" session (command 0x10) before it
    will accept dot-screen writes, and that session lapses after about five
    seconds. Sessions are refreshed automatically, so callers can just push
    frames and ignore the handshake.
    """

    SESSION_TTL = 4.0   # the session lapses after ~5s; refresh a little early

    def __init__(self, path: bytes, product_id: int, label: str, wireless: bool):
        self.path = path
        self.product_id = product_id
        self.label = label
        self.wireless = wireless
        self.buffer_len = p.DONGLE_BUFFER_LEN if wireless else p.WIRED_BUFFER_LEN
        self.max_data = p.DONGLE_DATA_LEN if wireless else p.WIRED_DATA_LEN
        self._dev: hid.Device | None = None
        self._session_at = 0.0
        self._seq = 1          # rolling counter; the device echoes it back
        self._streaming = False   # only then does the screen need handing back

    # -- discovery --------------------------------------------------------
    @classmethod
    def discover(cls) -> list[YogoDisplay]:
        found = []
        for d in hid.enumerate(p.VENDOR_ID, 0):
            if d["product_id"] not in p.PRODUCTS:
                continue
            if d["usage_page"] != p.RAW_USAGE_PAGE or d["usage"] != p.RAW_USAGE:
                continue
            label, wireless = p.PRODUCTS[d["product_id"]]
            found.append(cls(d["path"], d["product_id"], label, wireless))
        return found

    @classmethod
    def open_first(cls) -> YogoDisplay:
        devices = cls.discover()
        if not devices:
            raise YogoNotFound(
                "no ATK Yogo raw-HID interface (0xFF60/0x61) found - is the "
                "keyboard connected by USB or its 2.4GHz dongle?"
            )
        return devices[0].open()

    # -- lifecycle --------------------------------------------------------
    def open(self) -> YogoDisplay:
        self._dev = hid.Device(path=self.path)
        return self

    def close(self) -> None:
        if self._dev:
            self._dev.close()
            self._dev = None

    def __enter__(self) -> YogoDisplay:
        return self if self._dev else self.open()

    def __exit__(self, *_exc) -> None:
        try:
            # Only relinquish the screen if we actually took it over. A
            # persistent image written with 0x3B must NOT be followed by
            # end_sync_dot, or the firmware resumes its own animation and
            # the image never appears.
            if self._streaming:
                self.end_stream()
        finally:
            self.close()

    # -- raw transfer -----------------------------------------------------
    def _transfer(self, packet: bytes, read_reply: bool = True, timeout: int = 300):
        if not self._dev:
            raise RuntimeError("device not open")
        # hidapi wants a leading report-id byte; this interface is unnumbered.
        self._dev.write(b"\x00" + packet)
        if not read_reply:
            return None
        try:
            return self._dev.read(self.buffer_len, timeout=timeout) or None
        except hid.HIDException:
            return None

    def _command(self, cmd: int, payload: bytes = b"", offset: int = 0,
                 data_len: int | None = None, read_reply: bool = True,
                 check: bool = True):
        seq = self._seq
        self._seq = (self._seq % 255) + 1          # 1..255, never 0
        rx = self._transfer(
            p.build_packet(cmd, payload, offset, data_len, seq, self.buffer_len),
            read_reply=read_reply,
        )
        if check and rx is not None:
            status = rx[p.STATUS_OFFSET]
            if status != p.STATUS_OK:
                raise ProtocolError(
                    f"command 0x{cmd:02X} rejected: status 0x{status:02X} "
                    f"({p.STATUS.get(status, 'unknown')})")
        return rx

    # -- session ----------------------------------------------------------
    def begin_session(self, force: bool = False) -> None:
        if force or (time.monotonic() - self._session_at) >= self.SESSION_TTL:
            self._command(p.CMD_START)
            self._session_at = time.monotonic()

    def end_session(self) -> None:
        self._command(p.CMD_END)
        self._session_at = 0.0

    # -- features ---------------------------------------------------------
    def power_info(self) -> dict | None:
        """Battery percentage and charge state - a harmless comms check."""
        self.begin_session()
        rx = self._command(p.CMD_POWER_INFO)
        if not rx or len(rx) < p.DATA_OFFSET + 2:
            return None
        level = rx[p.DATA_OFFSET]
        state = rx[p.DATA_OFFSET + 1]
        return {
            "percent": min(level, 100),
            "charging": state == 2,
            "raw": bytes(rx[:12]).hex(" "),
        }

    def show(self, pixels) -> None:
        """Push one volatile 6x6 frame (36 RGB tuples) to the screen.

        Uses the streaming command (0x3C), so nothing is written to flash and
        this is safe to call at video frame rates.
        """
        self.begin_session()
        self._streaming = True
        data = p.frame_to_565_bytes(pixels)
        for off in range(0, len(data), self.max_data):
            piece = data[off:off + self.max_data]
            self._command(p.CMD_START_SYNC_DOT, piece, offset=off, data_len=len(piece))

    def end_stream(self) -> None:
        """Leave streaming mode and hand the screen back to the firmware."""
        if not self._dev or not self._streaming:
            return
        self.begin_session()
        self._command(p.CMD_END_SYNC_DOT)
        self._streaming = False

    def read_config(self) -> bytearray:
        """Read the 64-byte device config (56 from the device + 8 zero pad)."""
        self.begin_session()
        rx = self._command(p.CMD_GET_KEYBOARD_FUNCTION, offset=0,
                           data_len=p.CONFIG_READ_LEN)
        if rx is None:
            raise ProtocolError("no reply to config read")
        cfg = bytearray(rx[p.DATA_OFFSET:p.DATA_OFFSET + p.CONFIG_READ_LEN])
        if len(cfg) != p.CONFIG_READ_LEN:
            raise ProtocolError(f"short config read: {len(cfg)}B")
        cfg.extend(b"\x00" * (p.CONFIG_LEN - p.CONFIG_READ_LEN))
        return cfg

    def write_config(self, cfg: bytes) -> None:
        """Write a 64-byte config back, chunked 56 + 8."""
        if len(cfg) != p.CONFIG_LEN:
            raise ValueError(f"config must be {p.CONFIG_LEN}B, got {len(cfg)}")
        self.begin_session()
        self._command(p.CMD_SET_KEYBOARD_FUNCTION, bytes(cfg[:56]), 0, 56)
        self._command(p.CMD_SET_KEYBOARD_FUNCTION, bytes(cfg[56:]), 56, 8)

    def set_screen_mode(self, mode: int = p.SCREEN_MODE_CUSTOM) -> bool:
        """Put the dot screen into a given mode, e.g. Custom.

        Read-modify-write of a single byte: the image written by write_image()
        is only visible while the screen is in Custom mode, otherwise a
        built-in animation keeps ownership of it. Returns True if it changed.
        """
        cfg = self.read_config()
        if cfg[p.CONFIG_SCREEN_MODE] == mode:
            return False
        cfg[p.CONFIG_SCREEN_MODE] = mode
        self.write_config(cfg)
        return True

    def write_image(self, pixels) -> None:
        """Write a persistent custom image (command 0x3B), 36 RGB tuples.

        This is what the "Custom" screen mode displays, and unlike
        the streaming command it takes effect without the firmware being in
        music-sync mode. It updates the keyboard's stored custom slot, so do
        not call it in a fast loop - treat it as a save, not a frame push.
        """
        self.begin_session()
        data = p.frame_to_888_bytes(pixels)
        for off in range(0, len(data), self.max_data):
            piece = data[off:off + self.max_data]
            # The declared length is always the full DATA_LEN here, even on the
            # short final chunk, so mirror that exactly.
            self._command(p.CMD_SET_DOTSCREEN_MATRIX, piece, offset=off,
                          data_len=self.max_data)

    def __repr__(self) -> str:
        link = "2.4GHz" if self.wireless else "USB"
        return f"<YogoDisplay {self.label} 0x{self.product_id:04X} {link}>"
