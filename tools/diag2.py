"""Is 0xFF60 actually parsing these packets, or just echoing?

Malformed parameters go on the VOLATILE command (0x3C) only - never the
flash-writing one - so a bogus offset can't land anywhere persistent.
Also tries the second vendor channel (0xFF00, report id 0x3F).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import hid

from yogo import protocol as p

VID, PID = 0x373B, 0x119B

def rx(dev, n=64, timeout=400):
    try:
        r = dev.read(n, timeout=timeout)
    except hid.HIDException as e:
        return f"ERR {e}"
    return bytes(r).hex(" ") if r else "(no reply)"

print("=== A. malformed params on 0xFF60, volatile cmd 0x3C ===")
path = next(d["path"] for d in hid.enumerate(VID, PID)
            if d["usage_page"] == 0xFF60 and d["usage"] == 0x61)
dev = hid.Device(path=path)
dev.write(b"\x00" + p.build_packet(p.CMD_START))
print(f"  start        -> {rx(dev)[:47]}")

for label, off, dlen in [("offset=0xFFFF", 0xFFFF, 56),
                         ("datalen=0xFF ", 0, 0xFF),
                         ("both bogus   ", 0xDEAD & 0xFFFF, 0xFF)]:
    pkt = bytearray(p.build_packet(p.CMD_START_SYNC_DOT, b"\x11" * 56, 0, 56))
    pkt[2], pkt[3] = off & 0xFF, (off >> 8) & 0xFF
    pkt[4] = dlen
    dev.write(b"\x00" + bytes(pkt))
    print(f"  {label} -> {rx(dev)[:47]}")
    print("                  (byte7 of reply is the status field)")
dev.close()

print("\n=== B. second vendor channel 0xFF00 / report id 0x3F ===")
cands = [d for d in hid.enumerate(VID, PID) if d["usage_page"] == 0xFF00]
if not cands:
    print("  no 0xFF00 collection enumerated")
for d in cands:
    print(f"  path={d['path'].decode()} usage=0x{d['usage']:02X} iface={d['interface_number']}")
    try:
        d2 = hid.Device(path=d["path"])
    except Exception as e:
        print(f"    cannot open: {e}")
        print("    (macOS may require Input Monitoring for keyboard collections)")
        continue
    # report id 0x3F, 63-byte payload; mirror the same header shape
    body = bytearray(63)
    body[1] = p.CMD_START
    try:
        d2.write(bytes([0x3F]) + bytes(body))
        print(f"    start on 0x3F -> {rx(d2, 64)[:47]}")
    except Exception as e:
        print(f"    write failed: {e}")
    d2.close()
