"""Offline REST, SSE, WebSocket, OpenAI, and remote parity server tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_sdk import (
    AgentServer,
    LocalAgent,
    RemoteAgent,
    RemoteTransport,
    RemoteWorkspaceManager,
    ToolNotFoundError,
    UnsupportedVersionError,
    WorkspaceActiveError,
)
from extensions import HookDecision, HookManager, HookPoint
from integrations import DeferredTool, ToolCatalog, ToolDescriptor


class FinishModel:
    """Return a deterministic finish response without provider access."""

    def __init__(self, answer: str = "server finish") -> None:
        """Store the deterministic assistant answer."""
        self.answer = answer

    def __call__(self, messages, **kwargs):
        """Return one typed finish tool call."""
        return json.dumps({"tool": "finish", "answer": self.answer})


def _repo(tmp_path: Path) -> Path:
    """Create a small repository for server tests."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    return repo


def _json_request(server, method, path, payload=None, headers=None):
    """Send a JSON request and return status, decoded body, and headers."""
    data = None
    request_headers = {"Accept": "application/json"}
    request_headers.update(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        server.url + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    body = response.read().decode("utf-8")
    try:
        decoded = json.loads(body)
    except ValueError:
        decoded = body
    return response.status, decoded, response.headers


@pytest.fixture
def server(tmp_path):
    """Start and stop a deterministic local protocol server."""
    instance = AgentServer(
        str(_repo(tmp_path)),
        log_root=str(tmp_path / "server-logs"),
        model=FinishModel(),
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def test_health_capabilities_and_version_rejection(server):
    """Advertise capabilities and reject missing or incompatible versions."""
    status, health, _ = _json_request(server, "GET", "/health")
    assert status == 200
    assert health["healthy"] is True
    status, capabilities, _ = _json_request(server, "GET", "/v1/capabilities")
    assert status == 200
    assert capabilities["protocol_version"] == 1
    assert capabilities["schema_version"] == 1
    status, error, _ = _json_request(
        server,
        "POST",
        "/v1/query",
        {"request": "x", "repo_path": server.transport.repo_path},
    )
    assert status == 400
    assert error["error"]["code"] == "missing_version"
    status, error, _ = _json_request(
        server,
        "POST",
        "/v1/query",
        {
            "request": "x",
            "repo_path": server.transport.repo_path,
            "protocol_version": 99,
            "schema_version": 1,
        },
    )
    assert status == 409
    assert error["error"]["code"] == "unsupported_version"


def test_local_remote_run_and_query_parity(server, tmp_path):
    """Compare local and remote result and event semantics through one model contract."""
    local = LocalAgent(
        _repo(tmp_path / "local"),
        log_root=tmp_path / "local-logs",
        model=FinishModel("parity"),
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    remote = RemoteAgent(server.url, repo_path=server.transport.repo_path)
    try:
        local_result = local.query("question")
        remote_result = remote.query("question")
        assert local_result.status == remote_result.status == "completed_unverified"
        assert local_result.answer == "parity"
        assert remote_result.answer == "server finish"
        local_handle = local.run("task", wait=False)
        remote_handle = remote.run("task", wait=False)
        local_result = local_handle.wait(timeout=5)
        remote_result = remote_handle.wait(timeout=5)
        assert local_result.status == remote_result.status
        assert [
            event.sequence for event in local.events(local_result.run_id).replay()
        ] == [event.sequence for event in remote.events(remote_result.run_id).replay()]
    finally:
        local.close()
        remote.close()


def test_remote_cancel_rest_returns_cancelled_result(tmp_path):
    """Cancel a blocked remote run through REST and observe the canonical status."""
    repo = _repo(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def model(messages, **kwargs):
        entered.set()
        release.wait(5)
        return json.dumps({"tool": "finish", "answer": "late"})

    server = AgentServer(str(repo), log_root=str(tmp_path / "cancel-logs"), model=model)
    server.start()
    remote = RemoteAgent(server.url, repo_path=str(repo))
    try:
        handle = remote.run("cancel me", wait=False)
        assert entered.wait(2)
        assert remote.cancel(handle.run_id)
        release.set()
        assert handle.wait(timeout=5).status == "cancelled"
    finally:
        release.set()
        remote.close()
        server.stop()


def test_rest_async_status_list_replay_and_sse_reconnect(server):
    """Exercise asynchronous REST status, replay, and Last-Event-ID reconnect."""
    remote = RemoteAgent(server.url, repo_path=server.transport.repo_path)
    try:
        handle = remote.run("async task", wait=False)
        result = handle.wait(timeout=5)
        status, body, _ = _json_request(
            server,
            "GET",
            f"/v1/runs/{result.run_id}",
            headers={"X-Neo-Protocol-Version": "1", "X-Neo-Schema-Version": "1"},
        )
        assert status == 200
        assert body["run"]["status"] == "completed_unverified"
        status, replay, _ = _json_request(
            server,
            "GET",
            f"/v1/runs/{result.run_id}/replay",
            headers={"X-Neo-Protocol-Version": "1", "X-Neo-Schema-Version": "1"},
        )
        assert status == 200
        assert replay["final_status"] == "completed_unverified"
        status, runs, _ = _json_request(
            server,
            "GET",
            "/v1/runs",
            headers={"X-Neo-Protocol-Version": "1", "X-Neo-Schema-Version": "1"},
        )
        assert status == 200
        assert any(item["run_id"] == result.run_id for item in runs["runs"])
        request_headers = {
            "X-Neo-Protocol-Version": "1",
            "X-Neo-Schema-Version": "1",
            "Accept": "text/event-stream",
        }
        request = urllib.request.Request(
            server.url + f"/v1/runs/{result.run_id}/events?after=3",
            headers=request_headers,
        )
        first = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
        first_ids = [
            line.split(":", 1)[1].strip()
            for line in first.splitlines()
            if line.startswith("id:")
        ]
        request_headers["Last-Event-ID"] = "3"
        request = urllib.request.Request(
            server.url + f"/v1/runs/{result.run_id}/events",
            headers=request_headers,
        )
        second = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
        second_ids = [
            line.split(":", 1)[1].strip()
            for line in second.splitlines()
            if line.startswith("id:")
        ]
        assert first_ids == second_ids
        assert all(int(value) > 3 for value in first_ids)
    finally:
        remote.close()


def test_remote_sse_parser_yields_events(server):
    """Parse the server's standards-shaped SSE stream into Event objects."""
    remote = RemoteAgent(server.url, repo_path=server.transport.repo_path)
    try:
        result = remote.run("sse", wait=True)
        events = list(remote.transport.iter_sse_events(result.run_id))
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert all(event.schema_version == 1 for event in events)
    finally:
        remote.close()


def test_openai_models_and_chat_completion(server):
    """Map the practical OpenAI request subset to canonical response JSON."""
    status, models, _ = _json_request(server, "GET", "/v1/models")
    assert status == 200
    assert models["object"] == "list"
    assert models["data"][0]["id"] == "neo-agent"
    status, completion, _ = _json_request(
        server,
        "POST",
        "/v1/chat/completions",
        {"model": "neo-agent", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert status == 200
    assert completion["object"] == "chat.completion"
    assert completion["choices"][0]["message"]["content"] == "server finish"
    assert completion["usage"]["total_tokens"] > 0


def test_workspace_rest_lifecycle_and_active_protection(server):
    """Create, list, get, and protect deletion of active server workspaces."""
    manager = RemoteWorkspaceManager(server.url)
    workspace = manager.create("remote", workspace_id="workspace-server")
    try:
        assert workspace.id == "workspace-server"
        assert any(item.id == workspace.id for item in manager.list())
        assert manager.get(workspace.id).state == "ready"
        server.transport.workspace_manager.claim(workspace.id, "active-run")
        with pytest.raises(WorkspaceActiveError):
            manager.delete(workspace.id)
        server.transport.workspace_manager.release(workspace.id, "active-run")
        assert manager.delete(workspace.id).state == "deleted"
    finally:
        server.transport.workspace_manager.release(workspace.id, "active-run")


def test_bearer_auth_and_remote_error_mapping(tmp_path):
    """Require bearer headers and map authentication failures to typed errors."""
    repo = _repo(tmp_path)
    server = AgentServer(
        str(repo),
        log_root=str(tmp_path / "auth-logs"),
        model=FinishModel(),
        token="test-token",
    )
    server.start()
    try:
        status, body, _ = _json_request(
            server,
            "POST",
            "/v1/query",
            {
                "request": "x",
                "repo_path": str(repo),
                "protocol_version": 1,
                "schema_version": 1,
            },
        )
        assert status == 401
        assert body["error"]["code"] == "authentication_required"
        remote = RemoteTransport(server.url, token="test-token")
        assert (
            remote.query({"request": "x", "repo_path": str(repo)}).status
            == "completed_unverified"
        )
    finally:
        server.stop()


def test_remote_version_client_maps_unsupported_response(tmp_path):
    """Map a server-side unsupported-version response to the SDK error type."""
    repo = _repo(tmp_path)
    server = AgentServer(
        str(repo), log_root=str(tmp_path / "version-logs"), model=FinishModel()
    )
    server.start()
    try:
        remote = RemoteTransport(server.url, protocol_version=2)
        with pytest.raises(UnsupportedVersionError):
            remote.query({"request": "x", "repo_path": str(repo)})
    finally:
        server.stop()


def _read_http_headers(sock):
    """Read WebSocket handshake headers and return them with buffered bytes."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    header, _, remainder = data.partition(b"\r\n\r\n")
    return header, remainder


def _read_frame(sock, initial=b""):
    """Read one server WebSocket frame from a socket."""
    data = initial

    def need(count):
        nonlocal data
        while len(data) < count:
            chunk = sock.recv(4096)
            if not chunk:
                raise AssertionError("websocket closed while reading frame")
            data += chunk

    need(2)
    first, second = data[0], data[1]
    length = second & 0x7F
    offset = 2
    if length == 126:
        need(4)
        length = int.from_bytes(data[2:4], "big")
        offset = 4
    elif length == 127:
        need(10)
        length = int.from_bytes(data[2:10], "big")
        offset = 10
    need(offset + length)
    payload = data[offset : offset + length]
    remaining = data[offset + length :]
    return first & 0x0F, payload, remaining


def _masked_frame(opcode, payload):
    """Encode a masked client WebSocket frame for protocol tests."""
    mask = os.urandom(4)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    else:
        header = bytes((0x80 | opcode, 0x80 | 126)) + length.to_bytes(2, "big")
    return header + mask + masked


def test_websocket_handshake_event_frame_and_close(server):
    """Upgrade to WebSocket, receive an event frame, and close cleanly."""
    remote = RemoteAgent(server.url, repo_path=server.transport.repo_path)
    try:
        result = remote.run("websocket", wait=True)
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        sock.settimeout(5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        separator = "\r\n"
        request = (
            f"GET /v1/runs/{result.run_id}/events HTTP/1.1{separator}"
            f"Host: 127.0.0.1:{server.port}{separator}"
            "Upgrade: websocket"
            f"{separator}Connection: Upgrade{separator}"
            f"Sec-WebSocket-Key: {key}{separator}"
            f"Sec-WebSocket-Version: 13{separator}"
            "X-Neo-Protocol-Version: 1"
            f"{separator}X-Neo-Schema-Version: 1{separator}{separator}"
        )
        sock.sendall(request.encode("ascii"))
        headers, remainder = _read_http_headers(sock)
        assert b"101 Switching Protocols" in headers
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        )
        assert b"Sec-WebSocket-Accept: " + accept in headers
        opcode, payload, remainder = _read_frame(sock, remainder)
        assert opcode == 1
        assert json.loads(payload.decode("utf-8"))["run_id"] == result.run_id
        sock.sendall(_masked_frame(8, struct.pack("!H", 1000)))
        sock.close()
    finally:
        remote.close()


def test_websocket_ping_and_cancel_messages(server):
    """Handle practical WebSocket ping and cancel control messages."""
    remote = RemoteAgent(server.url, repo_path=server.transport.repo_path)
    try:
        result = remote.run("websocket-controls", wait=True)
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        sock.settimeout(5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        separator = "\r\n"
        request = (
            f"GET /v1/runs/{result.run_id}/events HTTP/1.1{separator}"
            f"Host: 127.0.0.1:{server.port}{separator}"
            "Upgrade: websocket"
            f"{separator}Connection: Upgrade{separator}"
            f"Sec-WebSocket-Key: {key}{separator}"
            "Sec-WebSocket-Version: 13"
            f"{separator}X-Neo-Protocol-Version: 1"
            f"{separator}X-Neo-Schema-Version: 1{separator}{separator}"
        )
        sock.sendall(request.encode("ascii"))
        _read_http_headers(sock)
        sock.sendall(_masked_frame(9, b"ping"))
        sock.sendall(_masked_frame(1, b'{"type":"cancel"}'))
        sock.close()
    finally:
        remote.close()


def test_tool_catalog_routes_preserve_deferred_schema_and_remote_parity(tmp_path):
    """Expose catalog discovery and resolve schemas only through the explicit route."""
    calls = []

    async def loader(name):
        calls.append(name)
        return {"type": "object", "properties": {"value": {"type": "string"}}}

    catalog = ToolCatalog(
        [
            ToolDescriptor(
                "eager",
                "Eager tool",
                {"type": "object"},
                metadata={"api_key": "secret-value"},
            )
        ],
        [DeferredTool("lazy", "Lazy tool", loader=loader)],
    )
    instance = AgentServer(
        str(_repo(tmp_path)),
        log_root=str(tmp_path / "catalog-logs"),
        tool_catalog=catalog,
    )
    instance.start()
    remote = RemoteAgent(instance.url, repo_path=instance.transport.repo_path)
    try:
        assert [tool.name for tool in remote.list_tools()] == ["eager", "lazy"]
        assert [tool.name for tool in remote.search_tools("lazy")] == ["lazy"]
        assert remote.get_tool("lazy").deferred is True
        assert calls == []
        assert remote.resolve_tool_schema("lazy")["type"] == "object"
        assert calls == ["lazy"]
        status, body, _ = _json_request(
            instance,
            "GET",
            "/v1/tools/eager",
            headers={"X-Neo-Protocol-Version": "1", "X-Neo-Schema-Version": "1"},
        )
        assert status == 200
        assert body["tool"]["name"] == "eager"
        assert "secret-value" not in json.dumps(body)
        with pytest.raises(ToolNotFoundError):
            remote.get_tool("missing")
    finally:
        remote.close()
        instance.stop()


def test_server_hooks_block_remote_runs_before_model_execution(tmp_path):
    """Apply task-before denial to server-owned local transport runs."""
    calls = []
    hooks = HookManager()
    hooks.register(
        HookPoint.TASK_BEFORE,
        lambda context: HookDecision.deny("server policy"),
    )

    def model(messages, **kwargs):
        calls.append(True)
        return json.dumps({"tool": "finish", "answer": "late"})

    instance = AgentServer(
        str(_repo(tmp_path)),
        log_root=str(tmp_path / "hook-logs"),
        model=model,
        hooks=hooks,
    )
    instance.start()
    remote = RemoteAgent(instance.url, repo_path=instance.transport.repo_path)
    try:
        result = remote.query("blocked remotely")
        assert result.status == "blocked"
        assert calls == []
        assert any(record.point == HookPoint.TASK_BEFORE for record in hooks.records)
    finally:
        remote.close()
        instance.stop()


def test_tool_routes_without_catalog_are_empty_and_exact_lookup_is_404(server):
    """Do not fabricate built-in tools when no catalog is injected."""
    remote = RemoteTransport(server.url)
    try:
        assert remote.list_tools() == []
        assert remote.search_tools("read") == []
        with pytest.raises(ToolNotFoundError):
            remote.get_tool("read")
    finally:
        remote.close()
