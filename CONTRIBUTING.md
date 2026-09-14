# Contributing

Thanks for your interest. This is a small project, so the process is light.

## Setup

```sh
brew install hidapi
python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'
./.venv/bin/pytest
./.venv/bin/ruff check .
```

The tests cover `yogo.bus` and `yogo.protocol`, which are pure stdlib and need
no hardware. Anything in `yogo/device.py`, the CLI or the daemon's rendering
can only be verified against a real keyboard, so say in your PR which model
and connection you tested on.

## Guidelines

- **Never add a command to `protocol.build_packet` that rewrites key maps,
  macros, LED matrices or touches the bootloader.** A malformed one can brick
  the keyboard. See `UNSAFE_COMMANDS`.
- **Animations stream (`0x3C`), still images persist (`0x3B`).** Do not drive
  anything continuous through the persistent path; it writes flash.
- **Adapters must never break their harness.** Swallow every error and keep
  the hot path cheap. The rules are in [`adapters/README.md`](adapters/README.md).
- Keep the protocol notes in the README accurate. If you discover something
  new about the wire format, document it there with how you verified it.

## Adding a harness adapter

An adapter is a mapping from harness lifecycle events to the six bus states.
The contract, arbitration rules and a checklist are in
[`adapters/README.md`](adapters/README.md). Please add it under
`adapters/<harness>/` and list it in the "Shipped adapters" table.

## Pull requests

Open an issue first for anything larger than a fix, so we can agree on the
approach. Keep PRs focused, fill in the template, and make sure CI is green.
