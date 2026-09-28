"""The Synapse sidecar: status mapping, and a run against a fake /api/work."""

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from yogo import bus

_spec = importlib.util.spec_from_file_location(
    "yogo_synapse", Path(__file__).parents[1] / "adapters/synapse/yogo_synapse.py")
ys = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ys)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setattr(bus, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(bus, "SOURCES_DIR", tmp_path / "sources")
    return tmp_path / "sources"


@pytest.mark.parametrize("payload", [
    [{"id": "WORK-1", "status": "working"}],
    {"items": [{"id": "WORK-1", "status": "working"}]},
    {"data": {"work": [{"id": "WORK-1", "status": "working", "plan": {"id": "PLAN-1",
                                                                     "status": "running"}}]}},
])
def test_work_items_any_shape(payload):
    assert ys.work_items(payload) == {"WORK-1": "working"}


def test_ongoing_state_priority():
    assert ys.ongoing_state({}) == "idle"
    assert ys.ongoing_state({"W1": "queued", "W2": "cancelled"}) == "idle"
    assert ys.ongoing_state({"W1": "working", "W2": "queued"}) == "thinking"
    assert ys.ongoing_state({"W1": "working", "W2": "waiting"}) == "waiting"
    assert ys.ongoing_state({"W1": "working", "W2": "blocked"}) == "waiting"
    assert ys.ongoing_state({"W1": "working", "W2": "blocked"}, blocked=False) == "thinking"


def test_finished_since_only_counts_transitions():
    assert ys.finished_since({"W1": "done"}, {"W1": "done"}) is None
    assert ys.finished_since({"W1": "working"}, {"W1": "done"}) == "done"
    assert ys.finished_since({}, {"W1": "failed"}) == "error"
    assert ys.finished_since({"W1": "working", "W2": "working"},
                             {"W1": "done", "W2": "failed"}) == "error"


def test_run_against_fake_synapse(sources):
    responses = [
        {"items": [{"id": "WORK-OLD", "status": "done"},        # finished before start
                   {"id": "WORK-A", "status": "working"}]},
        {"items": [{"id": "WORK-OLD", "status": "done"},
                   {"id": "WORK-A", "status": "waiting"}]},
        {"items": [{"id": "WORK-OLD", "status": "done"},
                   {"id": "WORK-A", "status": "done"}]},
    ]
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(responses[min(len(seen), len(responses) - 1)]).encode()
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/work"

    history = []
    real_emit = bus.emit

    def spy(source, state, **kw):
        history.append((source, state))
        return real_emit(source, state, **kw)

    ys.bus.emit = spy
    stop = threading.Event()
    real_sleep = time.sleep

    def fake_sleep(_s):
        if len(seen) >= len(responses):
            stop.set()
            raise KeyboardInterrupt
        real_sleep(0.01)

    ys.time.sleep = fake_sleep
    try:
        ys.run(url, interval=0.01, blocked=True, verbose=False)
    finally:
        ys.bus.emit = real_emit
        ys.time.sleep = real_sleep
        srv.shutdown()

    assert history == [("synapse", "thinking"), ("synapse", "waiting"),
                       ("synapse-events", "done"), ("synapse", "idle")]
    assert not list(sources.glob("synapse*.json"))           # cleaned up on exit


def agent(aid, status, role="desk", herdr=None):
    return {"id": aid, "role": role, "status": status,
            "metadata": {"herdrState": herdr} if herdr else {}}


def test_agent_items_skips_orchestrator():
    payload = {"agents": [agent("agent-synapse", "working", role="synapse"),
                          agent("A1", "idle"), agent("A2", "working"),
                          {"id": "A3"}, "junk"]}
    assert ys.agent_items(payload) == {"A1": "idle", "A2": "working"}
    assert ys.agent_items({"agents": None}) == {}


def test_agent_items_herdr_waiting_overrides_status():
    assert ys.agent_items({"agents": [agent("A1", "working", herdr="blocked")]}) == {
        "A1": "waiting"}


def test_ongoing_state_includes_agents():
    assert ys.ongoing_state({}, agents={"A1": "idle", "A2": "complete"}) == "idle"
    assert ys.ongoing_state({}, agents={"A1": "working"}) == "thinking"
    assert ys.ongoing_state({"W1": "working"}, agents={"A1": "waiting"}) == "waiting"


def test_agent_finished_transitions():
    f = ys.AGENT_FINISHED
    assert ys.finished_since({"A1": "working"}, {"A1": "complete"}, f) == "done"
    assert ys.finished_since({"A1": "complete"}, {"A1": "complete"}, f) is None
    assert ys.finished_since({"A1": "working"}, {"A1": "idle"}, f) is None


def test_run_with_desk_agents(sources):
    work = {"items": []}
    agents = [
        {"agents": [agent("agent-synapse", "working", role="synapse"),
                    agent("OLD", "complete"), agent("A1", "idle")]},
        {"agents": [agent("OLD", "complete"), agent("A1", "working")]},
        {"agents": [agent("OLD", "complete"), agent("A1", "complete")]},
    ]
    polls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/agents":
                body = agents[min(len(polls), len(agents) - 1)]
                polls.append(self.path)
            else:
                body = work
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    history = []
    real_emit, real_sleep = bus.emit, time.sleep

    def spy(source, state, **kw):
        history.append((source, state))
        return real_emit(source, state, **kw)

    def fake_sleep(_s):
        if len(polls) >= len(agents):
            raise KeyboardInterrupt
        real_sleep(0.01)

    ys.bus.emit, ys.time.sleep = spy, fake_sleep
    try:
        ys.run(f"{base}/api/work", interval=0.01, blocked=True, verbose=False,
               agents_url=f"{base}/api/agents")
    finally:
        ys.bus.emit, ys.time.sleep = real_emit, real_sleep
        srv.shutdown()

    # OLD was already complete at start: no bloom for it
    assert history == [("synapse", "idle"), ("synapse", "thinking"),
                       ("synapse-events", "done"), ("synapse", "idle")]
    assert not list(sources.glob("synapse*.json"))
