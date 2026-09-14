import hid

for d in hid.enumerate(0x373B, 0x119B):
    print(f"path={d['path'].decode()}")
    print(f"  usage_page=0x{d['usage_page']:04X} usage=0x{d['usage']:02X} "
          f"iface={d['interface_number']} serial={d.get('serial_number')}")
