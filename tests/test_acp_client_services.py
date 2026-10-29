"""P5/W1 T5 — the ACP v1 client-side services: fs/* and terminal/*.

These were the recorded absent optional methods. The agent (server) CALLS
them on the client (the editor); the client answers through injected public
services. Without a service, both sides fail closed — a read-only integration
that fakes an answer is worse than one that says it cannot.

NOTE on structure: every round trip runs INSIDE one event loop. A client's
reader task is bound to the loop that created it, so a test that initializes
in one ``run()`` and calls in another has a dead reader and times out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from acp._compat import run_sync as run
from acp.client import ACPClient
from acp.models import ACPError
from acp.server import ACPServer
from acp.transport import InMemoryACPTransport


class _RecordingTransport:
    """A transport that records what the client sends and never receives."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def start(self) -> "_RecordingTransport":
        return self

    async def asend(self, message: Any) -> None:
        self.sent.append(dict(message))

    async def areceive(self, timeout: Any = None) -> None:
        await asyncio.sleep(0.01)
        return None

    async def aclose(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class _StubAgent:
    """The smallest public-agent shape ACPServer needs for a session."""

    def stream(self, request: Any, **kwargs: Any):
        return iter(())

    def close(self) -> None:
        return None


def _pair(**client_kwargs: Any):
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(_StubAgent(), server_transport)
    client = ACPClient(client_transport, timeout_s=2, **client_kwargs)
    return client, server


class TestTheCapabilityGateIsFailClosed:
    def test_fs_read_is_refused_when_the_client_never_advertised_it(self, tmp_path):
        client, server = _pair()

        async def scenario() -> str:
            await server.start()
            await client.initialize()
            try:
                await server.fs_read_text_file("sess-1", str(tmp_path / "a.py"))
            except ACPError as exc:
                answer = str(exc)
            else:
                answer = "CALLED"
            await client.close()
            await server.aclose()
            return answer

        answer = run(scenario())
        assert "readTextFile" in answer
        assert "unavailable" in answer

    def test_terminal_calls_are_refused_when_the_client_never_advertised(self):
        client, server = _pair()

        async def scenario() -> str:
            await server.start()
            await client.initialize()
            try:
                await server.terminal_create("sess-1", "pytest")
            except ACPError as exc:
                answer = str(exc)
            else:
                answer = "CALLED"
            await client.close()
            await server.aclose()
            return answer

        assert "terminal" in run(scenario())


class TestTheFsRoundTrip:
    def test_fs_read_round_trip_through_the_injected_service(self, tmp_path):
        client, server = _pair(
            filesystem={"read_text_file": lambda path, **kw: f"contents of {path}"}
        )

        async def scenario() -> Any:
            await server.start()
            await client.initialize()
            # Injecting the service advertised the capability.
            assert client.client_capabilities["fs"]["readTextFile"] is True
            content = await server.fs_read_text_file("sess-1", str(tmp_path / "a.py"))
            await client.close()
            await server.aclose()
            return content

        assert run(scenario()) == f"contents of {tmp_path / 'a.py'}"

    def test_fs_write_round_trip_reaches_the_service(self, tmp_path):
        writes: list[dict[str, Any]] = []
        client, server = _pair(
            filesystem={"write_text_file": lambda **kw: writes.append(kw)}
        )

        async def scenario() -> None:
            await server.start()
            await client.initialize()
            await server.fs_write_text_file("sess-1", str(tmp_path / "a.py"), "x = 1\n")
            await client.close()
            await server.aclose()

        run(scenario())
        assert writes == [{"path": str(tmp_path / "a.py"), "content": "x = 1\n"}]

    def test_a_service_object_with_methods_works_too(self):
        class FS:
            def read_text_file(self, path: str, **kw: Any) -> str:
                return f"read {path}"

        client, server = _pair(filesystem=FS())

        async def scenario() -> Any:
            await server.start()
            await client.initialize()
            content = await server.fs_read_text_file("sess-1", "a.py")
            await client.close()
            await server.aclose()
            return content

        assert run(scenario()) == "read a.py"


class TestTheTerminalRoundTrip:
    def test_the_full_terminal_lifecycle_routes(self):
        calls: list[tuple[str, Any]] = []

        def _record(name: str):
            def _call(**kw: Any) -> Any:
                calls.append((name, kw))
                return {
                    "create": "term-1",
                    "output": {"output": "1 passed", "exitStatus": None},
                    "wait_for_exit": {"exitStatus": {"code": 0}},
                }.get(name)

            return _call

        client, server = _pair(
            terminal={
                "create": _record("create"),
                "output": _record("output"),
                "wait_for_exit": _record("wait_for_exit"),
                "kill": _record("kill"),
                "release": _record("release"),
            }
        )

        async def scenario() -> None:
            await server.start()
            await client.initialize()
            assert client.client_capabilities["terminal"] is True
            terminal_id = await server.terminal_create(
                "sess-1", "pytest", args=["-q"], cwd="."
            )
            assert terminal_id == "term-1"
            out = await server.terminal_output("sess-1", terminal_id)
            assert out["output"] == "1 passed"
            done = await server.terminal_wait_for_exit("sess-1", terminal_id)
            assert done["exitStatus"]["code"] == 0
            await server.terminal_kill("sess-1", terminal_id)
            await server.terminal_release("sess-1", terminal_id)
            await client.close()
            await server.aclose()

        run(scenario())
        assert [name for name, _ in calls] == [
            "create",
            "output",
            "wait_for_exit",
            "kill",
            "release",
        ]
        assert calls[0][1]["command"] == "pytest"
        assert calls[0][1]["args"] == ["-q"]


class TestTheClientSideFailuresAreHonest:
    def test_a_missing_service_answers_method_not_found(self):
        transport = _RecordingTransport()
        client = ACPClient(transport, timeout_s=1)

        async def scenario() -> None:
            await client._handle_client_service_request(
                7, "fs/read_text_file", {"sessionId": "s", "path": "/x"}
            )

        run(scenario())
        assert len(transport.sent) == 1
        error = transport.sent[0]["error"]
        assert error["code"] == -32601
        assert "no fs service" in error["message"]

    def test_a_service_error_is_reported_not_silently_dropped(self):
        def _explode(**kw: Any) -> Any:
            raise RuntimeError("disk ate it")

        transport = _RecordingTransport()
        client = ACPClient(transport, timeout_s=1, filesystem={"read_text_file": _explode})

        async def scenario() -> None:
            await client._handle_client_service_request(
                9, "fs/read_text_file", {"sessionId": "s", "path": "/x"}
            )

        run(scenario())
        error = transport.sent[0]["error"]
        assert error["code"] == -32603
        assert "disk ate it" in error["message"]

    def test_bad_params_are_refused_before_any_service_call(self):
        transport = _RecordingTransport()
        client = ACPClient(
            transport, timeout_s=1, filesystem={"write_text_file": lambda **kw: None}
        )

        async def scenario() -> None:
            await client._handle_client_service_request(
                11, "fs/write_text_file", {"sessionId": "s", "path": "/x"}
            )

        run(scenario())
        assert transport.sent[0]["error"]["code"] == -32602


class TestTheServerSideFailureIsObservable:
    def test_a_client_service_error_reaches_the_caller(self, tmp_path):
        def _explode(**kw: Any) -> Any:
            raise RuntimeError("disk ate it")

        client, server = _pair(filesystem={"read_text_file": _explode})

        async def scenario() -> str:
            await server.start()
            await client.initialize()
            try:
                await server.fs_read_text_file("sess-1", str(tmp_path / "a.py"))
            except ACPError as exc:
                answer = str(exc)
            else:
                answer = "NO ERROR"
            await client.close()
            await server.aclose()
            return answer

        assert "disk ate it" in run(scenario())
