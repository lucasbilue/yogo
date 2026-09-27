"""Bluetooth transport, exercised against a simulated keyboard.

No hardware and no hidapi: `yogo.device.hid` is swapped for a fake that
enumerates interfaces and answers packets the way the firmware does (echo,
status byte 7 = 0x55), optionally behind a report ID and at a smaller report
size, which is what a Bluetooth LE link looks like.
"""

import enum
import types

import pytest

from yogo import device as devmod
from yogo import protocol as p

# --- report descriptors -------------------------------------------------
# The QMK raw_hid collection as the USB interface declares it: unnumbered,
# 32 bytes in / 32 out here (the Yogo uses 64 on USB).
QMK_RAW = bytes.fromhex(
    "06 60 FF 09 61 A1 01"          # usage page FF60, usage 61, collection
    " 09 62 15 00 26 FF 00 75 08 95 20 81 02"   # 32 x 8 bit input
    " 09 63 15 00 26 FF 00 75 08 95 20 91 02"   # 32 x 8 bit output
    " C0")

# A BLE-style single report map: keyboard (ID 1), consumer (ID 2), then the
# vendor collection on ID 5 with 64-byte reports.
BLE_MAP = bytes.fromhex(
    "05 01 09 06 A1 01 85 01 05 07 19 E0 29 E7 15 00 25 01 75 01 95 08 81 02"
    " 95 06 75 08 26 FF 00 19 00 2A FF 00 81 00 C0"
    " 05 0C 09 01 A1 01 85 02 75 10 95 01 26 FF 03 19 00 2A FF 03 81 00 C0"
    " 06 60 FF 09 61 A1 01 85 05"
    " 09 62 15 00 26 FF 00 75 08 95 40 81 02"
    " 09 63 15 00 26 FF 00 75 08 95 40 91 02 C0")


def test_descriptor_unnumbered_raw_hid():
    assert p.parse_vendor_reports(QMK_RAW) == {
        "report_id": 0, "output_len": 32, "input_len": 32}


def test_descriptor_ble_report_map_finds_vendor_id():
    assert p.parse_vendor_reports(BLE_MAP) == {
        "report_id": 5, "output_len": 64, "input_len": 64}


def test_descriptor_without_vendor_page():
    keyboard_only = BLE_MAP[:BLE_MAP.index(bytes.fromhex("06 60 FF"))]
    assert p.parse_vendor_reports(keyboard_only) == {}


def test_descriptor_push_pop_restores_page():
    desc = bytes.fromhex(
        "06 60 FF A4 05 01 75 08 95 04 81 02 B4"   # push, other page, pop
        " 75 08 95 10 91 02")                      # back on FF60: 16 B out
    assert p.parse_vendor_reports(desc)["output_len"] == 16


# --- fake hidapi --------------------------------------------------------
class BusType(enum.Enum):
    UNKNOWN = 0
    USB = 1
    BLUETOOTH = 2


class FakeKeyboard:
    """Answers like the firmware: echo the packet with status 0x55."""

    def __init__(self, report_id=0, report_len=64, config=None, descriptor=None):
        self.report_id = report_id
        self.report_len = report_len
        self.descriptor = descriptor
        self.config = bytearray(config or range(56))
        self.written = []
        self.pending = []
        self.stale = []            # replies to deliver before the real one

    def write(self, data):
        rid, pkt = data[0], bytes(data[1:])
        if rid != self.report_id or len(pkt) != self.report_len:
            raise devmod.hid.HIDException("bad report")   # what macOS does
        self.written.append(pkt)
        reply = bytearray(pkt)
        reply[p.STATUS_OFFSET] = p.STATUS_OK
        if pkt[p.BASE_OFFSET] == p.CMD_GET_KEYBOARD_FUNCTION:
            off, n = pkt[2] | pkt[3] << 8, pkt[4]
            reply[p.DATA_OFFSET:p.DATA_OFFSET + n] = self.config[off:off + n]
        prefix = bytes((self.report_id,)) if self.report_id else b""
        self.pending += [prefix + s for s in self.stale] + [prefix + bytes(reply)]
        self.stale = []
        return len(data)

    def read(self, size, timeout=None):
        return self.pending.pop(0)[:size] if self.pending else b""


def install_fake_hid(monkeypatch, interfaces, keyboards):
    """interfaces: enumerate() dicts; keyboards: path -> FakeKeyboard."""

    class HIDException(Exception):
        pass

    class Device:
        def __init__(self, path):
            self._kb = keyboards[path]
            if self._kb.descriptor is not None:
                self.get_report_descriptor = lambda: self._kb.descriptor

        def write(self, data):
            return self._kb.write(data)

        def read(self, size, timeout=None):
            return self._kb.read(size, timeout)

        def close(self):
            pass

    def enumerate_(vid=0, pid=0):
        return [dict(d) for d in interfaces if vid in (0, d["vendor_id"])]

    fake = types.SimpleNamespace(HIDException=HIDException, Device=Device,
                                 enumerate=enumerate_, BusType=BusType)
    monkeypatch.setattr(devmod, "hid", fake)
    devmod.YogoDisplay._framing_cache.clear()
    return fake


def iface(path, pid, bus, name="YOGO 75 PRO", vid=p.VENDOR_ID, page=p.RAW_USAGE_PAGE):
    return {"path": path, "vendor_id": vid, "product_id": pid, "bus_type": bus,
            "usage_page": page, "usage": p.RAW_USAGE, "product_string": name}


# --- discovery ----------------------------------------------------------
def test_discover_classifies_links_and_prefers_usb(monkeypatch):
    install_fake_hid(monkeypatch, [
        iface(b"bt", 0x1234, BusType.BLUETOOTH),
        iface(b"usb", 0x119B, BusType.USB),
        iface(b"kbd", 0x119B, BusType.USB, page=0x0001),     # not the display
    ], {})
    found = devmod.YogoDisplay.discover()
    assert [(d.path, d.link) for d in found] == [
        (b"usb", p.LINK_USB), (b"bt", p.LINK_BLUETOOTH)]


def test_discover_finds_bluetooth_by_name_when_ids_differ(monkeypatch):
    # PnP ID over BLE can carry a Bluetooth SIG vendor source, not 0x373B.
    install_fake_hid(monkeypatch, [
        iface(b"bt", 0x0001, BusType.BLUETOOTH, vid=0x05AC, name="YOGO75 PRO"),
        iface(b"other", 0x0001, BusType.BLUETOOTH, vid=0x05AC, name="Magic Mouse"),
    ], {})
    assert [d.path for d in devmod.YogoDisplay.discover()] == [b"bt"]


def test_discover_old_hidapi_without_bus_type(monkeypatch):
    rows = [iface(b"usb", 0x119B, None), iface(b"bt", 0x2222, None)]
    for r in rows:
        del r["bus_type"]
    install_fake_hid(monkeypatch, rows, {})
    assert {d.path: d.link for d in devmod.YogoDisplay.discover()} == {
        b"usb": p.LINK_USB, b"bt": p.LINK_BLUETOOTH}


def test_yogo_link_env_puts_bluetooth_first(monkeypatch):
    install_fake_hid(monkeypatch, [
        iface(b"usb", 0x119B, BusType.USB),
        iface(b"bt", 0x1234, BusType.BLUETOOTH),
    ], {})
    monkeypatch.setenv("YOGO_LINK", "bluetooth")
    assert devmod.YogoDisplay.discover()[0].path == b"bt"


def test_open_first_falls_back_to_bluetooth_when_usb_busy(monkeypatch):
    kb = FakeKeyboard(report_id=5, report_len=64, descriptor=BLE_MAP)
    fake = install_fake_hid(monkeypatch, [
        iface(b"usb", 0x119B, BusType.USB),
        iface(b"bt", 0x1234, BusType.BLUETOOTH),
    ], {b"bt": kb})
    real_device = fake.Device

    def device(path):
        if path == b"usb":
            raise fake.HIDException("exclusive access and device already open")
        return real_device(path)

    fake.Device = device
    dev = devmod.YogoDisplay.open_first()
    assert dev.link == p.LINK_BLUETOOTH


# --- framing ------------------------------------------------------------
def test_bluetooth_framing_from_descriptor(monkeypatch):
    kb = FakeKeyboard(report_id=5, report_len=64, descriptor=BLE_MAP)
    install_fake_hid(monkeypatch, [iface(b"bt", 0x1234, BusType.BLUETOOTH)], {b"bt": kb})
    with devmod.YogoDisplay.discover()[0].open() as dev:
        assert (dev.report_id, dev.buffer_len, dev.max_data) == (5, 64, 56)
        dev.show([(255, 0, 0)] * p.PIXELS)
        frames = [w for w in kb.written if w[p.BASE_OFFSET] == p.CMD_START_SYNC_DOT]
        assert len(frames) == 2                            # 56 + 16
    assert kb.written[-1][p.BASE_OFFSET] == p.CMD_END_SYNC_DOT


def test_bluetooth_framing_by_probe_when_no_descriptor(monkeypatch):
    kb = FakeKeyboard(report_id=3, report_len=32)          # like the dongle
    install_fake_hid(monkeypatch, [iface(b"bt", 0x1234, BusType.BLUETOOTH)], {b"bt": kb})
    dev = devmod.YogoDisplay.discover()[0].open()
    assert (dev.report_id, dev.buffer_len, dev.max_data) == (3, 32, 24)
    dev.show([(0, 0, 255)] * p.PIXELS)
    sizes = [w[4] for w in kb.written if w[p.BASE_OFFSET] == p.CMD_START_SYNC_DOT]
    assert sizes == [24, 24, 24]                           # 72 B in thirds
    assert devmod.YogoDisplay._framing_cache[b"bt"] == (3, 32)


def test_bluetooth_probe_gives_up_cleanly(monkeypatch):
    kb = FakeKeyboard(report_id=42, report_len=20)         # nothing we try
    install_fake_hid(monkeypatch, [iface(b"bt", 0x1234, BusType.BLUETOOTH)], {b"bt": kb})
    with pytest.raises(devmod.ProtocolError, match="yogo probe"):
        devmod.YogoDisplay.discover()[0].open()


def test_bluetooth_drops_stale_replies(monkeypatch):
    kb = FakeKeyboard(report_id=5, report_len=64, descriptor=BLE_MAP)
    install_fake_hid(monkeypatch, [iface(b"bt", 0x1234, BusType.BLUETOOTH)], {b"bt": kb})
    dev = devmod.YogoDisplay.discover()[0].open()
    stale = bytearray(64)
    stale[0], stale[p.SEQ_OFFSET], stale[p.STATUS_OFFSET] = p.MAGIC, 0xEE, p.STATUS_REJECT
    kb.stale = [bytes(stale)]            # a late 0x0F for an older command
    assert dev.power_info() is not None  # would raise if the stale one won


def test_config_round_trip_on_small_reports(monkeypatch):
    kb = FakeKeyboard(report_id=3, report_len=32, config=range(100, 156))
    install_fake_hid(monkeypatch, [iface(b"bt", 0x1234, BusType.BLUETOOTH)], {b"bt": kb})
    dev = devmod.YogoDisplay.discover()[0].open()
    cfg = dev.read_config()
    assert bytes(cfg[:56]) == bytes(range(100, 156)) and len(cfg) == p.CONFIG_LEN
    kb.written.clear()
    dev.write_config(bytes(cfg))
    writes = [(w[2], w[4]) for w in kb.written
              if w[p.BASE_OFFSET] == p.CMD_SET_KEYBOARD_FUNCTION]
    assert writes == [(0, 24), (24, 24), (48, 16)]


def test_usb_path_unchanged(monkeypatch):
    kb = FakeKeyboard(report_id=0, report_len=64)
    install_fake_hid(monkeypatch, [iface(b"usb", 0x119B, BusType.USB)], {b"usb": kb})
    with devmod.YogoDisplay.discover()[0].open() as dev:
        assert (dev.report_id, dev.buffer_len) == (0, 64)
        dev.write_config(bytes(64))
    writes = [(w[2], w[4]) for w in kb.written
              if w[p.BASE_OFFSET] == p.CMD_SET_KEYBOARD_FUNCTION]
    assert writes == [(0, 56), (56, 8)]                    # as before
