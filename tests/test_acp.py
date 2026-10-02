"""Offline regression tests for the ACP v1 adapters."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from acp import (
    ACPClient,
    ACPError,
    ACPPromptResult,
    ACPServer,
    ACPTimeoutError,
    ACPTransportError,
    InMemoryACPTransport,
    StdioACPTransport,
)


class ScriptedAgent:
    """Deterministic public-method agent used by protocol tests."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.closed = False
        self.calls: list[tuple[str, Any]] = []

    async def stream(self, prompt: str, session_id: str | None = None) -> Any:
        """Yield ordered chunks and a verifier-backed terminal result."""
        self.calls.append(("stream", (prompt, session_id)))
        self.started.set()
        yield "first "
        await asyncio.sleep(0)
        yield {"type": "text", "text": "second"}
        yield {
            "status": "completed_verified",
            "answer": "done",
            "verification_evidence": [
                {
                    "kind": "verification",
                    "target_test_passed": True,
                    "regression_passed": True,
                    "flaky": False,
                }
            ],
        }

    def cancel(self, session_id: str | None = None) -> None:
        """Record a public cancellation request."""
        self.calls.append(("cancel", session_id))
        self.cancelled.set()

    def close(self) -> None:
        """Record public resource cleanup."""
        self.closed = True


class BlockingAgent:
    """Agent whose public stream waits until cancellation is requested."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.closed = False

    async def stream(self, prompt: str, session_id: str | None = None) -> Any:
        """Start a cancellable stream without using private state."""
        self.started.set()
        while not self.cancelled.is_set():
            await asyncio.sleep(0.01)
        yield {"type": "text", "text": "late but before terminal"}

    def cancel(self, session_id: str | None = None) -> None:
        """Release the stream loop through the public cancellation method."""
        self.cancelled.set()

    def close(self) -> None:
        """Record cleanup."""
        self.closed = True


class SyncAgent:
    """Synchronous public agent used to verify event-loop isolation."""

    def stream(self, prompt: str, session_id: str | None = None) -> Any:
        """Block briefly in a worker, then yield a terminal result."""
        time.sleep(0.05)
        yield "sync chunk"
        yield {"status": "completed_unverified", "answer": "sync done"}


class QueuedResponseTransport:
    """Async transport that permits out-of-order response injection."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def areceive(self, timeout: float | None = None) -> dict[str, Any]:
        """Return queued messages or raise a typed receive timeout."""
        try:
            if timeout is None:
                return await self.incoming.get()
            return await asyncio.wait_for(self.incoming.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ACPTransportError(-32000, "ACP transport receive timed out") from exc

    async def asend(self, message: dict[str, Any]) -> None:
        """Record an outgoing protocol message."""
        self.sent.append(dict(message))

    def close(self) -> None:
        """Mark the fake transport closed."""
        self.closed = True


class FakeRunHandle:
    """Public run handle used to verify request-to-handle cancellation."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.cancelled = False

    def cancel(self) -> bool:
        """Record cancellation without exposing private transport state."""
        self.cancelled = True
        return True


class StubbornHandleAgent:
    """Agent whose handle acknowledges cancellation but does not stop its stream."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.handle = FakeRunHandle("stubborn-run")

    async def stream(self, prompt: str, session_id: str | None = None) -> Any:
        """Keep the public stream active after cancellation acknowledgement."""
        self.started.set()
        while True:
            await asyncio.sleep(0.005)
            yield "should not finish"


class HandleResetAgent:
    """Agent that exposes a changing public handle while a stream is active."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.handle = FakeRunHandle("run-current")
        self.fallback_cancels = 0

    async def stream(self, prompt: str, session_id: str | None = None) -> Any:
        """Start a handle-backed stream that waits for cancellation."""
        self.started.set()
        while not self.handle.cancelled:
            await asyncio.sleep(0.005)
        yield {"status": "cancelled", "answer": "cancelled"}

    def cancel(self, session_id: str | None = None) -> None:
        """Fallback cancellation path used only if handle mapping fails."""
        self.fallback_cancels += 1


def run(coroutine: Any) -> Any:
    """Run one async regression scenario on Python 3.10 without a plugin."""
    return asyncio.run(coroutine)


def test_request_matching_survives_idle_and_out_of_order_responses() -> None:
    """Request deadlines match IDs without discarding queued peer responses."""
    transport = QueuedResponseTransport()
    client = ACPClient(transport, timeout_s=0.2)

    async def scenario() -> tuple[Any, Any]:
        first = asyncio.create_task(client.request("first", timeout_s=1))
        for _ in range(100):
            if transport.sent:
                break
            await asyncio.sleep(0.001)
        await transport.incoming.put(
            {"jsonrpc": "2.0", "id": 2, "result": {"second": True}}
        )
        await transport.incoming.put(
            {"jsonrpc": "2.0", "id": 1, "result": {"first": True}}
        )
        first_result = await asyncio.wait_for(first, timeout=1)
        await asyncio.sleep(0.25)
        second = asyncio.create_task(client.request("second", timeout_s=1))
        for _ in range(100):
            if len(transport.sent) >= 2:
                break
            await asyncio.sleep(0.001)
        await transport.incoming.put(
            {"jsonrpc": "2.0", "id": 1, "result": {"duplicate": True}}
        )
        second_result = await asyncio.wait_for(second, timeout=1)
        await client.close()
        return first_result, second_result

    first_result, second_result = run(scenario())
    assert first_result == {"first": True}
    assert second_result == {"second": True}
    assert transport.closed is True


def test_request_timeout_only_times_out_matching_id() -> None:
    """A timed-out request does not poison the next request on the connection."""
    transport = QueuedResponseTransport()
    client = ACPClient(transport, timeout_s=0.05)

    async def scenario() -> Any:
        with pytest.raises(ACPTimeoutError):
            await client.request("never", timeout_s=0.05)
        next_request = asyncio.create_task(client.request("next", timeout_s=1))
        for _ in range(100):
            if len(transport.sent) >= 3:
                break
            await asyncio.sleep(0.001)
        await transport.incoming.put(
            {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
        )
        result = await asyncio.wait_for(next_request, timeout=1)
        await client.close()
        return result

    assert run(scenario()) == {"ok": True}
    assert transport.closed is True


def test_cancel_maps_jsonrpc_request_to_current_run_handle(tmp_path: Path) -> None:
    """Cancellation uses the captured handle and keeps the connection alive."""
    agent = HandleResetAgent()
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(agent, server_transport)
    client = ACPClient(client_transport, timeout_s=1)

    async def scenario() -> ACPPromptResult:
        await server.start()
        await client.initialize()
        session = await client.new_session(str(tmp_path))
        prompt_task = asyncio.create_task(client.prompt(session.session_id, "wait"))
        for _ in range(100):
            if agent.started.is_set():
                break
            await asyncio.sleep(0.005)
        await client.cancel(session.session_id)
        result = await asyncio.wait_for(prompt_task, timeout=1)
        assert client.closed is False
        await client.close()
        await server.aclose()
        return result

    result = run(scenario())
    assert result.status == "cancelled"
    assert agent.handle.cancelled is True
    assert agent.fallback_cancels == 0


def test_cancel_does_not_wait_forever_for_stubborn_agent(tmp_path: Path) -> None:
    """Cancellation returns a terminal response even if a run handle ignores it."""
    agent = StubbornHandleAgent()
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(agent, server_transport)
    client = ACPClient(client_transport, timeout_s=1)

    async def scenario() -> ACPPromptResult:
        await server.start()
        await client.initialize()
        session = await client.new_session(str(tmp_path))
        task = asyncio.create_task(client.prompt(session.session_id, "stubborn"))
        for _ in range(100):
            if agent.started.is_set():
                break
            await asyncio.sleep(0.005)
        await client.cancel(session.session_id)
        result = await asyncio.wait_for(task, timeout=1)
        await client.close()
        await server.aclose()
        return result

    result = run(scenario())
    assert result.status == "cancelled"


def test_cancel_request_without_active_mapping_returns_typed_error() -> None:
    """A stale cancellation request never hangs and returns invalid params."""
    server = ACPServer(ScriptedAgent())

    async def scenario() -> dict[str, Any]:
        await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1},
            }
        )
        return await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "$/cancel_request",
                "params": {"id": 999},
            }
        )

    response = run(scenario())
    assert response["error"]["code"] == -32602
    assert "mapping" in response["error"]["message"]


def test_initialize_rejects_unsupported_versions_and_maps_v1_auth() -> None:
    """Stable v1 fails closed and does not treat terminal/host auth as agent auth."""
    server = ACPServer(
        ScriptedAgent(),
        auth_methods=[
            {"id": "never", "name": "Never"},
            {"id": "host", "name": "Host", "type": "bearer"},
            {"id": "terminal", "name": "Terminal", "type": "terminal"},
        ],
    )

    async def scenario() -> tuple[Any, Any, Any]:
        unsupported = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 2},
            }
        )
        initialized = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"auth": {"terminal": True}},
                },
            }
        )
        repeated = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "initialize",
                "params": {"protocolVersion": 1},
            }
        )
        return unsupported, initialized, repeated

    unsupported, initialized, repeated = run(scenario())
    assert unsupported["error"]["code"] == -32000
    assert {item["id"] for item in initialized["result"]["authMethods"]} == {
        "never",
        "host",
        "terminal",
    }
    assert repeated["error"]["code"] == -32600


def test_strict_envelope_validation_returns_redacted_errors() -> None:
    """Invalid IDs, params, sessions, prompts, and capabilities fail closed."""
    server = ACPServer(ScriptedAgent())

    async def scenario() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        results.append(
            await server.handle_message(
                {"jsonrpc": "2.0", "id": True, "method": "ping", "params": {}}
            )
        )
        results.append(
            await server.handle_message(
                {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None}
            )
        )
        results.append(
            await server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {"protocolVersion": 1},
                }
            )
        )
        results.append(
            await server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "initialize",
                    "params": {"protocolVersion": 1},
                }
            )
        )
        results.append(
            await server.handle_message(
                {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
            )
        )
        results.append(
            await server.handle_message(
                {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
            )
        )
        results.append(
            await server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {"sessionId": True, "prompt": []},
                }
            )
        )
        results.append(
            await server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/prompt",
                    "params": {"sessionId": "missing", "prompt": [{"type": "text"}]},
                }
            )
        )
        return results

    results = run(scenario())
    assert results[0]["error"]["code"] == -32600
    assert results[1]["error"]["code"] == -32602
    assert results[2]["error"]["code"] == -32600
    assert results[3]["result"]["protocolVersion"] == 1
    assert results[4]["result"] == {}
    assert results[5]["error"]["code"] == -32600
    assert results[6]["error"]["code"] == -32602
    assert results[7]["error"]["code"] == -32602
    assert "[REDACTED_SECRET]" not in json.dumps(results)


def test_required_capability_errors_are_typed() -> None:
    """Unknown required client capabilities are rejected with safe data."""
    server = ACPServer(ScriptedAgent(), required_capabilities=("fs.readTextFile",))

    async def scenario() -> dict[str, Any]:
        return await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False}},
                },
            }
        )

    response = run(scenario())
    assert response["error"]["code"] == -32602
    assert response["error"]["data"]["capability"] == "fs.readTextFile"


def test_permission_hook_responds_and_default_denies() -> None:
    """Optional permission hooks preserve the v1 response and default safely."""
    client_transport, peer_transport = InMemoryACPTransport.pair()
    decisions: list[dict[str, Any]] = []

    async def choose(params: dict[str, Any]) -> dict[str, Any]:
        decisions.append(dict(params))
        return {"optionId": "allow-once"}

    client = ACPClient(client_transport, on_permission_request=choose)
    default_transport, default_peer = InMemoryACPTransport.pair()
    default_client = ACPClient(default_transport)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        await client.connect()
        await peer_transport.send(
            {
                "jsonrpc": "2.0",
                "id": "permission-1",
                "method": "session/request_permission",
                "params": {
                    "sessionId": "session-1",
                    "toolCall": {"toolCallId": "call-1"},
                    "options": [
                        {"optionId": "reject", "kind": "reject_once"},
                        {"optionId": "allow-once", "kind": "allow_once"},
                    ],
                },
            }
        )
        response = await asyncio.wait_for(peer_transport.areceive(1), timeout=1)
        await client.close()
        await default_client.connect()
        await default_peer.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "session-2",
                    "toolCall": {"toolCallId": "call-2"},
                    "options": [{"optionId": "reject", "kind": "reject_once"}],
                },
            }
        )
        default_response = await asyncio.wait_for(default_peer.areceive(1), timeout=1)
        await default_client.close()
        return response, default_response

    response, default_response = run(scenario())
    assert response["id"] == "permission-1"
    assert response["result"]["outcome"]["optionId"] == "allow-once"
    assert default_response["result"]["outcome"]["outcome"] == "selected"
    assert default_response["result"]["outcome"]["optionId"] == "reject"
    assert decisions


def test_server_permission_request_round_trip(tmp_path: Path) -> None:
    """An agent permission request is answered without losing the prompt response."""

    class PermissionAgent(ScriptedAgent):
        async def stream(self, prompt: str, session_id: str | None = None) -> Any:
            yield {
                "type": "permission_request",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": "tool-1"},
                    "options": [{"optionId": "allow", "kind": "allow_once"}],
                },
            }
            yield "after permission"
            yield {"status": "completed_unverified", "answer": "done"}

    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(PermissionAgent(), server_transport)
    client = ACPClient(
        client_transport,
        on_permission_request=lambda params: {"optionId": "allow"},
    )

    async def scenario() -> ACPPromptResult:
        await server.start()
        await client.initialize()
        session = await client.new_session(str(tmp_path))
        result = await client.prompt(session.session_id, "permission")
        await client.close()
        await server.aclose()
        return result

    result = run(scenario())
    assert result.status == "completed_unverified"
    assert result.text == "after permission"


def test_transport_request_preserves_mismatched_responses() -> None:
    """The compatibility transport request helper also matches by ID."""
    left, right = InMemoryACPTransport.pair()

    def respond() -> None:
        right.send({"jsonrpc": "2.0", "id": 2, "result": {"second": True}})
        right.send({"jsonrpc": "2.0", "id": 1, "result": {"first": True}})

    thread = threading.Thread(target=respond)
    thread.start()
    assert left.request("first", timeout_s=1) == {"first": True}
    assert left.request("second", timeout_s=1) == {"second": True}
    thread.join(timeout=1)
    left.close()
    right.close()


def test_async_duplicate_request_id_gets_one_error() -> None:
    """Duplicate in-flight IDs receive one rejection without killing the reader."""
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(ScriptedAgent(), server_transport)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        await server.start()
        await client_transport.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1},
            }
        )
        await asyncio.wait_for(client_transport.areceive(1), timeout=1)
        await client_transport.send(
            {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}
        )
        await client_transport.send(
            {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}
        )
        first = await asyncio.wait_for(client_transport.areceive(1), timeout=1)
        second = await asyncio.wait_for(client_transport.areceive(1), timeout=1)
        await server.aclose()
        return first, second

    first, second = run(scenario())
    assert first.get("result") == {} or second.get("result") == {}
    assert (
        first.get("error", {}).get("code") == -32600
        or second.get("error", {}).get("code") == -32600
    )


def test_host_bearer_auth_allows_explicit_localhost_only() -> None:
    """Host-owned bearer auth remains transport-side and loopback constrained."""
    local = ACPServer(
        ScriptedAgent(),
        auth_methods=[
            {
                "id": "host",
                "name": "Host",
                "type": "bearer",
                "host": "http://localhost:8765",
            }
        ],
    )
    remote = ACPServer(
        ScriptedAgent(),
        auth_methods=[
            {
                "id": "host",
                "name": "Host",
                "type": "bearer",
                "host": "https://example.invalid",
            }
        ],
    )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        local_response = await local.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1},
            }
        )
        remote_response = await remote.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1},
            }
        )
        return local_response, remote_response

    local_response, remote_response = run(scenario())
    assert local_response["result"]["protocolVersion"] == 1
    assert remote_response["error"]["code"] == -32602


def test_explicit_auth_required_false_disables_agent_gate() -> None:
    """The compatibility override remains explicit after v1 negotiation."""
    server = ACPServer(
        ScriptedAgent(),
        auth_methods=[{"id": "login", "name": "Login"}],
        auth_required=False,
    )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        initialized = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1},
            }
        )
        session = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": str(Path.cwd()), "mcpServers": []},
            }
        )
        return initialized, session

    initialized, session = run(scenario())
    assert initialized["result"]["protocolVersion"] == 1
    assert session["result"]["sessionId"]


def test_initialize_negotiates_version_and_capabilities(tmp_path: Path) -> None:
    """A client accepts only the server's supported ACP version."""
    agent = ScriptedAgent()
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(agent, server_transport, capabilities={"loadSession": True})
    client = ACPClient(client_transport)

    async def scenario() -> tuple[Any, Any]:
        await server.start()
        capabilities = await client.initialize()
        session = await client.new_session(str(tmp_path))
        result = await client.prompt(session.session_id, "hello")
        await client.close()
        await server.aclose()
        return capabilities, result

    capabilities, result = run(scenario())
    assert capabilities.protocol_version == 1
    assert capabilities.load_session is True
    assert result.status == "completed_verified"
    assert result.verified is True
    assert result.stop_reason == "end_turn"
    assert result.text == "first second"
    assert agent.closed is True


def test_stream_updates_are_ordered_and_terminal_result_is_returned(
    tmp_path: Path,
) -> None:
    """Session updates arrive in order before the prompt response."""
    received: list[dict[str, Any]] = []
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(ScriptedAgent(), server_transport)
    client = ACPClient(client_transport, on_update=received.append)

    async def scenario() -> ACPPromptResult:
        await server.start()
        await client.initialize()
        session = await client.new_session(str(tmp_path))
        result = await client.prompt(session.session_id, "ordered")
        await client.close()
        await server.aclose()
        return result

    result = run(scenario())
    assert [item["update"]["content"]["text"] for item in received] == [
        "first ",
        "second",
    ]
    assert result.status == "completed_verified"
    assert result.verified is True


def test_sync_agent_does_not_block_event_loop(tmp_path: Path) -> None:
    """A synchronous public stream runs off-loop while the client remains live."""
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(SyncAgent(), server_transport)
    client = ACPClient(client_transport)
    ticks = 0

    async def scenario() -> ACPPromptResult:
        nonlocal ticks
        await server.start()
        await client.initialize()
        session = await client.new_session(str(tmp_path))

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.005)
                ticks += 1

        result, _ = await asyncio.gather(
            client.prompt(session.session_id, "sync"), ticker()
        )
        await client.close()
        await server.aclose()
        return result

    result = run(scenario())
    assert ticks == 5
    assert result.text == "sync chunk"
    assert result.status == "completed_unverified"


def test_unverified_agent_result_never_claims_verified_completion(
    tmp_path: Path,
) -> None:
    """A model completion without clean evidence is downgraded honestly."""

    class UnverifiedAgent(ScriptedAgent):
        async def stream(self, prompt: str, session_id: str | None = None) -> Any:
            """Yield a successful-looking but unverified terminal result."""
            yield "answer"
            yield {"status": "completed_verified", "answer": "not proven"}

    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(UnverifiedAgent(), server_transport)
    client = ACPClient(client_transport)

    async def scenario() -> ACPPromptResult:
        await server.start()
        await client.initialize()
        session = await client.new_session(str(tmp_path))
        result = await client.prompt(session.session_id, "unverified")
        await client.close()
        await server.aclose()
        return result

    result = run(scenario())
    assert result.status == "completed_unverified"
    assert result.verified is False
    assert result.stop_reason == "end_turn"


def test_cancel_before_prompt_is_safe_and_reaches_public_cancel() -> None:
    """A cancellation notification is harmless before a prompt starts."""
    agent = ScriptedAgent()
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(agent, server_transport)
    client = ACPClient(client_transport)

    async def scenario() -> None:
        await server.start()
        await client.initialize()
        session = await client.new_session(str(Path.cwd()))
        await client.cancel(session.session_id)
        await client.close()
        await server.aclose()

    run(scenario())
    assert any(name == "cancel" for name, _value in agent.calls)


def test_cancel_during_prompt_is_concurrent_and_late_updates_are_safe(
    tmp_path: Path,
) -> None:
    """A client can cancel an active turn while the server awaits its stream."""
    agent = BlockingAgent()
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(agent, server_transport)
    client = ACPClient(client_transport)

    async def scenario() -> ACPPromptResult:
        await server.start()
        await client.initialize()
        session = await client.new_session(str(tmp_path))
        task = asyncio.create_task(client.prompt(session.session_id, "wait"))
        for _ in range(100):
            if agent.started.is_set():
                break
            await asyncio.sleep(0.01)
        await client.cancel(session.session_id)
        result = await asyncio.wait_for(task, timeout=2)
        await client.close()
        await server.aclose()
        return result

    result = run(scenario())
    assert result.status == "cancelled"
    assert result.stop_reason == "cancelled"
    assert result.verified is False


def test_malformed_unknown_method_and_version_fail_closed() -> None:
    """Invalid envelopes and unsupported versions receive redacted errors."""
    server = ACPServer(ScriptedAgent())

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        malformed = await server.handle_message(
            {"jsonrpc": "1.0", "id": 1, "method": "initialize"}
        )
        version = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": 99},
            }
        )
        unknown = await server.handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "no/such", "params": {}}
        )
        return malformed, version, unknown

    malformed, version, unknown = run(scenario())
    assert malformed["error"]["code"] == -32600
    assert version["error"]["code"] == -32000
    assert unknown["error"]["code"] == -32601
    assert "[REDACTED_SECRET]" not in json.dumps([malformed, version, unknown])


def test_public_errors_and_notifications_are_redacted(tmp_path: Path) -> None:
    """Secret-shaped agent text and errors are scrubbed at the ACP boundary."""

    class SecretAgent(ScriptedAgent):
        async def stream(self, prompt: str, session_id: str | None = None) -> Any:
            """Yield a secret-shaped chunk and raise a secret-shaped error."""
            yield "api_key=sk-test-secret-123456789"
            raise RuntimeError("token=ghp_abcdefghijklmnop")

    updates: list[dict[str, Any]] = []
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(SecretAgent(), server_transport)
    client = ACPClient(client_transport, on_update=updates.append)

    async def scenario() -> ACPPromptResult:
        await server.start()
        await client.initialize()
        session = await client.new_session(str(tmp_path))
        result = await client.prompt(session.session_id, "secret")
        await client.close()
        await server.aclose()
        return result

    result = run(scenario())
    payload = json.dumps([updates, result.to_dict()])
    assert "sk-test-secret-123456789" not in payload
    assert "ghp_abcdefghijklmnop" not in payload
    assert "[REDACTED_SECRET]" in payload


def test_set_mode_authentication_and_sync_transport_surface(tmp_path: Path) -> None:
    """Optional auth and mode methods work over the in-memory sync transport."""
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(
        ScriptedAgent(),
        server_transport,
        auth_methods=[{"id": "login", "name": "Login"}],
        modes={
            "currentModeId": "default",
            "availableModes": [{"id": "default"}, {"id": "code"}],
        },
    )
    client = ACPClient(client_transport)

    async def scenario() -> tuple[dict[str, Any], Any]:
        await server.start()
        capabilities = await client.initialize()
        with pytest.raises(ACPError):
            await client.new_session(str(tmp_path))
        await client.authenticate("login")
        session = await client.new_session(str(tmp_path))
        mode = await client.set_mode(session.session_id, "code")
        await client.close()
        await server.aclose()
        return capabilities.to_dict(), mode

    capabilities, mode = run(scenario())
    assert capabilities["authMethods"][0]["id"] == "login"
    assert mode["currentModeId"] == "code"


def test_shutdown_closes_client_and_server_lifecycle(tmp_path: Path) -> None:
    """The optional shutdown request completes before both sides close."""
    client_transport, server_transport = InMemoryACPTransport.pair()
    server = ACPServer(ScriptedAgent(), server_transport)
    client = ACPClient(client_transport)

    async def scenario() -> None:
        await server.start()
        await client.initialize()
        await client.shutdown()
        await server.aclose()

    run(scenario())
    assert client.closed is True
    assert server.closed is True


def test_stdio_transport_is_shell_free_scrubbed_and_protocol_only(
    tmp_path: Path,
) -> None:
    """Stdio uses argv, scrubbed child environment, and stdout for JSONL."""
    script = "import sys; sys.stderr.write('x' * 100000); sys.stdin.read()"
    transport = StdioACPTransport(
        [sys.executable, "-u", "-c", script],
        cwd=str(tmp_path),
        env={"PUBLIC_VALUE": "visible", "OPENAI_API_KEY": "must-not-leak"},
        max_stderr_bytes=128,
    )
    try:
        assert transport.argv[0] == sys.executable
        assert "OPENAI_API_KEY" not in transport.env
        assert transport.env["PUBLIC_VALUE"] == "visible"
        transport.start()
        transport.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {}})
        time.sleep(0.05)
        assert len(transport.stderr_bytes) <= 128
    finally:
        transport.close()


def test_stdio_frames_reject_bad_utf8_controls_and_size(tmp_path: Path) -> None:
    """Stdio rejects malformed frames and resumes at the next valid frame."""
    script = (
        "import sys,time; "
        "out=sys.stdout.buffer; "
        "out.write(b'\\xff\\n'); "
        'out.write(b\'{\\"jsonrpc\\":\\"2.0\\",\\"id\\":1,\\"result\\":{}}\\x00\\n\'); '
        "out.write(b'x'*300+b'\\n'); "
        'out.write(b\'{\\"jsonrpc\\":\\"2.0\\",\\"id\\":2,\\"result\\":{\\"ok\\":true}}\\n\'); '
        "out.flush(); time.sleep(5)"
    )
    transport = StdioACPTransport(
        [sys.executable, "-u", "-c", script],
        cwd=str(tmp_path),
        timeout_s=1,
        max_frame_bytes=256,
    )
    try:
        transport.start()
        for _ in range(3):
            with pytest.raises(ACPError):
                transport.receive(timeout=1)
        assert transport.receive(timeout=1)["id"] == 2
    finally:
        transport.close()


def test_stdio_wire_payload_is_not_diagnostic_redacted(tmp_path: Path) -> None:
    """Protocol data keeps legitimate bearer-shaped MCP configuration intact."""
    script = "import sys; sys.stdout.write(sys.stdin.readline()); sys.stdout.flush()"
    transport = StdioACPTransport(
        [sys.executable, "-u", "-c", script], cwd=str(tmp_path), timeout_s=1
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "session/new",
        "params": {
            "cwd": str(tmp_path),
            "mcpServers": [{"env": {"Authorization": "Bearer legitimate-local-token"}}],
        },
    }
    try:
        transport.start()
        transport.send(payload)
        received = transport.receive(timeout=1)
        assert received["params"]["mcpServers"][0]["env"]["Authorization"] == (
            "Bearer legitimate-local-token"
        )
    finally:
        transport.close()


def test_stdio_receive_timeout_is_typed_and_bounded(tmp_path: Path) -> None:
    """A child that emits no protocol line produces a bounded typed timeout."""
    transport = StdioACPTransport(
        [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
        cwd=str(tmp_path),
        timeout_s=0.05,
    )
    try:
        transport.start()
        with pytest.raises(ACPTransportError, match="timed out"):
            transport.receive(timeout=0.05)
    finally:
        process = transport.process
        transport.close()
    assert transport.closed is True
    if process is not None:
        assert process.poll() is not None


def test_real_stdio_fixture_roundtrip_and_timeout(tmp_path: Path) -> None:
    """A local Python fixture proves real subprocess framing and timeout cleanup."""
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        "\n".join(
            [
                "import json, sys",
                "for line in sys.stdin:",
                "    value = json.loads(line)",
                "    method = value.get('method')",
                "    if method == 'initialize':",
                "        result = {'protocolVersion': 1, 'agentCapabilities': {}, 'authMethods': []}",
                "    elif method == 'session/new':",
                "        result = {'sessionId': 'fixture-session'}",
                "    elif method == 'session/prompt':",
                "        sid = value['params']['sessionId']",
                "        update = {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'fixture'}}}}",
                "        sys.stdout.write(json.dumps(update) + '\\n'); sys.stdout.flush()",
                "        result = {'stopReason': 'end_turn', 'status': 'completed_unverified', 'verified': False}",
                "    else:",
                "        result = {}",
                "    if value.get('id') is not None:",
                "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': value['id'], 'result': result}) + '\\n'); sys.stdout.flush()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    transport = StdioACPTransport(
        [sys.executable, "-u", str(fixture)], cwd=str(tmp_path), timeout_s=2
    )
    client = ACPClient(transport, timeout_s=2)
    try:

        async def scenario() -> ACPPromptResult:
            await client.initialize()
            session = await client.new_session(str(tmp_path))
            return await client.prompt(session.session_id, "fixture")

        result = run(scenario())
        assert result.text == "fixture"
        assert result.status == "completed_unverified"
        assert transport.process is not None
    finally:
        client.close_sync()
        assert transport.closed is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
