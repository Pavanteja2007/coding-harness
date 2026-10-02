"""Offline and local stdio regression tests for integrations."""

from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from integrations.mcp import (
    MCPAdapter,
    MCPTimeoutError,
    MCPTransportConfig,
    MCPTransportError,
    normalize_tool_result,
    safe_child_environment,
)
from integrations.oauth import (
    FileTokenStorage,
    OAuthCallbackError,
    OAuthConfig,
    OAuthManager,
    OAuthStateExpiredError,
    OAuthStateMismatchError,
    OAuthStateReplayError,
    OAuthToken,
)
from integrations.tools import (
    DeferredTool,
    RequiredToolsError,
    SchemaValidationError,
    ToolCatalog,
    ToolDescriptor,
    ToolResolutionError,
    validate_json_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeSession:
    """Deterministic injected MCP session for lifecycle tests."""

    def __init__(self, events: list[str], *, initialize_delay: float = 0.0) -> None:
        """Create a fake session with an optional initialization delay."""
        self.events = events
        self.initialize_delay = initialize_delay

    async def __aenter__(self) -> "FakeSession":
        """Record session entry."""
        self.events.append("session-enter")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """Record session exit."""
        self.events.append("session-exit")
        return False

    async def initialize(self) -> dict[str, bool]:
        """Return a stable initialization response."""
        if self.initialize_delay:
            await asyncio.sleep(self.initialize_delay)
        self.events.append("initialize")
        return {"ok": True}

    async def list_tools(self) -> dict[str, list[dict[str, object]]]:
        """Return one SDK-shaped tool definition."""
        return {
            "tools": [
                {
                    "name": "echo",
                    "description": "Return a value",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                }
            ]
        }

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        """Return a stable SDK-shaped tool result."""
        return {
            "content": [{"type": "text", "text": f"{name}:{arguments['value']}"}],
            "isError": False,
        }


class Unauthorized(Exception):
    """Injected HTTP-like failure used to test one retry only."""

    status_code = 401


def connector_factory(
    events: list[str], *, fail_first: bool = False, attempts: list[int] | None = None
):
    """Build an injected connector factory with observable cleanup."""
    state = {"count": 0}

    @asynccontextmanager
    async def connector(config: MCPTransportConfig):
        """Open fake streams and optionally fail the first attempt."""
        state["count"] += 1
        if attempts is not None:
            attempts.append(state["count"])
        events.append(f"connector-enter-{state['count']}")
        if fail_first and state["count"] == 1:
            raise Unauthorized("401 unauthorized")
        try:
            yield object(), object()
        finally:
            events.append(f"connector-exit-{state['count']}")

    return connector


def session_factory(events: list[str], *, initialize_delay: float = 0.0):
    """Build an injected session factory."""
    return lambda read, write: FakeSession(events, initialize_delay=initialize_delay)


def run(coroutine):
    """Run one async test body on a fresh event loop."""
    return asyncio.run(coroutine)


def test_three_transport_configuration_paths_are_explicit():
    """stdio, SSE, and Streamable HTTP remain distinct immutable selections."""
    configs = [
        MCPTransportConfig(kind="stdio", command=(sys.executable, "-m", "mcp_server")),
        MCPTransportConfig(kind="sse", url="https://example.test/sse"),
        MCPTransportConfig(kind="streamable-http", url="https://example.test/mcp"),
    ]
    assert [config.transport for config in configs] == [
        "stdio",
        "sse",
        "streamable_http",
    ]
    assert configs[0].command == (sys.executable, "-m", "mcp_server")
    assert configs[1].endpoint == "https://example.test/sse"
    assert configs[2].endpoint == "https://example.test/mcp"
    assert configs[0].timeout_s == configs[1].timeout_s == configs[2].timeout_s


@pytest.mark.parametrize(
    ("kind", "module_name", "attribute"),
    [
        ("sse", "mcp.client.sse", "sse_client"),
        ("streamable_http", "mcp.client.streamable_http", "streamable_http_client"),
    ],
)
def test_http_transport_uses_official_sdk_factory(
    monkeypatch, kind, module_name, attribute
):
    """SSE and Streamable HTTP select the installed official SDK factory."""
    pytest.importorskip("mcp")
    module = importlib.import_module(module_name)
    calls = []

    @asynccontextmanager
    async def official_factory(*args, **kwargs):
        """Replace only the network boundary with an observable async context."""
        calls.append((args, kwargs))
        yield object(), object()

    monkeypatch.setattr(module, attribute, official_factory)
    events = []
    adapter = MCPAdapter(
        MCPTransportConfig(kind=kind, url="https://example.test/mcp"),
        session_factory=session_factory(events),
    )
    run(adapter.connect())
    run(adapter.close())
    assert calls
    assert calls[0][1]["url"] == "https://example.test/mcp"


def test_safe_argv_and_scrubbed_default_environment(monkeypatch):
    """Command parsing is shell-free and inherited credentials are removed."""
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    monkeypatch.setenv("SERVICE_TOKEN", "do-not-forward")
    assert (
        MCPTransportConfig(
            kind="stdio", command=f'"{sys.executable}" -m mcp_server'
        ).command[0]
        == sys.executable
    )
    child = safe_child_environment()
    assert "OPENAI_API_KEY" not in child
    assert "SERVICE_TOKEN" not in child
    projected = MCPTransportConfig(
        kind="stdio", command=(sys.executable, "server", "token=secret-value")
    ).to_dict()
    assert "secret-value" not in str(projected)
    with pytest.raises(ValueError):
        MCPTransportConfig(kind="stdio", command='python "unterminated')


def test_injected_adapter_normalizes_tools_and_result():
    """Injected streams exercise initialization, listing, calling, and cleanup."""
    events: list[str] = []
    config = MCPTransportConfig(kind="streamable_http", url="https://example.test/mcp")
    adapter = MCPAdapter(
        config,
        connector_factory=connector_factory(events),
        session_factory=session_factory(events),
    )

    async def exercise():
        async with adapter:
            tools = await adapter.list_tools()
            result = await adapter.call_tool("echo", {"value": "hello"})
        return tools, result

    tools, result = run(exercise())
    assert [tool.name for tool in tools] == ["echo"]
    assert tools[0].input_schema["required"] == ["value"]
    assert result["ok"] is True
    assert result["text"] == "echo:hello"
    assert events.index("session-exit") < events.index("connector-exit-1")
    assert adapter.closed is True


def test_real_stdio_roundtrip_against_local_mcp_server(tmp_path):
    """The default stdio path uses the official SDK against a tiny local server."""
    pytest.importorskip("mcp")
    server_script = tmp_path / "tiny_mcp_server.py"
    server_script.write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        "server = MCPServer('tiny-integration-server')\n"
        "@server.tool()\n"
        "def echo(value: str) -> str:\n"
        "    return 'echo:' + value\n"
        "if __name__ == '__main__':\n"
        "    server.run()\n",
        encoding="utf-8",
    )
    child_env = safe_child_environment()
    config = MCPTransportConfig(
        kind="stdio",
        command=(sys.executable, str(server_script)),
        cwd=REPO_ROOT,
        env=child_env,
        timeout_s=20.0,
    )

    async def exercise():
        async with MCPAdapter(config) as adapter:
            tools = await adapter.list_tools()
            result = await adapter.call_tool("echo", {"value": "stdio"})
        return tools, result

    tools, result = run(exercise())
    assert [tool.name for tool in tools] == ["echo"]
    assert result["ok"] is True
    assert result["text"] == "echo:stdio"


def test_transport_failure_is_typed_and_does_not_fallback():
    """A failed selected connector is surfaced without trying another transport."""
    attempts: list[int] = []

    async def failing(config: MCPTransportConfig):
        """Fail the selected injected connector."""
        attempts.append(1)
        raise RuntimeError("connection refused")

    adapter = MCPAdapter(
        MCPTransportConfig(kind="sse", url="https://example.test/sse"),
        connector_factory=failing,
        session_factory=lambda read, write: FakeSession([]),
    )
    with pytest.raises(MCPTransportError, match="connection refused"):
        run(adapter.connect())
    assert attempts == [1]
    assert adapter.connected is False


def test_asyncio_timeout_and_deterministic_cleanup():
    """Initialization deadlines are typed and close entered resources once."""
    events: list[str] = []
    adapter = MCPAdapter(
        MCPTransportConfig(
            kind="stdio", command=(sys.executable, "-m", "mcp_server"), timeout_s=0.01
        ),
        connector_factory=connector_factory(events),
        session_factory=session_factory(events, initialize_delay=0.2),
    )
    with pytest.raises(MCPTimeoutError):
        run(adapter.connect())
    assert "session-exit" in events
    assert "connector-exit-1" in events
    run(adapter.close())
    assert events.count("session-exit") == 1
    assert events.count("connector-exit-1") == 1


def test_mapping_result_is_bounded_and_redacted():
    """Tool result normalization bounds output and removes credential values."""
    result = normalize_tool_result(
        {
            "content": [{"type": "text", "text": "token=secret-value " + "x" * 2_000}],
            "structuredContent": {"access_token": "secret-value", "value": "ok"},
            "isError": False,
        },
        max_output_chars=128,
    )
    assert result["ok"] is True
    assert "secret-value" not in str(result)
    assert len(result["text"]) <= 128


class OAuthProvider:
    """Provider hook that returns no token before authorization."""

    def __init__(self) -> None:
        """Initialize provider call counters."""
        self.authorizations = 0

    async def get_access_token(self) -> None:
        """Return no initial bearer token."""
        return None

    def begin_authorization(self, session_id: str):
        """Return a safe authorization URL for the adapter retry lane."""
        self.authorizations += 1
        return f"https://auth.example.test/authorize?session={session_id}"


def test_adapter_retries_once_after_401_with_oauth_hook():
    """An HTTP 401 triggers exactly one provider initiation and one retry."""
    events: list[str] = []
    attempts: list[int] = []
    provider = OAuthProvider()
    adapter = MCPAdapter(
        MCPTransportConfig(kind="streamable_http", url="https://example.test/mcp"),
        connector_factory=connector_factory(events, fail_first=True, attempts=attempts),
        session_factory=session_factory(events),
        oauth_provider=provider,
    )

    async def exercise():
        await adapter.connect()
        await adapter.close()

    run(exercise())
    assert attempts == [1, 2]
    assert provider.authorizations == 1
    assert adapter.last_authorization_url is not None
    assert events.count("connector-enter-1") == 1
    assert events.count("connector-exit-2") == 1


def test_operation_401_retries_once_after_oauth_without_fallback():
    """A list operation can also trigger the single OAuth reconnect retry."""
    events: list[str] = []
    sessions = []

    class RetrySession(FakeSession):
        """Fail its first list operation with an injected 401."""

        def __init__(self, fail: bool) -> None:
            """Create a retrying fake session."""
            super().__init__(events)
            self.fail = fail

        async def list_tools(self):
            """Raise once, then return the normal tool response."""
            if self.fail:
                raise Unauthorized("401 unauthorized")
            return await super().list_tools()

    @asynccontextmanager
    async def connector():
        """Yield a fresh retrying session for each connection attempt."""
        session = RetrySession(not sessions)
        sessions.append(session)
        try:
            yield session
        finally:
            events.append("retry-connector-exit")

    provider = OAuthProvider()
    adapter = MCPAdapter(
        MCPTransportConfig(kind="streamable_http", url="https://example.test/mcp"),
        connector_factory=connector,
        oauth_provider=provider,
    )

    async def exercise():
        await adapter.connect()
        tools = await adapter.list_tools()
        await adapter.close()
        return tools

    tools = run(exercise())
    assert [tool.name for tool in tools] == ["echo"]
    assert len(sessions) == 2
    assert provider.authorizations == 1


def test_oauth_pkce_state_binding_and_one_time_callback():
    """PKCE, client/session binding, mismatch handling, and replay are enforced."""
    config = OAuthConfig(
        authorization_endpoint="https://auth.example.test/authorize",
        token_endpoint="https://auth.example.test/token",
        client_id="client-1",
        redirect_uri="https://app.example.test/callback",
        scope="read write",
    )
    manager = OAuthManager(config)
    authorization = manager.begin_authorization("session-1")
    query = parse_qs(urlparse(authorization.authorization_url).query)
    assert authorization.state not in repr(authorization)
    assert authorization.state not in str(authorization.to_diagnostic_dict())
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [authorization.code_challenge]
    callback = urlparse(authorization.authorization_url)
    valid = f"https://app.example.test/callback?code=abc&state={authorization.state}"
    with pytest.raises(OAuthStateMismatchError):
        manager.handle_callback(
            "https://app.example.test/callback?code=abc&state=wrong",
            session_id="session-1",
        )
    validated = manager.handle_callback(valid, session_id="session-1")
    assert validated.code == "abc"
    with pytest.raises(OAuthStateReplayError):
        manager.handle_callback(valid, session_id="session-1")
    assert callback.path == "/authorize"


def test_oauth_expiry_and_strict_redirect_session_binding():
    """Expired state and mismatched callback bindings fail closed."""
    now = [0.0]
    config = OAuthConfig(
        authorization_endpoint="https://auth.example.test/authorize",
        token_endpoint="https://auth.example.test/token",
        client_id="client-1",
        redirect_uri="https://app.example.test/callback",
        state_ttl_s=1.0,
    )
    manager = OAuthManager(config, monotonic_clock=lambda: now[0])
    expired = manager.begin_authorization("session-1")
    now[0] = 2.0
    with pytest.raises(OAuthStateExpiredError):
        manager.handle_callback(
            f"https://app.example.test/callback?code=abc&state={expired.state}",
            session_id="session-1",
        )
    active = manager.begin_authorization("session-2")
    with pytest.raises(OAuthCallbackError):
        manager.handle_callback(
            f"https://app.example.test/other?code=abc&state={active.state}",
            session_id="session-2",
        )


def test_oauth_refresh_and_token_storage_redaction(tmp_path):
    """Refresh grants preserve refresh tokens and storage keeps values private."""
    requests = []

    async def exchanger(request):
        """Return a short-lived token for an injected exchange."""
        requests.append(request)
        return {"access_token": "access-secret", "expires_in": 0}

    config = OAuthConfig(
        authorization_endpoint="https://auth.example.test/authorize",
        token_endpoint="https://auth.example.test/token",
        client_id="client-1",
        redirect_uri="https://app.example.test/callback",
    )
    storage = FileTokenStorage(tmp_path / "private" / "tokens.json")
    manager = OAuthManager(config, token_storage=storage, token_exchanger=exchanger)
    token = OAuthToken("access-secret", "refresh-secret", expires_in=0)
    run(storage.set(token, "owner"))
    loaded = run(storage.get("owner"))
    assert loaded is not None
    assert loaded.access_token == "access-secret"
    assert "access-secret" not in repr(loaded)
    assert "access-secret" not in repr(storage)
    refreshed = run(manager.refresh("owner", force=True))
    assert refreshed.access_token == "access-secret"
    assert refreshed.refresh_token == "refresh-secret"
    assert requests[-1].grant_type == "refresh_token"
    assert "refresh-secret" not in repr(requests[-1])


def test_tool_search_deferred_loading_and_required_validation():
    """Catalog search stays metadata-only while deferred schemas load explicitly."""
    calls = []

    async def loader(name: str):
        """Load a required-field schema only when requested."""
        calls.append(name)
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

    eager = ToolDescriptor(
        "read_file",
        "Read a repository file",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        tags=("files", "read"),
    )
    deferred = DeferredTool(
        "write_file", "Write a repository file", ("files", "write"), loader=loader
    )
    catalog = ToolCatalog([eager], [deferred])
    search = catalog.search("repository", max_results=1)
    assert search[0].name == "read_file"
    assert search[0].deferred is False
    assert len(catalog.search("", max_results=1)) == 1
    assert calls == []
    assert catalog.resolve("write_file").input_schema == {}
    assert calls == []
    schema = run(catalog.resolve_schema("write_file"))
    assert schema["required"] == ["path"]
    assert calls == ["write_file"]
    with pytest.raises(SchemaValidationError):
        validate_json_schema({}, {"type": "object", "required": ["path"]})
    with pytest.raises(RequiredToolsError):
        catalog.validate_required_tools(["missing_tool"])


def test_deferred_failure_is_typed_and_bounded():
    """A failing schema loader never executes a tool or exposes raw errors."""

    async def failing_loader(name: str):
        """Fail with a secret-shaped diagnostic."""
        raise RuntimeError("access_token=secret-value " + "x" * 2_000)

    tool = DeferredTool("danger", "Danger tool", loader=failing_loader)
    with pytest.raises(ToolResolutionError) as raised:
        run(tool.resolve_schema())
    assert "secret-value" not in str(raised.value)
    assert len(str(raised.value)) < 700
