"""The demo cycles every state in bus order, whichever route it takes."""

from yogo import bus, cli
from yogo import daemon as dmn


def test_demo_over_bus_emits_every_state_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(bus, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(bus, "SOURCES_DIR", tmp_path / "sources")
    monkeypatch.setattr(dmn, "daemon_pid", lambda: 4242)      # pretend it runs
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    seen = []
    real_emit = bus.emit

    def spy(source, state, **kw):
        seen.append((source, state))
        return real_emit(source, state, **kw)

    monkeypatch.setattr(bus, "emit", spy)

    assert cli.main(["-q", "demo"]) == 0
    assert seen == [("demo", s) for s in bus.STATES]
    assert bus.STATES[0] == "idle" and bus.STATES[-1] == "error"
    assert bus.read_all() == [], "the demo must clear its own signal on exit"


def test_demo_over_bus_honours_explicit_states(monkeypatch, tmp_path):
    monkeypatch.setattr(bus, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(bus, "SOURCES_DIR", tmp_path / "sources")
    monkeypatch.setattr(dmn, "daemon_pid", lambda: 4242)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    seen = []
    monkeypatch.setattr(bus, "emit", lambda src, st, **kw: seen.append(st))
    monkeypatch.setattr(bus, "arbitrate", lambda *a, **k: ("idle", 0.0, None))

    assert cli.main(["-q", "demo", "waiting", "done", "--hold", "0"]) == 0
    assert seen == ["waiting", "done"]


def test_demo_rejects_unknown_state(monkeypatch, capsys):
    monkeypatch.setattr(dmn, "daemon_pid", lambda: 4242)
    assert cli.main(["-q", "demo", "waiting", "napping"]) == 2
    assert "napping" in capsys.readouterr().err


def test_demo_direct_uses_daemon_renderers(monkeypatch):
    """Without a daemon it must paint with the very same renderers."""
    monkeypatch.setattr(dmn, "daemon_pid", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    class FakeDev:
        frames = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def show(self, pixels):
            self.frames.append(pixels)

    monkeypatch.setattr(cli, "_open", lambda args, need_custom_mode=False: FakeDev())
    called = []
    for name, fn in list(dmn.RENDERERS.items()):
        monkeypatch.setitem(dmn.RENDERERS, name,
                            lambda t, _n=name, _f=fn: (called.append(_n), _f(t))[1])

    assert cli.main(["-q", "demo", "--hold", "0.05", "--fps", "200"]) == 0
    assert [s for s in bus.STATES if s != "idle"] == list(dict.fromkeys(called))
    assert FakeDev.frames and FakeDev.frames[-1] == [(0, 0, 0)] * 36
