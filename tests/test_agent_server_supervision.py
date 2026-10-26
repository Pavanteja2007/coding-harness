"""P5/W1 T5 — supervision and reconnect for AgentServer.

The recorded gap: "no supervision/reconnect story for AgentServer — a crashed
server is restarted by whoever launched it, and a client that cannot reconnect
reports a failure it cannot distinguish from a genuine one."
"""

from __future__ import annotations

import threading
import time
import urllib.request
from pathlib import Path

import pytest

from agent_sdk.server import AgentServer
from agent_sdk.supervision import ServerSupervisor, wait_for_health


class _FakeHTTPD:
    """A serve_forever that dies on chosen invocations and blocks otherwise."""

    def __init__(self, die_on_calls=()):
        self.die_on_calls = set(die_on_calls)
        self.calls = 0
        self.daemon_threads = True
        self.agent_server = None
        self.server_address = ("127.0.0.1", 8123)
        self._release = threading.Event()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.calls += 1
        if self.calls in self.die_on_calls:
            raise RuntimeError("serving thread exploded")
        # Each serving generation waits fresh: an Event once set stays set,
        # and the watchdog's own stop() sets it before the next generation.
        self._release.clear()
        self._release.wait(30)

    def shutdown(self) -> None:
        self._release.set()

    def server_close(self) -> None:
        self._release.set()


def _server(tmp_path: Path, monkeypatch, die_on_calls=()):
    fake = _FakeHTTPD(die_on_calls)

    def _fake_bind(self):
        # Mirror the real _bind's state transitions: it clears _stopped,
        # or the watchdog reads its own restart as "stopped elsewhere".
        self._httpd = fake
        self.host, self.port = self.address
        self._stopped = False
        return fake

    monkeypatch.setattr(AgentServer, "_bind", _fake_bind)
    server = AgentServer(str(tmp_path))
    return server, fake


def _wait_for(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestTheWatchdog:
    def test_a_healthy_server_is_left_alone(self, tmp_path, monkeypatch):
        server, fake = _server(tmp_path, monkeypatch)
        supervisor = ServerSupervisor(server, poll_s=0.02)
        supervisor.start()
        try:
            time.sleep(0.2)
            assert supervisor.state == "running"
            assert supervisor.restarts == 0
            assert server._thread is not None and server._thread.is_alive()
        finally:
            supervisor.stop()
        assert fake.calls == 1

    def test_a_dead_serving_thread_is_restarted(self, tmp_path, monkeypatch):
        server, fake = _server(tmp_path, monkeypatch, die_on_calls={1})
        events = []
        supervisor = ServerSupervisor(server, poll_s=0.02, backoff_s=(0.01,), on_event=events.append)
        supervisor.start()
        try:
            assert _wait_for(
                lambda: supervisor.restarts >= 1
                and server._thread is not None
                and server._thread.is_alive()
            )
            assert supervisor.state == "running"
            assert fake.calls >= 2
        finally:
            supervisor.stop()
        kinds = [event.kind for event in events]
        assert "restart" in kinds
        assert "restarted" in kinds

    def test_a_server_that_keeps_dying_is_reported_exhausted(self, tmp_path, monkeypatch):
        server, fake = _server(tmp_path, monkeypatch, die_on_calls=set(range(1, 100)))
        supervisor = ServerSupervisor(
            server, max_restarts=2, poll_s=0.02, backoff_s=(0.01,)
        )
        supervisor.start()
        try:
            assert _wait_for(lambda: supervisor.state == "exhausted")
            assert supervisor.restarts == 2
            final_state = supervisor.state
        finally:
            supervisor.stop()
        report = supervisor.report()
        assert final_state == "exhausted"
        assert any(event.kind == "exhausted" for event in report.events)

    def test_a_deliberately_stopped_server_is_not_restarted(self, tmp_path, monkeypatch):
        server, fake = _server(tmp_path, monkeypatch)
        supervisor = ServerSupervisor(server, poll_s=0.02)
        supervisor.start()
        server.stop()  # somebody else stops it
        assert _wait_for(lambda: any(e.kind == "watchdog_exit" for e in supervisor.report().events))
        assert supervisor.restarts == 0
        assert fake.calls == 1

    def test_an_injected_transport_is_refused_up_front(self, tmp_path):
        server = AgentServer(str(tmp_path), transport=object())
        with pytest.raises(ValueError, match="restartable"):
            ServerSupervisor(server)


class TestWaitForHealth:
    def test_a_live_server_reports_healthy(self, tmp_path):
        server = AgentServer(str(tmp_path)).start()
        try:
            assert wait_for_health(server.url, timeout_s=5.0) is True
        finally:
            server.stop()

    def test_a_dead_port_reports_false_within_the_budget(self):
        start = time.monotonic()
        assert wait_for_health("http://127.0.0.1:1", timeout_s=0.3) is False
        assert time.monotonic() - start < 5.0

    def test_an_empty_url_is_false_not_an_error(self):
        assert wait_for_health("", timeout_s=0.1) is False

    def test_a_client_reconnects_after_a_restart(self, tmp_path):
        """The gap's second half: after a restart, the client gets a real
        answer, not a hang it cannot tell from a failure."""
        server = AgentServer(str(tmp_path)).start()
        first_port = server.address[1]
        server.stop()
        assert wait_for_health(server.url, timeout_s=0.3) is False
        restarted = AgentServer(str(tmp_path), port=first_port).start()
        try:
            assert wait_for_health(restarted.url, timeout_s=5.0) is True
            with urllib.request.urlopen(restarted.url + "/health", timeout=5) as response:
                assert response.status == 200
        finally:
            restarted.stop()
