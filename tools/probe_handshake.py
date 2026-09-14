"""Minimal, read-mostly probe: open the 0xFF60 raw-HID interface and try the
`start` (0x10) session handshake. Only safe opcodes are used."""
import sys
import time

import hid

VID, PID = 0x373B, 0x119B
BUFFER_LEN = 64
CMD_START = 0x10

def find_raw_path():
    for d in hid.enumerate(VID, PID):
        if d["usage_page"] == 0xFF60 and d["usage"] == 0x61:
            return d["path"]
    return None

def main():
    path = find_raw_path()
    print(f"raw-HID path: {path}")
    if not path:
        sys.exit("no 0xFF60/0x61 interface found")

    dev = hid.Device(path=path)
    print(f"opened: manufacturer={dev.manufacturer!r} product={dev.product!r}")

    pkt = bytearray(BUFFER_LEN)
    pkt[1] = CMD_START                      # byte0 stays 0x00 for bare commands
    print(f"TX ({len(pkt)}B): {pkt[:12].hex(' ')} ...")

    dev.write(bytes([0x00]) + bytes(pkt))    # leading 0x00 = unnumbered report
    t0 = time.time()
    rx = dev.read(BUFFER_LEN, timeout=1500)
    dt = (time.time() - t0) * 1000
    if rx:
        print(f"RX ({len(rx)}B, {dt:.0f}ms): {bytes(rx[:16]).hex(' ')} ...")
        print(f"  byte0=0x{rx[0]:02X} byte1=0x{rx[1]:02X} (echo of cmd 0x{CMD_START:02X}?)")
    else:
        print(f"no reply after {dt:.0f}ms")
    dev.close()

main()
