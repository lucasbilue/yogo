"""HID transport for the Yogo 75 PRO dot screen.

Works over USB, the 2.4 GHz dongle, and Bluetooth LE. The command set is the
same on every link; only the framing differs (report ID, report length), and
for Bluetooth that framing is learned from the keyboard when it is opened.
"""

from __future__ import annotations

import os
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

    # Bluetooth LE round trips ride the connection interval (7.5-30 ms, more
    # when the keyboard is saving power), so give replies longer to arrive.
    REPLY_TIMEOUT_MS = {p.LINK_USB: 300, p.LINK_DONGLE: 400, p.LINK_BLUETOOTH: 700}

    # Bluetooth framing that worked last time, per interface path, so a
    # reconnect (the keyboard sleeps and wakes) doesn't re-probe from scratch.
    _framing_cache: dict[bytes, tuple[int, int]] = {}

    def __init__(self, path: bytes, product_id: int, label: str, link: str,
                 vendor_id: int = p.VENDOR_ID):
        self.path = path
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.label = label
        self.link = link
        self.report_id = 0     # 0 = unnumbered reports (USB, dongle)
        self._set_geometry(p.DONGLE_BUFFER_LEN if link == p.LINK_DONGLE
                           else p.WIRED_BUFFER_LEN)
        self._dev: hid.Device | None = None
        self._session_at = 0.0
        self._seq = 1          # rolling counter; the device echoes it back
        self._streaming = False   # only then does the screen need handing back

    @property
    def wireless(self) -> bool:
        return self.link != p.LINK_USB

    def _set_geometry(self, buffer_len: int) -> None:
        self.buffer_len = buffer_len
        self.max_data = buffer_len - p.HEADER_LEN

    # -- discovery --------------------------------------------------------
    @staticmethod
    def _link_of(d: dict) -> str | None:
        """Work out how an enumerated interface is attached."""
        bus_type = d.get("bus_type")           # hidapi >= 0.13 only
        bus_name = getattr(bus_type, "name", str(bus_type or "")).upper()
        is_yogo = (d.get("vendor_id") == p.VENDOR_ID
                   or p.PRODUCT_NAME_HINT in (d.get("product_string") or "").lower())
        if not is_yogo:
            return None
        if "BLUETOOTH" in bus_name or bus_type == 2:
            return p.LINK_BLUETOOTH
        known = p.PRODUCTS.get(d.get("product_id"))
        if known and d.get("vendor_id") == p.VENDOR_ID:
            return known[1]
        if "USB" in bus_name:
            return p.LINK_USB                  # a Yogo PID we haven't met yet
        # Bus unknown (older hidapi) and not a USB product ID we know: that
        # is what a Bluetooth link looks like there.
        return p.LINK_BLUETOOTH

    @classmethod
    def discover(cls) -> list[YogoDisplay]:
        """Every Yogo raw-HID interface, best link first.

        Set YOGO_LINK=usb|2.4GHz|bluetooth to put one link ahead of the rest.
        """
        seen: set[bytes] = set()
        found: list[YogoDisplay] = []
        # By vendor first, then by name: over Bluetooth the IDs come from the
        # GATT PnP ID characteristic and may not be the USB ones.
        for d in [*hid.enumerate(p.VENDOR_ID, 0), *hid.enumerate(0, 0)]:
            if d["path"] in seen:
                continue
            if d["usage_page"] != p.RAW_USAGE_PAGE or d["usage"] != p.RAW_USAGE:
                continue
            link = cls._link_of(d)
            if link is None:
                continue
            seen.add(d["path"])
            label = p.PRODUCTS.get(d["product_id"], (d.get("product_string")
                                                     or "YOGO 75 PRO",))[0]
            if link == p.LINK_BLUETOOTH:
                label = f"{label.removesuffix(' 2.4G')} BT"
            found.append(cls(d["path"], d["product_id"], label, link,
                             vendor_id=d.get("vendor_id", p.VENDOR_ID)))

        prefer = os.environ.get("YOGO_LINK", "").strip().lower()
        order = {name.lower(): i for i, name in enumerate(p.LINK_ORDER)}
        found.sort(key=lambda dev: (dev.link.lower() != prefer,
                                    order.get(dev.link.lower(), 99)))
        return found

    @classmethod
    def open_first(cls) -> YogoDisplay:
        devices = cls.discover()
        if not devices:
            raise YogoNotFound(
                "no ATK Yogo raw-HID interface (0xFF60/0x61) found - is the "
                "keyboard connected by USB, its 2.4GHz dongle, or Bluetooth? "
                "Run `yogo probe` to see what the OS exposes."
            )
        errors = []
        for dev in devices:              # e.g. USB busy with ATK HUB -> try BT
            try:
                return dev.open()
            except Exception as e:       # hid.HIDException is not an OSError
                errors.append(f"{dev!r}: {e}")
        raise YogoNotFound("could not open any Yogo interface:\n  " + "\n  ".join(errors))

    # -- lifecycle --------------------------------------------------------
    def open(self) -> YogoDisplay:
        self._dev = hid.Device(path=self.path)
        if self.link == p.LINK_BLUETOOTH:
            try:
                self._configure_bluetooth()
            except Exception:
                self.close()
                raise
        return self

    def _configure_bluetooth(self) -> None:
        """Learn the Bluetooth framing: from the descriptor, else by probing."""
        layout = self._descriptor_layout()
        if layout and layout.get("output_len"):
            self.report_id = layout["report_id"]
            self._set_geometry(layout["output_len"])
            self._session_at = 0.0
            self.begin_session(force=True)          # prove it answers
            return

        cached = self._framing_cache.get(self.path)
        candidates = ([cached] if cached else []) + [
            f for f in p.BLUETOOTH_PROBE_FRAMINGS if f != cached]
        for rid, length in candidates:
            self.report_id = rid
            self._set_geometry(length)
            try:
                rx = self._command(p.CMD_START, check=False, timeout=250)
            except Exception:
                continue                             # wrong size: write refused
            if rx and rx[p.STATUS_OFFSET] == p.STATUS_OK:
                self._session_at = time.monotonic()
                self._framing_cache[self.path] = (rid, length)
                return
        self._framing_cache.pop(self.path, None)
        raise ProtocolError(
            "the keyboard is paired over Bluetooth and exposes the 0xFF60 "
            "interface, but did not answer the handshake with any known "
            "framing. Run `yogo probe` and include its output in a bug report.")

    def _descriptor_layout(self) -> dict:
        getter = getattr(self._dev, "get_report_descriptor", None)  # hidapi >= 0.14
        if getter is None:
            return {}
        try:
            return p.parse_vendor_reports(bytes(getter()))
        except Exception:
            return {}

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
    def _read_reply(self, seq: int, timeout: int) -> bytes | None:
        """Read until the reply to `seq` arrives, dropping stale ones.

        A reply that missed its own timeout (common on a busy Bluetooth link)
        would otherwise be taken as the answer to the next command. USB keeps
        the original, hardware-verified behaviour of taking the first reply.
        """
        deadline = time.monotonic() + timeout / 1000
        while True:
            left = int((deadline - time.monotonic()) * 1000)
            if left <= 0:
                return None
            try:
                rx = self._dev.read(self.buffer_len + (1 if self.report_id else 0),
                                    timeout=left)
            except hid.HIDException:
                return None
            if not rx:
                return None
            rx = bytes(rx)
            if self.link == p.LINK_USB:
                return rx                    # verified path: unchanged
            # Numbered reports come back with the report ID in front.
            if self.report_id and len(rx) > 1 and rx[0] == self.report_id \
                    and rx[1] == p.MAGIC:
                rx = rx[1:]
            if len(rx) <= p.STATUS_OFFSET or rx[0] != p.MAGIC:
                continue                     # not ours (e.g. another report)
            if rx[p.SEQ_OFFSET] in (seq, 0):
                return rx

    def _transfer(self, packet: bytes, read_reply: bool = True,
                  timeout: int | None = None):
        if not self._dev:
            raise RuntimeError("device not open")
        # hidapi wants a leading report-id byte: 0 for the unnumbered USB and
        # dongle interfaces, the collection's own ID over Bluetooth.
        self._dev.write(bytes((self.report_id,)) + packet)
        if not read_reply:
            return None
        if timeout is None:
            timeout = self.REPLY_TIMEOUT_MS.get(self.link, 300)
        return self._read_reply(packet[p.SEQ_OFFSET], timeout)

    def _command(self, cmd: int, payload: bytes = b"", offset: int = 0,
                 data_len: int | None = None, read_reply: bool = True,
                 check: bool = True, timeout: int | None = None):
        seq = self._seq
        self._seq = (self._seq % 255) + 1          # 1..255, never 0
        rx = self._transfer(
            p.build_packet(cmd, payload, offset, data_len, seq, self.buffer_len),
            read_reply=read_reply, timeout=timeout,
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
        """Read the 64-byte device config (56 from the device + 8 zero pad).

        One read on USB; on smaller wireless reports it is read in
        payload-sized pieces at increasing offsets.
        """
        self.begin_session()
        cfg = bytearray()
        while len(cfg) < p.CONFIG_READ_LEN:
            want = min(self.max_data, p.CONFIG_READ_LEN - len(cfg))
            rx = self._command(p.CMD_GET_KEYBOARD_FUNCTION, offset=len(cfg),
                               data_len=want)
            if rx is None:
                raise ProtocolError("no reply to config read")
            piece = rx[p.DATA_OFFSET:p.DATA_OFFSET + want]
            if len(piece) != want:
                raise ProtocolError(f"short config read: {len(cfg) + len(piece)}B")
            cfg += piece
        cfg.extend(b"\x00" * (p.CONFIG_LEN - p.CONFIG_READ_LEN))
        return cfg

    def write_config(self, cfg: bytes) -> None:
        """Write a 64-byte config back: 56 + 8 on USB, smaller pieces wireless."""
        if len(cfg) != p.CONFIG_LEN:
            raise ValueError(f"config must be {p.CONFIG_LEN}B, got {len(cfg)}")
        self.begin_session()
        for off in range(0, p.CONFIG_LEN, self.max_data):
            piece = bytes(cfg[off:off + self.max_data])
            self._command(p.CMD_SET_KEYBOARD_FUNCTION, piece, off, len(piece))

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
        rid = f" report-id {self.report_id}" if self.report_id else ""
        return (f"<YogoDisplay {self.label} 0x{self.product_id:04X} "
                f"{self.link}{rid}>")
