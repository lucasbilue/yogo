"""Mirror Hermes' lifecycle onto the ATK Yogo 75 PRO keyboard's 6x6 RGB display.

This plugin deliberately depends on the display's *wire format*, not on the
display's code. It writes one small JSON file per producer into
``~/.yogo/sources/`` and nothing else - no imports from the yogo package, no
shared venv, no PYTHONPATH surgery. A separate daemon owns the USB HID device,
arbitrates between producers, and renders at ~15 fps.

That boundary is the point: Hermes and Claude Code can both drive the same
display without knowing about each other, and neither has to link against the
display driver. If the daemon is not running these writes are simply inert.

States: idle | thinking | tool | waiting | done | error
Arbitration is by state priority, so a Hermes `waiting` will pre-empt another
harness's `thinking` - the attention-demanding state wins.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SOURCE = os.environ.get("YOGO_DISPLAY_SOURCE", "hermes")
_HOME = Path(os.environ.get("YOGO_HOME", os.path.expanduser("~/.yogo")))
_SOURCES = _HOME / "sources"

# Tools that mean "the agent is waiting on the human" rather than "working".
_ASK_TOOLS = {"clarify", "ask_user", "ask", "elicit", "confirm"}

# Long, because a legitimate turn can run for many minutes; the daemon drops a
# signal past its TTL so a crashed Hermes cannot pin the display forever.
_TTL = float(os.environ.get("YOGO_DISPLAY_TTL", "900"))


def _emit(state: str) -> None:
    """Publish a state. Never raises - a display must not break the agent."""
    if os.environ.get("YOGO_DISPLAY_DISABLE"):
        return
    try:
        _SOURCES.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "state": state,
            "ttl": _TTL,
            "pid": os.getpid(),      # daemon drops us instantly if Hermes dies
            "label": SOURCE,
        })
        # Atomic replace, so a reader never sees a partially written file.
        fd, tmp = tempfile.mkstemp(dir=str(_SOURCES), prefix=f".{SOURCE}.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            os.replace(tmp, _SOURCES / f"{SOURCE}.json")
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass                          # display is best-effort, always


# --- Hermes lifecycle hooks ---------------------------------------------
# Keyword-only with **_ so new payload fields are additive, per the plugin
# compatibility contract in plugins/AGENTS.md.

def on_session_start(**_: Any) -> None:
    _emit("idle")


def on_pre_llm_call(**_: Any) -> None:
    _emit("thinking")


def on_post_llm_call(**_: Any) -> None:
    _emit("thinking")


def on_pre_tool_call(*, tool_name: str = "", **_: Any) -> None:
    # A question to the user reads as "waiting", not "working".
    _emit("waiting" if str(tool_name).lower() in _ASK_TOOLS else "tool")


def on_post_tool_call(*, tool_name: str = "", **_: Any) -> None:
    _emit("thinking")


def on_api_request_error(**_: Any) -> None:
    _emit("error")


def on_subagent_start(**_: Any) -> None:
    _emit("tool")


def on_subagent_stop(**_: Any) -> None:
    _emit("thinking")


def on_session_finalize(**_: Any) -> None:
    _emit("done")


def register(ctx) -> None:
    # Both naming variants, mirroring the bundled langfuse plugin, so this
    # keeps working across Hermes versions that differ on the `on_` prefix.
    hooks = (
        ("on_session_start", on_session_start),
        ("pre_llm_call", on_pre_llm_call),
        ("post_llm_call", on_post_llm_call),
        ("pre_tool_call", on_pre_tool_call),
        ("post_tool_call", on_post_tool_call),
        ("api_request_error", on_api_request_error),
        ("subagent_start", on_subagent_start),
        ("subagent_stop", on_subagent_stop),
        ("on_session_finalize", on_session_finalize),
        ("on_session_end", on_session_finalize),
    )
    for name, fn in hooks:
        try:
            ctx.register_hook(name, fn)
        except Exception:
            pass                      # unknown hook on an older core: skip it
