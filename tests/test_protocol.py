import pytest

from yogo import protocol as p


def test_rgb565_round_trip_extremes():
    assert p.rgb_to_565(0, 0, 0) == 0x0000
    assert p.rgb_to_565(255, 255, 255) == 0xFFFF
    assert p.rgb565_to_rgb(0xFFFF) == (255, 255, 255)
    assert p.rgb565_to_rgb(0x0000) == (0, 0, 0)


def test_rgb565_channel_positions():
    assert p.rgb_to_565(255, 0, 0) == 0xF800
    assert p.rgb_to_565(0, 255, 0) == 0x07E0
    assert p.rgb_to_565(0, 0, 255) == 0x001F


def test_build_packet_layout():
    pkt = p.build_packet(p.CMD_SET_DOTSCREEN_MATRIX, b"\x01\x02\x03", offset=0x38, seq=0x6C)
    assert len(pkt) == p.WIRED_BUFFER_LEN
    assert pkt[0] == p.MAGIC
    assert pkt[p.BASE_OFFSET] == p.CMD_SET_DOTSCREEN_MATRIX
    assert pkt[2:4] == b"\x38\x00"  # little-endian offset
    assert pkt[4] == 3
    assert pkt[p.SEQ_OFFSET] == 0x6C
    assert pkt[p.STATUS_OFFSET] == 0
    assert pkt[p.DATA_OFFSET : p.DATA_OFFSET + 3] == b"\x01\x02\x03"
    assert not any(pkt[p.DATA_OFFSET + 3 :])


def test_build_packet_bare_command_keeps_magic():
    pkt = p.build_packet(p.CMD_START, seq=0x6A)
    assert pkt[0] == p.MAGIC
    assert pkt[4] == 0
    assert pkt[p.SEQ_OFFSET] == 0x6A


def test_build_packet_declared_length_overrides_actual():
    # Chunk 1 of a persistent write carries 52 bytes but still declares 56.
    pkt = p.build_packet(p.CMD_SET_DOTSCREEN_MATRIX, bytes(52), offset=56, data_len=56)
    assert pkt[4] == 56


def test_build_packet_dongle_geometry():
    pkt = p.build_packet(p.CMD_START, buffer_len=p.DONGLE_BUFFER_LEN)
    assert len(pkt) == p.DONGLE_BUFFER_LEN


@pytest.mark.parametrize("cmd", sorted(p.UNSAFE_COMMANDS))
def test_build_packet_refuses_unsafe_commands(cmd):
    with pytest.raises(ValueError, match="unsafe"):
        p.build_packet(cmd)


def test_build_packet_rejects_oversized_payload():
    with pytest.raises(ValueError, match="exceeds"):
        p.build_packet(p.CMD_START, bytes(p.WIRED_DATA_LEN + 1))


def test_frame_to_565_bytes_is_little_endian_row_major():
    pixels = [(0, 0, 0)] * p.PIXELS
    pixels[7] = (255, 0, 0)  # row 1, col 1
    out = p.frame_to_565_bytes(pixels)
    assert len(out) == p.FRAME_BYTES_565
    assert out[14:16] == b"\x00\xf8"
    assert not any(out[:14]) and not any(out[16:])


def test_frame_to_888_bytes():
    pixels = [(1, 2, 3)] * p.PIXELS
    out = p.frame_to_888_bytes(pixels)
    assert len(out) == p.FRAME_BYTES_888
    assert out[:3] == b"\x01\x02\x03"


@pytest.mark.parametrize("fn", [p.frame_to_565_bytes, p.frame_to_888_bytes])
def test_frame_encoders_require_exactly_36_pixels(fn):
    with pytest.raises(ValueError, match="expected 36"):
        fn([(0, 0, 0)] * 35)
