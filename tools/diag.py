"""Read what the keyboard actually replies to each command.

Includes an unused opcode as a control, so we can tell a real ACK from a
"don't know that command" reply.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import hid

from yogo import YogoDisplay
from yogo import protocol as p

STATUS = p.STATUS

def drain(dev, label, timeout=400):
    """Collect every report the device volunteers, not just the first."""
    got = []
    while True:
        try:
            rx = dev._dev.read(dev.buffer_len, timeout=timeout)
        except hid.HIDException as e:
            print(f"    read error: {e}")
            break
        if not rx:
            break
        got.append(bytes(rx))
        timeout = 120          # first reply arrived; poll briefly for more
    if not got:
        print(f"    {label}: (no reply)")
    for i, rx in enumerate(got):
        head = rx[:16].hex(" ")
        tail_nz = [f"{j}=0x{v:02X}" for j, v in enumerate(rx) if v and j >= 16]
        print(f"    {label}[{i}]: {head}")
        print(f"        cmd_echo=0x{rx[1]:02X}  byte7=0x{rx[7]:02X}"
              f"  status?={STATUS.get(rx[7], '?')}")
        if tail_nz:
            print(f"        nonzero past byte16: {' '.join(tail_nz[:12])}")
    return got

def send(dev, name, cmd, payload=b"", offset=0, data_len=None):
    pkt = p.build_packet(cmd, payload, offset, data_len, dev.buffer_len)
    print(f"\n>>> {name}  (cmd 0x{cmd:02X})")
    print(f"    TX: {pkt[:16].hex(' ')}")
    dev._dev.write(b"\x00" + pkt)
    return drain(dev, "RX")

dev = YogoDisplay.open_first()
print(f"{dev!r}")
with dev:
    send(dev, "start / correspondence", p.CMD_START)
    send(dev, "get_keyboard_info", 0x12, data_len=56)
    send(dev, "power_info", p.CMD_POWER_INFO)
    send(dev, "get_led_matrix (read-only)", 0x32, data_len=56)
    send(dev, "CONTROL: unused opcode 0x7F", 0x7F)

    print("\n--- now the two display paths ---")
    send(dev, "start (refresh session)", p.CMD_START)
    img = p.frame_to_888_bytes([(255, 0, 0)] * p.PIXELS)
    send(dev, "set_dotscreen_matrix chunk0", p.CMD_SET_DOTSCREEN_MATRIX,
         img[:56], 0, 56)
    send(dev, "set_dotscreen_matrix chunk1", p.CMD_SET_DOTSCREEN_MATRIX,
         img[56:108], 56, 56)

    frm = p.frame_to_565_bytes([(0, 0, 255)] * p.PIXELS)
    send(dev, "start_sync_dot chunk0", p.CMD_START_SYNC_DOT, frm[:56], 0, 56)
    send(dev, "start_sync_dot chunk1", p.CMD_START_SYNC_DOT, frm[56:72], 56, 16)
