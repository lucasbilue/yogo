import json
import os
import time

import pytest

from yogo import bus


@pytest.fixture
def sources(tmp_path, monkeypatch):
    d = tmp_path / "sources"
    monkeypatch.setattr(bus, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(bus, "SOURCES_DIR", d)
    return d


def sig(state, ts, **kw):
    return bus.Signal(source=kw.pop("source", state), state=state, ts=ts, **kw)


def test_normalise_source():
    assert bus.normalise_source("Claude Code") == "claude-code"
    assert bus.normalise_source("  my_ci.job  ") == "my_ci-job"
    assert bus.normalise_source("") == "unknown"
    assert bus.normalise_source("---") == "unknown"


def test_arbitrate_idle_when_nothing_live():
    assert bus.arbitrate([], now=100.0) == ("idle", 0.0, None)
    assert bus.arbitrate([sig("idle", 99.0)], now=100.0)[0] == "idle"


def test_arbitrate_priority_beats_recency():
    now = 100.0
    waiting = sig("waiting", now - 30)
    thinking = sig("thinking", now - 1)
    state, elapsed, win = bus.arbitrate([thinking, waiting], now=now)
    assert state == "waiting"
    assert win is waiting
    assert elapsed == pytest.approx(30.0)


def test_arbitrate_ties_break_on_recency():
    now = 100.0
    older = sig("thinking", now - 10, source="a")
    newer = sig("thinking", now - 2, source="b")
    assert bus.arbitrate([older, newer], now=now)[2] is newer


def test_arbitrate_full_priority_order():
    now = 100.0
    order = ["thinking", "tool", "done", "waiting", "error"]
    live = [sig(s, now) for s in order]
    for expected in reversed(order):
        assert bus.arbitrate(live, now=now)[0] == expected
        live = [s for s in live if s.state != expected]


def test_ttl_expiry():
    s = sig("thinking", 0.0, ttl=60.0)
    assert not s.expired(now=59.0)
    assert s.expired(now=61.0)


def test_transient_hold_lets_underlying_state_resume():
    now = 100.0
    done = sig("done", now - 6, source="a")
    thinking = sig("thinking", now - 60, source="b")
    assert bus.arbitrate([done, thinking], now=now - 3)[0] == "done"
    assert bus.arbitrate([done, thinking], now=now)[0] == "thinking"


def test_dead_pid_expires_signal():
    # Find a pid that certainly is not running.
    pid = 2**22 - 1
    while bus._pid_alive(pid):
        pid -= 1
    assert sig("thinking", time.time(), pid=pid).expired()
    assert not sig("thinking", time.time(), pid=os.getpid()).expired()


def test_emit_and_read_all_round_trip(sources):
    s = bus.emit("Claude Code", "tool", ttl=30, pid=os.getpid(), label="cc")
    assert s.source == "claude-code"
    files = list(sources.glob("*.json"))
    assert [f.name for f in files] == ["claude-code.json"]
    assert not list(sources.glob(".*.tmp")), "temp file must be renamed away"

    [got] = bus.read_all()
    assert (got.source, got.state, got.ttl, got.pid, got.label) == (
        "claude-code", "tool", 30.0, os.getpid(), "cc")


def test_emit_rejects_unknown_state(sources):
    with pytest.raises(ValueError, match="unknown state"):
        bus.emit("x", "sleeping")


def test_read_all_falls_back_to_mtime_when_ts_omitted(sources):
    sources.mkdir()
    f = sources / "sh.json"
    f.write_text(json.dumps({"state": "waiting", "ttl": 900}))
    [got] = bus.read_all()
    assert got.ts == pytest.approx(f.stat().st_mtime)
    assert got.label == "sh"


def test_read_all_skips_corrupt_and_unknown(sources):
    sources.mkdir()
    (sources / "bad.json").write_text("{not json")
    (sources / "odd.json").write_text(json.dumps({"state": "napping"}))
    (sources / "ok.json").write_text(json.dumps({"state": "error"}))
    assert [s.source for s in bus.read_all()] == ["ok"]


def test_clear_is_idempotent(sources):
    bus.emit("a", "thinking")
    bus.clear("a")
    bus.clear("a")
    assert bus.read_all() == []
