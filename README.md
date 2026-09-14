# yogo — drive the 6×6 RGB display on an ATK Yogo 75 PRO

[![CI](https://github.com/rossgpt/yogo/actions/workflows/ci.yml/badge.svg)](https://github.com/rossgpt/yogo/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

A small Python library and CLI for the pixel display on the ATK Yogo 75 PRO
keyboard, talking to it directly over USB HID. No ATK HUB required.

The wire format was worked out and verified against the hardware, and is
documented in full below. Tested only on the Yogo 75 PRO.

## Install

```sh
brew install hidapi
python3 -m venv .venv && ./.venv/bin/pip install hid Pillow psutil
```

## Use

```sh
./bin/yogo info                     # device, screen mode, battery
./bin/yogo solid '#ff0055'          # fill with a colour
./bin/yogo image ~/pic.png          # downscale any image to 6×6
./bin/yogo pixels red green blue …  # all 36 pixels, row-major from top-left
./bin/yogo text 'RossGPT'           # spell it out, a letter at a time
./bin/yogo rainbow                  # animated hue sweep
./bin/yogo cpu                      # live scrolling CPU meter
./bin/yogo clear
```

Useful flags: `--stream` (volatile still image, no flash write — it fades after
about a second), `--brightness 0.3`, `-q` (no terminal preview).

```python
from yogo import YogoDisplay
from yogo.frame import Frame

with YogoDisplay.open_first() as dev:
    dev.set_screen_mode()               # Custom, else a built-in effect wins
    f = Frame.solid((0, 0, 0))
    f[0, 0] = (255, 255, 255)           # x, y from top-left
    dev.show(f.pixels)                  # volatile
    dev.write_image(f.pixels)           # persistent (writes flash)
```

## Harness integration

The display is driven by a **signal bus** so more than one agent harness can
share it. Producers write a small JSON file; a daemon owns the USB device,
arbitrates between producers, and renders at ~15 fps.

Working adapters: **Claude Code** (hooks) and **Hermes** (plugin). The full
contract, arbitration rules and a guide to writing another one are in
[`adapters/README.md`](adapters/README.md).

```sh
./bin/yogo daemon --idle breathe    # start the renderer (detaches)
./bin/yogo daemon --idle firmware   # …or let the keyboard's own animation play when idle
./bin/yogo daemon --status
./bin/yogo sources                  # who is signalling; * marks the winner
./bin/yogo-signal my-script done    # signal from anything
```

| State | Look | Meaning |
|---|---|---|
| `thinking` | cyan comet orbiting the edge | working / calling the model |
| `tool` | violet, faster orbit | running a tool or subagent |
| `waiting` | amber flashing, ~2 Hz | needs a human |
| `done` | green bloom, then fading glow | finished |
| `error` | red double-flash | failed |
| `idle` | off, a dim blue breathe, or the keyboard's built-in animation (`--idle firmware`) | between sessions |

When producers disagree, the highest priority wins —
`error` › `waiting` › `done` › `tool` › `thinking` › `idle` — so an agent
needing input pre-empts another one merely thinking. `done` and `error` decay
after 5 s. A signal is dropped if its TTL lapses or its declared pid dies, so
a crashed harness cannot pin the display.

Three things worth knowing:

- **The daemon holds the display**, so one-shot commands (`solid`, `image`,
  `text`) refuse while it runs. Stop it, or use `yogo state`.
- **Detaching must `exec`, never `fork`.** hidapi goes through
  IOKit/CoreFoundation, which macOS forbids using after a `fork()` without an
  `exec()`. A forked daemon dies instantly *and* raises a crash dialog each
  time.
- **`yogo.bus` and `yogo.protocol` are pure stdlib** and import without
  hidapi, so an adapter can use them without the driver. The package `__init__`
  is lazy for exactly this reason.

Runtime state lives in `~/.yogo/` (`sources/`, `daemon.pid`, `daemon.log`);
`YOGO_HOME` overrides it.

## Two ways to write

| | command | encoding | flash | use for |
|---|---|---|---|---|
| **Stream** | `0x3C` | RGB565-LE, 72 B | no | animation, live data, ~15 fps |
| **Persist** | `0x3B` | RGB888, 108 B | **yes** | an image that survives unplugging |

A **streamed frame is volatile** — the firmware reclaims the screen after about
a second unless something keeps refreshing it. So:

- *Still image* → persistent path. This is the default for `solid`, `image`,
  `pixels` and `clear`, and it is the only way a picture stays up on its own.
- *Animation* → streaming, refreshed continuously. `text`, `rainbow` and `cpu`
  do this and perform **no flash writes at all**.

Never drive an animation through `0x3B`; it would wear the flash out.

## Protocol

**Transport.** USB HID, VID `0x373B`, PID `0x119B` (`0x11FF` for the 2.4 GHz
dongle, which is untested). Usage page `0xFF60`, usage `0x61` — the QMK `raw_hid` descriptor,
though the firmware is not QMK. Unnumbered 64-byte reports (prepend a `0x00`
report-id byte for hidapi). Over the dongle the report is 32 bytes with a
24-byte payload.

**Packet layout** (64-byte wired report):

```
off  size  field
 0    1    0xAA   magic — required on EVERY packet, including bare commands
 1    1    command id
 2    2    payload offset, little endian
 4    1    payload length (0x38 = 56)
 5    1    sequence counter, increments per packet
 6    1    reserved, zero
 7    1    status: 0x00 outbound; device replies 0x55 = OK, 0x0F = rejected
 8   56    payload
```

The device answers every command by echoing the packet with byte 7 set. Omit
the magic or leave the sequence at zero and it replies `0x0F` — which looks
convincingly like a dumb echo endpoint if you aren't checking byte 7.

**Commands used here**

| id | name | notes |
|---|---|---|
| `0x10` | `start` | opens a session; lapses after ~5 s, so refresh it |
| `0x14` | `get_keyboard_function` | read 56 bytes of config |
| `0x15` | `set_keyboard_function` | write 64 bytes back, chunked 56 + 8 |
| `0x30` | `power_info` | payload[0] = battery %, payload[1] = 2 when charging |
| `0x3B` | `set_dotscreen_matrix` | persistent image, 108 B RGB888 |
| `0x3C` | `start_sync_dot` | volatile frame, 72 B RGB565-LE |
| `0x3D` | `end_sync_dot` | hands the screen back to the firmware |

**Screen mode.** Config byte 30 selects what owns the display: `0x06` is
Custom (shows the `0x3B` image), other values are built-in effects. Always
read-modify-write the config — never synthesise one.

**Geometry.** 6×6 = 36 pixels, row-major from the **top-left**, so
`index = row * 6 + col`.

**Writing an image**

```
TX  aa 10 00 00 00 6a 00 00                     start
TX  aa 3b 00 00 38 6b 00 00  <56 bytes>         chunk 0
TX  aa 3b 38 00 38 6c 00 00  <52 bytes>         chunk 1 (length still says 56)
```

Streaming is the same shape with `0x3C` and a 72-byte RGB565 frame, split
56 + 16. No commit step is needed for either.

## Gotchas

- **Quit ATK HUB first.** It claims the HID interface exclusively.
- **Don't send `0x3D` after a persistent write** — the firmware immediately
  resumes its own animation and your image never appears.
- The display only shows your image while the mode byte is Custom.
- Commands that rewrite key maps, macros or LED matrices, and the bootloader
  opcodes (`0xA0`–`0xA4`, `0xC0`), are deliberately refused by
  `protocol.build_packet`; a malformed one can brick the keyboard.

## Contributing

Bug reports and new harness adapters are welcome. See
[`CONTRIBUTING.md`](CONTRIBUTING.md), and please report security problems
privately as described in [`SECURITY.md`](SECURITY.md).

## License

[MIT](LICENSE).
