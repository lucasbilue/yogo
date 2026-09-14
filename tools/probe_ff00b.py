"""Is 0xFF00 emitting unsolicited reports? Read first, write nothing."""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import hid

VID, PID, RID, BODY = 0x373B, 0x119B, 0x3F, 63
d = next(x for x in hid.enumerate(VID, PID) if x["usage_page"] == 0xFF00)
dev = hid.Device(path=d["path"])

print("=== A. PASSIVE: reading 2s with zero writes ===")
seen, t_end = [], time.time() + 2.0
while time.time() < t_end:
    r = dev.read(BODY + 1, timeout=200)
    if r:
        seen.append(bytes(r))
print(f"  {len(seen)} unsolicited report(s) with no command sent")
for s in seen[:3]:
    print(f"    {s[:20].hex(' ')}")
if seen:
    print("  -> the device volunteers this on its own; earlier 'replies' were these")

def drain(dev):
    n = 0
    while dev.read(BODY + 1, timeout=60):
        n += 1
    return n

print("\n=== B. DRAIN, then send, then read ===")
def frame_a(cmd):
    b = bytearray(BODY); b[1] = cmd; return bytes(b)

for label, cmd in [("start 0x10", 0x10), ("CONTROL 0x7F", 0x7F), ("info 0x12", 0x12)]:
    flushed = drain(dev)
    dev.write(bytes([RID]) + frame_a(cmd))
    r = dev.read(BODY + 1, timeout=500)
    got = bytes(r)[:20].hex(" ") if r else "(no reply)"
    print(f"  flushed {flushed:>2}  {label:<14} -> {got}")

dev.close()
