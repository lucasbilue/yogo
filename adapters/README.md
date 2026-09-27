# Adapters — plugging a harness into the display

The display is driven by a **signal bus**, not by direct calls. Any harness
that can write a small JSON file can drive it, in about fifteen lines, with no
dependency on this repo.

```
  harness lifecycle                    the bus                    the renderer
  ─────────────────                    ───────                    ────────────
  Claude Code hooks ──┐
  Hermes plugin     ──┼──▶  ~/.yogo/sources/<source>.json  ──▶  yogo daemon
  your CI script    ──┘         (one file per producer)          arbitrate()
                                                                 render 15 fps
                                                                      │
                                                                      ▼
                                                            6x6 dot screen
```

## Why a bus rather than a library

Three constraints, each of which rules out the obvious approach:

- **The HID interface is exclusive-access.** Only one process can hold the
  device, so producers cannot write pixels. Something must own it.
- **A streamed frame is volatile.** The firmware reclaims the screen about a
  second after the last frame, so the owner must keep refreshing.
- **Producers are foreign processes.** Hermes runs in its own venv; a CI job
  may have no Python at all. Anything requiring them to import this package,
  or share a virtualenv, would not survive contact.

So the integration contract is the **file format**, not an API. The Hermes
adapter deliberately does not import `yogo` — it writes the JSON itself.

## The contract

Write `~/.yogo/sources/<source>.json` (override the root with `YOGO_HOME`):

```json
{ "state": "thinking", "ttl": 900, "pid": 4242, "label": "hermes" }
```

| Field | Required | Meaning |
|---|---|---|
| `state` | yes | one of `idle` `thinking` `tool` `waiting` `done` `error` |
| `ttl` | no | seconds before this signal is ignored (default 900) |
| `ts` | no | epoch seconds. **Omit it** and the bus uses the file's mtime, which is sub-second precise — `date +%s` on macOS is not |
| `pid` | no | if given and that process dies, the signal is dropped immediately |
| `label` | no | display name, defaults to the source name |

**Write atomically** (temp file then `rename`), or a reader will occasionally
see a half-written file. One file per producer means no locking and no lost
writes between harnesses.

The shell one-liner version is `bin/yogo-signal <source> <state> [ttl]`.

## Arbitration

Several producers can be active at once, so the daemon picks a winner:

1. Drop signals that are expired, whose `pid` is dead, or whose transient hold
   has elapsed.
2. Take the highest **state priority**:
   `error` 5 › `waiting` 4 › `done` 3 › `tool` 2 › `thinking` 1 › `idle` 0.
3. Break ties on recency.

Priority rather than last-writer-wins because the attention-demanding state is
the useful one: if Hermes needs input while Claude Code is mid-thought, you
want the amber flash, not the cyan orbit. `done` outranks `thinking` so a
completion still gets its moment, then decays after 5 s and lets the ongoing
work resume underneath it.

Animation phase comes from the winning signal's own age, so a bloom is timed
from when the harness reported it, not from when the daemon noticed.

Inspect it with `yogo sources` — `*` marks the winner.

## Liveness

No producer can be trusted to always send a closing signal, so a crashed
harness must not pin the display in `thinking` forever. Two mechanisms:
the `ttl` (coarse, always available) and `pid` liveness (instant, for
long-lived producers like the Hermes plugin). Short-lived producers such as
Claude Code hooks cannot supply a useful pid, which is why the TTL exists.

## Writing a new adapter

An adapter is only a lifecycle→state mapping. The whole job:

| Show | When the harness… |
|---|---|
| `thinking` | started a turn / is calling the model |
| `tool` | is running a tool or subagent |
| `waiting` | needs a human — permission, a question, a confirmation |
| `done` | finished a turn successfully |
| `error` | hit a failure |
| `idle` | started or ended a session |

Rules worth following, both learned the hard way:

- **Never let the display break the harness.** Wrap every emit in a bare
  `except` and swallow it. A keyboard light is not worth an agent crash.
- **Keep it cheap.** `pre_tool_call`-style hooks fire constantly. `yogo-signal`
  is POSIX shell precisely to avoid interpreter start-up (~28 ms vs ~150 ms).
- **Offer a mute switch.** The Hermes plugin honours `YOGO_DISPLAY_DISABLE=1`.

## Shipped adapters

| Harness | Location | Mechanism |
|---|---|---|
| Claude Code | `adapters/claude-code/settings-hooks.json` | 11 `async` hooks in `~/.claude/settings.json` calling `bin/yogo-signal` |
| Hermes | `adapters/hermes/yogo-display/` | Python plugin, symlinked into `~/.hermes/plugins/`, registers 10 lifecycle hooks |

### Claude Code

Merge `settings-hooks.json` into `~/.claude/settings.json`. All hooks are
`async` so the display never delays a turn.

The hook commands locate this repo via `${YOGO_REPO:-$HOME/Yogo}`, so they work
unchanged if you cloned to `~/Yogo`. Otherwise either export `YOGO_REPO` in
your shell profile, or set it in the same settings file so Claude Code passes
it to every hook:

```json
{ "env": { "YOGO_REPO": "/path/to/Yogo" } }
```

### Hermes

```sh
ln -s "$PWD/adapters/hermes/yogo-display" ~/.hermes/plugins/yogo-display
hermes plugins enable yogo-display
```

Decline the tool-override prompt — this plugin registers hooks only, never
tools, so it does not need that privilege. It maps `clarify`-style tools to
`waiting` rather than `tool`, since a question to the user is not work.

## Synapse (read-only sidecar)

`adapters/synapse/yogo_synapse.py` mirrors Synapse work-item status onto the
display without touching Synapse at all: it polls the local dashboard API
(`GET http://127.0.0.1:3000/api/work`) and publishes on the bus.

```
./bin/yogo-synapse --dump   # show what it sees and would display; changes nothing
./bin/yogo-synapse -v       # run it (Ctrl-C to stop)
```

| Synapse work status | Display |
|---|---|
| `assigned`, `starting`, `working` | thinking |
| `waiting`, `blocked` (`--no-blocked` to skip) | waiting |
| newly `done` | done |
| newly `failed` | error |
| anything else | ignored |

The most urgent item wins. Items already finished when the sidecar starts are
ignored, and every signal carries the sidecar's pid, so stopping it clears
the display. `--url` or `SYNAPSE_WORK_URL` points it at another address.

## Claude in Chrome (claude.ai)

`adapters/claude-web/` lights the display while Claude replies in an ordinary
claude.ai chat in Chrome. It shows cyan `thinking` while a reply streams and
green `done` when it finishes. There is no amber `waiting`, because a chat has
no permission prompts to wait on.

It has two parts. A Chrome extension watches each claude.ai tab, and a local
listener publishes what it reports on the bus as `claude-web-<tab>`:

```
claude.ai tab ──▶ content.js ──▶ background.js ──POST──▶ bin/yogo-web ──▶ bus
               polls every 500 ms   adds the tab id      127.0.0.1:7437
```

Start the listener, then load the extension once:

```
./bin/yogo-web              # Ctrl-C to stop; -v logs each signal, --port to move it
```

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and pick `adapters/claude-web/extension`.

The extension counts a tab as replying when the page has an element with
`[data-is-streaming="true"]` or shows a visible button whose `aria-label`
contains "Stop". While a reply runs it re-sends `thinking` every 60 s, so the
120 s TTL doesn't expire on long answers. Closing a tab clears its source, and
stopping the listener drops every signal it wrote, since they all carry its
pid.

The listener only accepts `POST /signal` requests that carry `X-Yogo: 1` and
have no `Origin` header, or a `chrome-extension://` one. Any web page can send
a request to localhost, and this check stops a page from faking a signal.

Limits:

- **It reads the page.** A claude.ai redesign can break detection until the
  selectors are updated. They're in one `SELECTORS` constant at the top of
  `extension/content.js`.
- **Chrome only, not the Claude desktop app.** The desktop app can't load
  extensions.
