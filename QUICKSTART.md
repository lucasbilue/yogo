# Yogo pixel display: quickstart

Commands to get the keyboard's 6×6 display showing Claude Code, Synapse and claude.ai
activity. Everything runs from the repo:

```bash
cd /Users/zipcode/Documents/atk/yogo
```

Nothing starts by itself yet, so after a restart run steps 1 and 2 again.

## 1. Start the renderer (always needed)

The daemon owns the keyboard and draws whatever the producers report. It detaches, so
you can close the terminal afterwards.

```bash
./bin/yogo daemon --idle breathe
```

Use `--idle off` for a dark screen when nothing is happening, or `--idle firmware` to
let the keyboard's own animation play.

## 2. Start the Synapse sidecar

It mirrors Synapse work items and desk agents onto the display. It's read-only and
never changes Synapse.

**Survives closing the terminal** (recommended). Its output goes to a log file:

```bash
nohup ./bin/yogo-synapse -v > ~/.yogo/synapse.log 2>&1 &
```

**Or run it in the foreground** to watch it, and press Ctrl-C to stop:

```bash
./bin/yogo-synapse -v
```

It's fine to start it before Synapse. It waits, retries, and lights up once
`http://127.0.0.1:3000` answers.

| Synapse | Display |
|---|---|
| a desk agent or work item is `working` | cyan orbit |
| an agent or item becomes `complete` / `done` | green bloom, for ~5 s |
| something is `waiting` / `blocked` on you | amber flash |
| an item `failed` | red double-flash |
| all idle | idle (breathe, off or firmware) |

## 3. Optional: claude.ai in Chrome

```bash
./bin/yogo-web
```

Then, once only: open `chrome://extensions`, turn on **Developer mode**, click
**Load unpacked**, and pick `adapters/claude-web/extension`. Cyan while Claude replies,
green when it's done.

Claude Code sessions need nothing extra: the hooks in `~/.claude/settings.json`
signal the display by themselves.

## Check what's happening

```bash
./bin/yogo daemon --status
```

```bash
./bin/yogo sources
```

`sources` lists every producer; `*` marks the one on screen. The most urgent state
wins: error › waiting › done › tool › thinking › idle. So a Claude Code session waiting
on you (amber) will hide Synapse's cyan until you answer it.

```bash
./bin/yogo-synapse --dump
```

That shows what the sidecar sees in Synapse right now, and changes nothing.

```bash
tail -f ~/.yogo/synapse.log
```

That follows the background sidecar's log.

## Stop

```bash
pkill -f yogo_synapse.py
```

```bash
pkill -f claude-web/listener.py
```

```bash
./bin/yogo daemon --stop
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Display dark | `./bin/yogo daemon --status`. If it isn't running, redo step 1 |
| Synapse not showing | `pgrep -fl yogo_synapse`. If there's no process, redo step 2 |
| Sidecar running, still nothing | `./bin/yogo-synapse --dump`. Is any agent `working`? |
| Stuck on amber | `./bin/yogo sources`. Usually a Claude Code session waiting for your reply |
| Stuck on a state | `./bin/yogo daemon --stop`, then start it again |
