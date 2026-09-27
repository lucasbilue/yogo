"""The claude.ai listener: request gating, state mapping and cleanup."""

import http.client
import importlib.util
import json
import os
import threading
from pathlib import Path

import pytest

from yogo import bus

_spec = importlib.util.spec_from_file_location(
    "yogo_web_listener", Path(__file__).parents[1] / "adapters/claude-web/listener.py")
lw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lw)

OK_HEADERS = {"X-Yogo": "1", "Content-Type": "application/json"}


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setattr(bus, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(bus, "SOURCES_DIR", tmp_path / "sources")
    return tmp_path / "sources"


@pytest.fixture
def server(sources):
    srv = lw.Listener(("127.0.0.1", 0))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    conn.request(method, path, body=data, headers=headers or {})
    r = conn.getresponse()
    out = r.status, json.loads(r.read() or b"null")
    conn.close()
    return out


def signal(srv, tab, state, **headers):
    return request(srv, "POST", "/signal", {"tab": tab, "state": state},
                   {**OK_HEADERS, **headers})[0]


def read(sources, tab):
    return json.loads((sources / f"claude-web-{tab}.json").read_text())


def test_health(server):
    assert request(server, "GET", "/health") == (200, {"ok": True})


def test_thinking_then_done(server, sources):
    assert signal(server, 7, "thinking") == 200
    d = read(sources, 7)
    assert (d["state"], d["ttl"], d["pid"]) == ("thinking", 120, os.getpid())

    assert signal(server, 7, "done") == 200
    d = read(sources, 7)
    assert (d["state"], d["ttl"]) == ("done", 60)


def test_idle_uses_short_ttl(server, sources):
    assert signal(server, 3, "idle") == 200
    assert read(sources, 3)["ttl"] == 60


def test_clear_removes_source(server, sources):
    signal(server, 7, "thinking")
    signal(server, 8, "thinking")
    assert signal(server, 7, "clear") == 200
    assert sorted(f.name for f in sources.glob("*.json")) == ["claude-web-8.json"]
    assert server.sources == {"claude-web-8"}


def test_missing_header_is_forbidden(server, sources):
    status, _ = request(server, "POST", "/signal", {"tab": 1, "state": "thinking"},
                        {"Content-Type": "application/json"})
    assert status == 403
    assert not sources.exists() or not list(sources.glob("*.json"))


def test_wrong_header_value_is_forbidden(server):
    assert signal(server, 1, "thinking", **{"X-Yogo": "yes"}) == 403


def test_web_origin_is_forbidden(server, sources):
    assert signal(server, 1, "thinking", Origin="https://evil.example") == 403
    assert signal(server, 1, "thinking", Origin="https://claude.ai") == 403
    assert not sources.exists() or not list(sources.glob("*.json"))


def test_extension_origin_is_allowed(server, sources):
    assert signal(server, 1, "thinking", Origin="chrome-extension://abc") == 200
    assert read(sources, 1)["state"] == "thinking"


@pytest.mark.parametrize("body", [
    {"tab": 1, "state": "napping"},
    {"tab": 1, "state": "error"},          # a real bus state, but not one this adapter sends
    {"tab": "1", "state": "thinking"},
    {"tab": True, "state": "thinking"},
    {"tab": -1, "state": "thinking"},
    {"state": "thinking"},
    [1, "thinking"],
    b"{not json",
])
def test_bad_request_is_400(server, sources, body):
    assert request(server, "POST", "/signal", body, OK_HEADERS)[0] == 400
    assert not sources.exists() or not list(sources.glob("*.json"))


def test_unknown_path_is_404(server):
    assert request(server, "POST", "/other", {"tab": 1, "state": "done"}, OK_HEADERS)[0] == 404


def test_shutdown_clears_its_sources(server, sources):
    bus.emit("claude-code", "thinking")        # someone else's; must survive
    signal(server, 1, "thinking")
    signal(server, 2, "done")
    server.clear_all()
    assert [f.name for f in sources.glob("*.json")] == ["claude-code.json"]


def test_sigterm_clears_its_sources(tmp_path):
    """The real process: start it, signal through it, SIGTERM it, files gone."""
    import signal as sig
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "YOGO_HOME": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, _spec.origin, "--port", str(port)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("POST", "/signal", json.dumps({"tab": 5, "state": "thinking"}),
                             OK_HEADERS)
                assert conn.getresponse().status == 200
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail(f"listener never came up: {proc.stderr.read()!r}")
        assert (tmp_path / "sources/claude-web-5.json").exists()

        proc.send_signal(sig.SIGTERM)
        assert proc.wait(timeout=5) == 0
        assert not (tmp_path / "sources/claude-web-5.json").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
