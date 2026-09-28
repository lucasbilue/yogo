"""Render functions are pure: seconds in, frame out. No device needed."""

from yogo import daemon


def halves(frame):
    left = {frame.pixels[y * 6 + x] for x in range(3) for y in range(6)}
    right = {frame.pixels[y * 6 + x] for x in range(3, 6) for y in range(6)}
    return left, right


def test_waiting_alternates_left_and_right():
    [l1], [r1] = halves(daemon.render_waiting(0.0))
    [l2], [r2] = halves(daemon.render_waiting(0.25))
    assert sum(l1) > sum(r1)                 # left half lit first
    assert sum(r2) > sum(l2)                 # then the right
    assert (l1, r1) == (r2, l2)              # same two levels, swapped


def test_waiting_never_flashes_full_brightness():
    for i in range(40):
        for px in daemon.render_waiting(i * 0.05).pixels:
            assert max(px) <= 0.4 * 255
