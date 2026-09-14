# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private
vulnerability reporting instead:

https://github.com/rossgpt/yogo/security/advisories/new

You should get an acknowledgement within a week. Once a fix is available it
will be released and the advisory published with credit, unless you prefer
otherwise.

## Scope

Things that count as security issues here:

- Any way to make `protocol.build_packet` emit one of the refused commands
  (key map, macro, LED matrix or bootloader opcodes), since a malformed one
  can brick the keyboard.
- Unsafe handling of the signal bus files under `~/.yogo/`, such as following
  symlinks or acting on another user's files.
- Anything that lets a harness adapter execute code from an untrusted source.

The daemon and adapters run as your own user and only ever talk to a keyboard
over USB HID. They do not open network connections.

## Supported versions

Only the latest commit on `main` is supported.
