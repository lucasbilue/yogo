"""Probe the 0xFF00 / report-id 0x3F vendor channel.

Every valid command is paired with an unused-opcode control, so we can tell a
real parser from an echo. Read-only opcodes only - nothing that writes flash.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import hid

VID, PID = 0x373B, 0x119B
RID, BODY = 0x3F, 63

def rx(dev, timeout=400):
    try:
        r = dev.read(BODY + 1, timeout=timeout)
    except hid.HIDException as e:
        return None, f"ERR {e}"
    if not r:
        return None, "(no reply)"
    b = bytes(r)
    return b, b[:20].hex(" ")

def shot(dev, label, body):
    assert len(body) == BODY
    dev.write(bytes([RID]) + body)
    b, s = rx(dev)
    print(f"  {label:<34} -> {s}")
    return b

d = next((x for x in hid.enumerate(VID, PID) if x["usage_page"] == 0xFF00), None)
if not d:
    sys.exit("no 0xFF00 collection found")
print(f"path={d['path'].decode()} usage=0x{d['usage']:02X} iface={d['interface_number']}")

try:
    dev = hid.Device(path=d["path"])
except Exception as e:
    sys.exit(f"still cannot open: {e}\n"
             "If you just granted Input Monitoring, the terminal app usually "
             "has to be fully quit and reopened for it to take effect.")
print("OPENED OK\n")

print("--- framing A: 0xAA header, as used on 0xFF60 ---")
def frame_a(cmd, payload=b"", offset=0, dlen=None):
    b = bytearray(BODY)
    if payload:
        b[0] = 0xAA
        b[2], b[3] = offset & 0xFF, (offset >> 8) & 0xFF
        b[4] = len(payload) if dlen is None else dlen
        b[8:8 + len(payload)] = payload
    b[1] = cmd
    return bytes(b)

a_start = shot(dev, "start 0x10", frame_a(0x10))
a_ctrl  = shot(dev, "CONTROL unused 0x7F", frame_a(0x7F))
a_info  = shot(dev, "get_keyboard_info 0x12", frame_a(0x12))

print("\n--- framing B: command in byte 0 (VIA style) ---")
def frame_b(cmd, *rest):
    b = bytearray(BODY)
    b[0] = cmd
    for i, v in enumerate(rest):
        b[1 + i] = v
    return bytes(b)

b_ver  = shot(dev, "VIA get_protocol_version 0x01", frame_b(0x01))
b_ctrl = shot(dev, "CONTROL unused 0x7F", frame_b(0x7F))
b_kbd  = shot(dev, "VIA get_keyboard_value 0x02", frame_b(0x02, 0x01))

print("\n--- verdict ---")
def cmp(tag, valid, ctrl):
    if valid is None or ctrl is None:
        print(f"  {tag}: inconclusive (missing reply)")
    elif valid == ctrl:
        print(f"  {tag}: valid and control replies IDENTICAL -> not parsing")
    else:
        print(f"  {tag}: valid differs from control -> REAL PARSER HERE")
cmp("framing A", a_start, a_ctrl)
cmp("framing B", b_ver, b_ctrl)
dev.close()
