"""ACP v1 server adapter for duck-typed public agent SDK surfaces."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import os
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from shared.security import redact_text

from ._compat import (
    ImmediateAwaitable,
    call_maybe_async,
    decode_wire_message,
    run_sync,
)
from .models import (
    ACP_ERROR_AUTH_REQUIRED,
    ACP_ERROR_INTERNAL,
    ACP_ERROR_INVALID_PARAMS,
    ACP_ERROR_INVALID_REQUEST,
    ACP_ERROR_METHOD_NOT_FOUND,
    ACP_ERROR_NOT_INITIALIZED,
    JSONRPC_VERSION,
    PROTOCOL_VERSION,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    VALID_STOP_REASONS,
    ACPCapabilities,
    ACPError,
    ACPPromptResult,
    ACPProtocolError,
    ACPRequest,
    ACPResponse,
    ACPSession,
    ACPTimeoutError,
    ACPTransportClosed,
    ACPTransportError,
    auth_method_is_terminal,
    auth_method_requires_protocol_auth,
    clean_verification_evidence,
    is_terminal_result,
    negotiate_protocol_version,
    normalize_prompt,
    normalize_protocol_version,
    normalize_status,
    normalize_update,
    result_answer,
    stop_reason_for_status,
    validate_session_id,
)
from .transport import ACPTransport


class PublicAgent(Protocol):
    """Structural documentation for the optional public agent methods used here."""

    def query(self, prompt: Any, **kwargs: Any) -> Any:
        """Process one prompt through a public query operation."""

    def run(self, prompt: Any, **kwargs: Any) -> Any:
        """Process one prompt through a public run operation."""

    def stream(self, prompt: Any, **kwargs: Any) -> Any:
        """Yield public prompt chunks through a public stream operation."""

    def cancel(self, **kwargs: Any) -> Any:
        """Request cancellation through the public agent API."""

    def replay(self, **kwargs: Any) -> Any:
        """Replay a public agent stream when supported."""

    def close(self) -> Any:
        """Release public agent resources."""


_MAX_REQUEST_IDS = 4096
_RECEIVE_POLL_SECONDS = 0.1
_CANCEL_GRACE_SECONDS = 0.2

#: Sentinel returned by ``next(iterator, _ITERATOR_DONE)`` at end of stream.
#: Never raised, so it is safe to cross a Future/thread boundary.
_ITERATOR_DONE: Any = object()

#: Cap on the string form substituted for a non-projected public object.
_MAX_PUBLIC_STRING = 4000


def _json_safe_public(value: Any, _depth: int = 0) -> Any:
    """Project an arbitrary public agent value into JSON-safe data.

    A JSON-RPC server that puts a non-serializable object in a result does
    not degrade - it fails to answer, and the peer reports a protocol error
    with no cause. A real `agent_sdk.Event` terminal item is exactly that
    case: the first version of this adapter passed it through and every
    `session/prompt` against a real agent died in ``json.dumps``.

    Primitives pass through. Mappings and ``to_dict()`` objects are walked
    recursively. Anything else is represented by its bounded string form,
    which is lossy but honest and always parseable.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
            return str(value)
        return value
    if _depth >= 6:
        return str(value)[:_MAX_PUBLIC_STRING]
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe_public(item, _depth + 1)
            for key, item in list(value.items())[:64]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe_public(item, _depth + 1) for item in list(value)[:64]]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe_public(to_dict(), _depth + 1)
        except Exception:
            return str(value)[:_MAX_PUBLIC_STRING]
    as_dict = getattr(value, "__dict__", None)
    if isinstance(as_dict, dict) and as_dict:
        public = [
            (str(key), item)
            for key, item in list(as_dict.items())
            if not str(key).startswith("_")
        ]
        return {key: _json_safe_public(item, _depth + 1) for key, item in public[:64]}
    return str(value)[:_MAX_PUBLIC_STRING]


@dataclass
class _SessionRuntime:
    """Mutable server state for one ACP session."""

    session_id: str
    cwd: str
    modes: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task[Any] | None = None
    request_id: int | str | None = None
    run_handle: Any = None
    cancel_requested: bool = False
    cancel_sent: bool = False
    handle_cancel_sent: bool = False
    finalized: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ACPServer:
    """Serve ACP v1 by adapting only public agent SDK methods.

    ``agent`` may be an Agent, Conversation, or a compatible object.  The
    adapter calls only public ``query``, ``run``, ``stream``, ``cancel``,
    ``replay``, and ``close`` attributes; it never imports an agent SDK
    implementation module or reaches into private state.
    """

    def __init__(
        self,
        agent: Any = None,
        transport: ACPTransport | Any = None,
        *,
        protocol_version: int = PROTOCOL_VERSION,
        version: int | None = None,
        supported_versions: Sequence[int] | None = None,
        capabilities: Mapping[str, Any] | ACPCapabilities | None = None,
        required_capabilities: Sequence[str] = (),
        auth_methods: Sequence[Mapping[str, Any] | str] | None = None,
        auth_required: bool | None = None,
        allow_localhost: bool = True,
        allow_non_loopback: bool = False,
        auth_host: str | None = None,
        authenticator: Callable[..., Any] | None = None,
        permission_handler: Callable[..., Any] | None = None,
        permission_hook: Callable[..., Any] | None = None,
        on_permission_request: Callable[..., Any] | None = None,
        on_permission: Callable[..., Any] | None = None,
        on_permission_response: Callable[..., Any] | None = None,
        permission_response_handler: Callable[..., Any] | None = None,
        permission_response: Callable[..., Any] | None = None,
        agent_info: Mapping[str, Any] | None = None,
        modes: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        session_factory: Callable[..., Any] | None = None,
        timeout_s: float = 30.0,
        timeout: float | None = None,
        owns_transport: bool = True,
    ) -> None:
        """Create a server adapter with injectable transport and public agent."""
        if _looks_like_transport(agent) and not _looks_like_transport(transport):
            agent, transport = transport, agent
        self.agent = agent
        self.transport = transport
        if version is not None:
            protocol_version = version
        if timeout is not None:
            timeout_s = timeout
        self.protocol_version = normalize_protocol_version(protocol_version)
        self.supported_versions = frozenset(
            {self.protocol_version}
            if supported_versions is None
            else {
                item
                for item in supported_versions
                if isinstance(item, int)
                and not isinstance(item, bool)
                and item == PROTOCOL_VERSION
            }
        )
        if not self.supported_versions:
            raise ACPProtocolError(
                -32000, "ACP server has no supported protocol versions"
            )
        if auth_required is not None and not isinstance(auth_required, bool):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "auth_required must be a boolean"
            )
        self.capabilities = _capability_mapping(capabilities)
        declared_required = self.capabilities.get(
            "requiredCapabilities", self.capabilities.get("required_capabilities")
        )
        if declared_required is not None:
            _validate_required_capability_names(declared_required)
        required_values = (
            [required_capabilities]
            if isinstance(required_capabilities, str)
            else (required_capabilities or ())
        )
        self.required_capabilities = tuple(str(item) for item in required_values)
        _validate_required_capability_names(self.required_capabilities)
        self.auth_methods = _auth_methods(auth_methods)
        self._auth_required_explicit = auth_required is not None
        self._auth_required_value = (
            bool(auth_required) if auth_required is not None else None
        )
        self.allow_localhost = bool(allow_localhost)
        self.allow_non_loopback = bool(allow_non_loopback)
        self.auth_host = str(auth_host or "")
        self.auth_required = (
            any(auth_method_requires_protocol_auth(item) for item in self.auth_methods)
            if auth_required is None
            else bool(auth_required)
        )
        self.authenticator = authenticator
        self.permission_handler = (
            permission_handler
            or permission_hook
            or on_permission_request
            or on_permission
        )
        self.on_permission_request = self.permission_handler
        self.permission_response_handler = (
            permission_response_handler or permission_response or on_permission_response
        )
        self.on_permission_response = self.permission_response_handler
        self.agent_info = dict(agent_info or {"name": "neo-acp-server", "version": "1"})
        self.modes = _modes(modes)
        self.session_factory = session_factory
        self.timeout_s = max(0.01, float(timeout_s))
        self.owns_transport = bool(owns_transport)
        self.initialized = False
        self.authenticated = not self.auth_required
        self.closed = False
        self.last_error = ""
        self.negotiated_version: int | None = None
        self.sessions: dict[str, _SessionRuntime] = {}
        self._request_runtimes: dict[int | str, _SessionRuntime] = {}
        self._pending_session_requests: dict[str, int | str] = {}
        self._active_request_ids: set[int | str] = set()
        self._seen_request_ids: set[int | str] = set()
        self._seen_request_order: deque[int | str] = deque()
        self._completed_request_ids: set[int | str] = set()
        self._duplicate_error_ids: set[int | str] = set()
        self._cancelled_request_ids: set[int | str] = set()
        self._outbound_permissions: dict[int | str, asyncio.Future[Any]] = {}
        self._permission_sessions: dict[int | str, str] = {}
        self._permission_payloads: dict[int | str, dict[str, Any]] = {}
        self._next_permission_id = 1
        self._reader_task: asyncio.Task[Any] | None = None
        self._request_tasks: set[asyncio.Task[Any]] = set()
        self._cancel_tasks: set[asyncio.Task[Any]] = set()
        self._closed_event: asyncio.Event | None = None
        self._reader_started = False

    @property
    def protocolVersion(self) -> int | None:
        """Return the negotiated protocol version, if any."""
        return self.negotiated_version

    @property
    def capabilities_model(self) -> ACPCapabilities:
        """Return this server's advertised capabilities as a typed model."""
        return ACPCapabilities(
            protocol_version=self.negotiated_version or self.protocol_version,
            agent_capabilities=self.capabilities,
            auth_methods=self.auth_methods,
            agent_info=self.agent_info,
            auth_required=self.auth_required,
        )

    async def _start_transport(self) -> None:
        """Start the transport and reader loop once."""
        if self.closed:
            raise ACPTransportClosed(-32000, "ACP server is closed")
        if self.transport is None:
            raise ACPProtocolError(
                ACP_ERROR_INTERNAL, "ACP server requires a transport"
            )
        if not self._reader_started:
            starter = getattr(self.transport, "start", None)
            if callable(starter):
                await call_maybe_async(self.transport, "start")
            self._reader_started = True
            self._closed_event = asyncio.Event()
            self._reader_task = asyncio.create_task(self._reader_loop())

    async def start(self) -> "ACPServer":
        """Start serving incoming ACP messages."""
        await self._start_transport()
        return self

    async def serve_forever(self) -> None:
        """Serve until the transport closes or the server is closed."""
        await self.start()
        event = self._closed_event
        if event is not None:
            await event.wait()

    run = serve_forever
    serve = serve_forever

    def _require_initialized(self) -> None:
        """Raise when a direct server operation is used before negotiation."""
        if not self.initialized:
            raise ACPProtocolError(
                ACP_ERROR_NOT_INITIALIZED, "ACP server is not initialized"
            )

    async def initialize(
        self,
        protocol_version: int = PROTOCOL_VERSION,
        **params: Any,
    ) -> dict[str, Any]:
        """Negotiate initialization directly for embedding callers."""
        values = {"protocolVersion": protocol_version, **dict(params)}
        return await self._initialize(values)

    async def authenticate(self, method_id: str, **params: Any) -> dict[str, Any]:
        """Authenticate directly through the declared public method."""
        self._require_initialized()
        return await self._authenticate({"methodId": method_id, **dict(params)})

    async def new_session(self, cwd: str, mcp_servers: Any = None) -> dict[str, Any]:
        """Create a session directly through the server adapter."""
        self._require_initialized()
        return await self._new_session(
            {"cwd": cwd, "mcpServers": list(mcp_servers or [])}
        )

    create_session = new_session

    async def prompt(self, session_id: str, prompt: Any) -> dict[str, Any]:
        """Run one prompt directly and return its terminal wire result."""
        self._require_initialized()
        request = ACPRequest(
            id=0,
            method="session/prompt",
            params={"sessionId": session_id, "prompt": normalize_prompt(prompt)},
        )
        try:
            return await self._run_prompt_request(request.params)
        finally:
            self._clear_prompt_runtime(request)

    async def cancel_session(self, session_id: str) -> bool:
        """Mark a session cancelled through the public agent cancellation path."""
        return await self._cancel_session({"sessionId": session_id})

    async def send_update(self, session_id: str, update: Any) -> None:
        """Send one public session/update notification from an embedding caller."""
        await self._send_update(validate_session_id(session_id), update)

    async def _reader_loop(self) -> None:
        """Read requests while keeping an idle connection alive."""
        fatal = False
        try:
            while not self.closed:
                try:
                    message = await self._receive_transport()
                except (ACPTimeoutError, TimeoutError) as exc:
                    if "timed out" not in redact_text(
                        str(exc)
                    ).lower() and not isinstance(exc, TimeoutError):
                        raise
                    await asyncio.sleep(0)
                    continue
                except ACPTransportError as exc:
                    if "timed out" not in redact_text(str(exc)).lower():
                        raise
                    await asyncio.sleep(0)
                    continue
                except ACPProtocolError as exc:
                    await self._send_error(None, exc)
                    continue
                if message is None:
                    if bool(getattr(self.transport, "closed", False)):
                        fatal = True
                        break
                    await asyncio.sleep(0.001)
                    continue
                await self._route_raw_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fatal = True
            self.last_error = redact_text(str(exc))
        finally:
            if fatal and not self.closed:
                await self.aclose()
            if self._closed_event is not None:
                self._closed_event.set()

    async def _receive_transport(self) -> Mapping[str, Any] | None:
        """Receive one raw message through an available transport read method."""
        for name in ("areceive", "receive", "recv", "read", "receive_message"):
            method = getattr(self.transport, name, None)
            if not callable(method):
                continue
            try:
                value = await call_maybe_async(
                    self.transport, name, _RECEIVE_POLL_SECONDS
                )
                try:
                    return decode_wire_message(value)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ACPProtocolError(
                        -32700, "ACP transport emitted invalid JSON or UTF-8"
                    ) from exc
            except TypeError as exc:
                try:
                    value = await call_maybe_async(self.transport, name)
                    try:
                        return decode_wire_message(value)
                    except UnicodeDecodeError as exc:
                        raise ACPProtocolError(
                            -32700, "ACP transport emitted invalid UTF-8"
                        ) from exc
                except TypeError:
                    raise exc from None
        raise ACPProtocolError(
            ACP_ERROR_INTERNAL, "ACP transport cannot receive messages"
        )

    async def _send_transport(self, message: Mapping[str, Any]) -> None:
        """Send one raw message through an available transport method."""
        for name in ("asend", "send", "write", "send_message", "write_message"):
            method = getattr(self.transport, name, None)
            if callable(method):
                argument: Any = dict(message)
                try:
                    parameters = list(inspect.signature(method).parameters.values())
                except (TypeError, ValueError):
                    parameters = []
                if parameters:
                    first = parameters[0]
                    if first.annotation in {str, "str"} or first.name in {
                        "line",
                        "raw",
                        "text",
                    }:
                        try:
                            argument = (
                                json.dumps(
                                    dict(message), ensure_ascii=False, allow_nan=False
                                )
                                + "\n"
                            )
                        except (TypeError, ValueError) as exc:
                            raise ACPProtocolError(
                                -32602, "ACP message is not valid JSON"
                            ) from exc
                await call_maybe_async(self.transport, name, argument)
                return
        raise ACPProtocolError(ACP_ERROR_INTERNAL, "ACP transport cannot send messages")

    async def _route_raw_message(self, message: Any) -> None:
        """Validate a raw envelope and dispatch requests, responses, or notifications."""
        if not isinstance(message, Mapping):
            await self._send_error(
                None,
                ACPProtocolError(
                    ACP_ERROR_INVALID_REQUEST, "ACP message must be an object"
                ),
            )
            return
        if "jsonrpc" not in message or message.get("jsonrpc") != JSONRPC_VERSION:
            await self._send_error(
                _safe_response_id(message.get("id")),
                ACPProtocolError(
                    ACP_ERROR_INVALID_REQUEST, "unsupported JSON-RPC version"
                ),
            )
            return
        if "method" not in message:
            if "result" in message or "error" in message:
                await self._route_response(message)
            else:
                await self._send_error(
                    _safe_response_id(message.get("id")),
                    ACPProtocolError(
                        ACP_ERROR_INVALID_REQUEST, "JSON-RPC method is required"
                    ),
                )
            return
        has_id = "id" in message
        request_id = message.get("id")
        if has_id and (
            request_id is None
            or isinstance(request_id, bool)
            or not isinstance(request_id, (int, str))
        ):
            await self._send_error(
                _safe_response_id(request_id),
                ACPProtocolError(
                    ACP_ERROR_INVALID_REQUEST, "JSON-RPC request id is invalid"
                ),
            )
            return
        try:
            request = ACPRequest.from_dict(message)
        except ACPError as exc:
            await self._send_error(_safe_response_id(request_id), exc)
            return
        if not has_id:
            if request.method not in {
                "session/cancel",
                "$/cancel_request",
                "session/update",
                "elicitation/complete",
            }:
                await self._send_error(
                    None,
                    ACPProtocolError(
                        ACP_ERROR_INVALID_REQUEST, "JSON-RPC request id is required"
                    ),
                )
                return
            try:
                if request.method == "session/cancel":
                    await self._cancel_session(request.params)
                elif request.method == "$/cancel_request":
                    await self._cancel_request(request.params)
            except ACPError as exc:
                self.last_error = exc.message
            return
        if not self._remember_request_id(request_id):
            if request_id not in self._duplicate_error_ids:
                self._duplicate_error_ids.add(request_id)
                await self._send_error(
                    request_id,
                    ACPProtocolError(
                        ACP_ERROR_INVALID_REQUEST, "duplicate JSON-RPC request id"
                    ),
                    force=True,
                )
            return
        self._active_request_ids.add(request_id)
        if request.method == "session/prompt":
            session_value = request.params.get(
                "sessionId", request.params.get("session_id", "")
            )
            if isinstance(session_value, str) and session_value:
                self._pending_session_requests[session_value] = request_id
        task = asyncio.create_task(self._process_request(request))
        self._request_tasks.add(task)
        task.add_done_callback(self._request_tasks.discard)

    async def _route_response(self, message: Mapping[str, Any]) -> None:
        """Route a peer response to an outstanding server-originated request."""
        if message.get("jsonrpc") != JSONRPC_VERSION:
            self.last_error = "ACP peer response has an unsupported JSON-RPC version"
            return
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            self.last_error = "ACP peer response id is invalid"
            return
        future = self._outbound_permissions.pop(request_id, None)
        self._permission_sessions.pop(request_id, None)
        permission_params = self._permission_payloads.pop(request_id, {})
        if future is None or future.done():
            return
        try:
            parsed = ACPResponse.from_dict(message)
        except ACPError as exc:
            future.set_exception(exc)
            return
        if parsed.error is not None:
            future.set_exception(parsed.error)
        else:
            if self.permission_response_handler is not None:
                try:
                    replacement = await _invoke_callback(
                        self.permission_response_handler,
                        {
                            "id": request_id,
                            "params": permission_params,
                        },
                        parsed.result,
                    )
                    options = permission_params.get("options", [])
                    if replacement is not None and isinstance(options, list):
                        parsed.result = _permission_result(replacement, options)
                except Exception as exc:
                    self.last_error = redact_text(str(exc))
            future.set_result(parsed.result)

    def _remember_request_id(self, request_id: int | str) -> bool:
        """Remember request IDs and reject reuse for the connection lifetime."""
        if request_id in self._seen_request_ids:
            return False
        self._seen_request_ids.add(request_id)
        self._seen_request_order.append(request_id)
        while len(self._seen_request_order) > _MAX_REQUEST_IDS:
            self._seen_request_ids.discard(self._seen_request_order.popleft())
        return True

    @staticmethod
    def _safe_response_id(value: Any) -> int | str | None:
        """Return a response-safe ID for malformed incoming envelopes."""
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        return value

    async def _process_request(self, request: ACPRequest) -> None:
        """Dispatch one request and send exactly one response."""
        try:
            try:
                if not self.initialized and request.method != "initialize":
                    if not _is_known_method(request.method):
                        raise ACPError(
                            ACP_ERROR_METHOD_NOT_FOUND,
                            f"unknown ACP method: {request.method}",
                        )
                    raise ACPError(
                        ACP_ERROR_NOT_INITIALIZED, "ACP server is not initialized"
                    )
                result = await self._dispatch_request(request)
            except ACPError as exc:
                await self._send_error(request.id, exc)
                return
            except Exception as exc:
                await self._send_error(
                    request.id,
                    ACPError(ACP_ERROR_INTERNAL, redact_text(str(exc))),
                )
                return
            await self._send_result(request.id, result)
            if request.method in {"shutdown", "exit", "$/shutdown"}:
                await self.aclose()
        finally:
            self._active_request_ids.discard(request.id)
            self._clear_prompt_runtime(request)

    def _clear_prompt_runtime(self, request: ACPRequest) -> None:
        """Release a prompt task only after its terminal response is sent."""
        if request.method != "session/prompt":
            return
        session_id = str(
            request.params.get("sessionId", request.params.get("session_id", "")) or ""
        )
        runtime = self.sessions.get(session_id)
        self._pending_session_requests.pop(session_id, None)
        if runtime is not None and runtime.task is asyncio.current_task():
            runtime.task = None
        if runtime is not None and runtime.request_id == request.id:
            self._request_runtimes.pop(request.id, None)
            runtime.request_id = None
            runtime.run_handle = None

    async def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Process one message directly with the same identity rules as stdio."""
        if not isinstance(message, Mapping):
            return await self._handle_direct_error(
                None,
                ACPProtocolError(
                    ACP_ERROR_INVALID_REQUEST, "ACP message must be an object"
                ),
            )
        if "jsonrpc" not in message or message.get("jsonrpc") != JSONRPC_VERSION:
            return await self._handle_direct_error(
                _safe_response_id(message.get("id")),
                ACPProtocolError(
                    ACP_ERROR_INVALID_REQUEST, "unsupported JSON-RPC version"
                ),
            )
        if "method" not in message:
            if "result" in message or "error" in message:
                await self._route_response(message)
            else:
                return await self._handle_direct_error(
                    _safe_response_id(message.get("id")),
                    ACPProtocolError(
                        ACP_ERROR_INVALID_REQUEST, "JSON-RPC method is required"
                    ),
                )
            return None
        has_id = "id" in message
        request_id = message.get("id")
        if has_id and (
            request_id is None
            or isinstance(request_id, bool)
            or not isinstance(request_id, (int, str))
        ):
            return await self._handle_direct_error(
                _safe_response_id(request_id),
                ACPProtocolError(
                    ACP_ERROR_INVALID_REQUEST, "JSON-RPC request id is invalid"
                ),
            )
        try:
            request = ACPRequest.from_dict(message)
        except ACPError as exc:
            return await self._handle_direct_error(_safe_response_id(request_id), exc)
        if not has_id:
            if request.method not in {
                "session/cancel",
                "$/cancel_request",
                "session/update",
                "elicitation/complete",
            }:
                return await self._handle_direct_error(
                    None,
                    ACPProtocolError(
                        ACP_ERROR_INVALID_REQUEST, "JSON-RPC request id is required"
                    ),
                )
            try:
                if request.method == "session/cancel":
                    await self._cancel_session(request.params)
                elif request.method == "$/cancel_request":
                    await self._cancel_request(request.params)
            except ACPError as exc:
                self.last_error = exc.message
            return None
        if not self._remember_request_id(request_id):
            return await self._handle_direct_error(
                request_id,
                ACPProtocolError(
                    ACP_ERROR_INVALID_REQUEST, "duplicate JSON-RPC request id"
                ),
            )
        self._active_request_ids.add(request_id)
        try:
            try:
                if not self.initialized and request.method != "initialize":
                    if not _is_known_method(request.method):
                        raise ACPError(
                            ACP_ERROR_METHOD_NOT_FOUND,
                            f"unknown ACP method: {request.method}",
                        )
                    raise ACPError(
                        ACP_ERROR_NOT_INITIALIZED, "ACP server is not initialized"
                    )
                result = await self._dispatch_request(request)
            except ACPError as exc:
                return await self._handle_direct_error(request.id, exc)
            except Exception as exc:
                return await self._handle_direct_error(
                    request.id,
                    ACPError(ACP_ERROR_INTERNAL, redact_text(str(exc))),
                )
            response = ACPResponse(id=request.id, result=result).to_dict()
            if request.method in {"shutdown", "exit", "$/shutdown"}:
                await self.aclose()
            return response
        finally:
            self._active_request_ids.discard(request.id)
            self._clear_prompt_runtime(request)

    handle_request = handle_message

    async def _handle_direct_error(
        self, request_id: Any, error: ACPError
    ) -> dict[str, Any]:
        """Return a redacted direct-handler error response."""
        return ACPResponse(id=_safe_response_id(request_id), error=error).to_dict()

    async def _dispatch_request(self, request: ACPRequest) -> Any:
        """Route a validated request to one server operation."""
        method = request.method
        if method == "initialize":
            return await self._initialize(request.params)
        if method == "authenticate":
            return await self._authenticate(request.params)
        if method == "session/new":
            return await self._new_session(request.params)
        if method == "session/prompt":
            return await self._run_prompt_request(request.params, request_id=request.id)
        if method == "session/set_mode":
            return await self._set_mode(request.params)
        if method in {
            "session/cancel",
            "$/cancel_request",
            "shutdown",
            "exit",
            "$/shutdown",
        }:
            if method == "session/cancel":
                if not await self._cancel_session(request.params):
                    raise ACPProtocolError(
                        ACP_ERROR_INVALID_PARAMS, "unknown ACP session"
                    )
            elif method == "$/cancel_request":
                await self._cancel_request(request.params)
            return {}
        if method == "ping":
            return {}
        raise ACPError(ACP_ERROR_METHOD_NOT_FOUND, f"unknown ACP method: {method}")

    async def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Negotiate stable ACP v1 and reject unsupported client requests."""
        if self.initialized:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_REQUEST, "ACP connection is already initialized"
            )
        if "protocolVersion" not in params and "protocol_version" not in params:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "initialize protocolVersion is required"
            )
        requested = params.get("protocolVersion", params.get("protocol_version"))
        if "protocolVersions" in params:
            requested = params.get("protocolVersions")
        try:
            version = negotiate_protocol_version(requested, self.supported_versions)
        except ACPError:
            raise
        client_capabilities = params.get("clientCapabilities", {})
        if client_capabilities is None:
            client_capabilities = {}
        if not isinstance(client_capabilities, Mapping):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "clientCapabilities must be an object"
            )
        client_capabilities = dict(client_capabilities)
        nested_required = client_capabilities.get(
            "requiredCapabilities", client_capabilities.get("required_capabilities")
        )
        if nested_required is not None:
            if not isinstance(nested_required, Sequence) or isinstance(
                nested_required, (str, bytes, bytearray)
            ):
                raise ACPProtocolError(
                    ACP_ERROR_INVALID_PARAMS, "requiredCapabilities must be an array"
                )
            _validate_required_capabilities(nested_required, client_capabilities)
        _validate_required_capabilities(self.required_capabilities, client_capabilities)
        requested_required = params.get(
            "requiredCapabilities", params.get("required_capabilities", ())
        )
        if requested_required is not None:
            if not isinstance(requested_required, Sequence) or isinstance(
                requested_required, (str, bytes, bytearray)
            ):
                raise ACPProtocolError(
                    ACP_ERROR_INVALID_PARAMS, "requiredCapabilities must be an array"
                )
            _validate_required_capabilities(requested_required, client_capabilities)
        self.client_capabilities = client_capabilities
        declared_required = self.capabilities.get(
            "requiredCapabilities", self.capabilities.get("required_capabilities")
        )
        if declared_required is not None:
            if not isinstance(declared_required, Sequence) or isinstance(
                declared_required, (str, bytes, bytearray)
            ):
                raise ACPProtocolError(
                    ACP_ERROR_INVALID_PARAMS, "requiredCapabilities must be an array"
                )
            _validate_required_capabilities(declared_required, self.capabilities)
        terminal_supported = _capability_enabled(client_capabilities, "auth.terminal")
        visible_auth = [
            dict(item)
            for item in self.auth_methods
            if not auth_method_is_terminal(item) or terminal_supported
        ]
        if self.auth_host and any(
            str(item.get("type", "agent")).lower() in {"host", "bearer"}
            or str(item.get("id", "")).lower() in {"host", "bearer"}
            for item in visible_auth
        ):
            _validate_host_auth_methods(
                [{"id": "host", "type": "bearer", "host": self.auth_host}],
                self.allow_localhost,
                self.allow_non_loopback,
            )
        _validate_host_auth_methods(
            visible_auth, self.allow_localhost, self.allow_non_loopback
        )
        self.negotiated_version = version
        self.auth_required = (
            self._auth_required_value
            if self._auth_required_explicit
            else any(auth_method_requires_protocol_auth(item) for item in visible_auth)
        )
        self.authenticated = not self.auth_required
        self.initialized = True
        result: dict[str, Any] = {
            "protocolVersion": version,
            "agentCapabilities": dict(self.capabilities),
            "authMethods": visible_auth,
            "agentInfo": dict(self.agent_info),
        }
        return result

    async def _authenticate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Authenticate with one declared method and optional injected callback."""
        method_id = str(params.get("methodId", params.get("method_id", "")) or "")
        if not method_id:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "authenticate methodId is required"
            )
        method = next(
            (
                item
                for item in self.auth_methods
                if str(item.get("id", "")) == method_id
            ),
            None,
        )
        if method is None:
            raise ACPError(
                ACP_ERROR_AUTH_REQUIRED, "ACP authentication method is not declared"
            )
        if not auth_method_requires_protocol_auth(method):
            raise ACPError(
                ACP_ERROR_AUTH_REQUIRED,
                "the selected ACP v1 authentication method is not protocol-driven",
            )
        if self.authenticator is not None:
            await _invoke_callback(self.authenticator, method_id, dict(params))
        self.authenticated = True
        return {}

    async def _new_session(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Create a local session identity and optional public session state."""
        if self.auth_required and not self.authenticated:
            raise ACPError(ACP_ERROR_AUTH_REQUIRED, "ACP authentication is required")
        cwd = params.get("cwd")
        if isinstance(cwd, os.PathLike):
            cwd = os.fspath(cwd)
        if not isinstance(cwd, str) or not cwd.strip():
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "session/new cwd is required"
            )
        if "mcpServers" not in params:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "session/new mcpServers is required"
            )
        if not isinstance(params["mcpServers"], list):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "mcpServers must be an array"
            )
        session_id = ""
        modes = dict(self.modes)
        if self.session_factory is not None:
            value = await _invoke_callback(self.session_factory, dict(params))
            if isinstance(value, ACPSession):
                session_id = value.session_id
                modes = dict(value.modes or modes)
            elif isinstance(value, Mapping):
                session_id = value.get("sessionId", value.get("session_id", ""))
                modes = dict(value.get("modes", modes) or modes)
            elif isinstance(value, str):
                session_id = value
            else:
                raise ACPProtocolError(
                    ACP_ERROR_INVALID_PARAMS,
                    "session factory returned an invalid session",
                )
        if not session_id:
            session_id = f"sess_{uuid.uuid4().hex}"
        validate_session_id(session_id)
        if session_id in self.sessions:
            raise ACPError(
                ACP_ERROR_INTERNAL, "session/new returned a duplicate session id"
            )
        self.sessions[session_id] = _SessionRuntime(
            session_id=session_id, cwd=cwd, modes=modes
        )
        result: dict[str, Any] = {"sessionId": session_id}
        if modes:
            result["modes"] = dict(modes)
        return result

    async def _set_mode(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Set a session mode after checking the locally advertised modes."""
        session_id = validate_session_id(
            params.get("sessionId", params.get("session_id", ""))
        )
        mode_id = params.get("modeId", params.get("mode_id", ""))
        if not isinstance(mode_id, str) or not mode_id or mode_id.strip() != mode_id:
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "modeId is invalid")
        runtime = self.sessions.get(session_id)
        if runtime is None:
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "unknown ACP session")
        available = runtime.modes.get("availableModes", [])
        if available:
            ids = {
                str(item.get("id", item.get("modeId", "")))
                for item in available
                if isinstance(item, Mapping)
            }
            if ids and mode_id not in ids:
                raise ACPProtocolError(
                    ACP_ERROR_INVALID_PARAMS, "unknown ACP session mode"
                )
        runtime.modes["currentModeId"] = mode_id
        return {"currentModeId": mode_id}

    def _validate_prompt_capabilities(self, blocks: list[dict[str, Any]]) -> None:
        """Reject content types not advertised by this stable ACP agent."""
        prompt_caps = self.capabilities.get("promptCapabilities", {})
        if not isinstance(prompt_caps, Mapping):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "ACP prompt capabilities are invalid"
            )
        for block in blocks:
            kind = str(block.get("type", ""))
            required = {
                "image": "image",
                "audio": "audio",
                "resource": "embeddedContext",
            }.get(kind)
            if required is not None and not bool(prompt_caps.get(required, False)):
                raise ACPProtocolError(
                    ACP_ERROR_INVALID_PARAMS,
                    "ACP prompt content capability is unavailable",
                    {"contentType": kind},
                )

    async def _run_prompt_request(
        self,
        params: Mapping[str, Any],
        *,
        request_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Run one prompt while the reader loop remains available for cancel."""
        session_id = validate_session_id(
            params.get("sessionId", params.get("session_id", ""))
        )
        runtime = self.sessions.get(session_id)
        if runtime is None:
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "unknown ACP session")
        if runtime.task is not None and not runtime.task.done():
            raise ACPError(
                ACP_ERROR_INVALID_PARAMS, "ACP session already has an active prompt"
            )
        if "prompt" not in params or params.get("prompt") is None:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "session/prompt prompt is required"
            )
        blocks = normalize_prompt(params.get("prompt"), allow_text=False)
        self._validate_prompt_capabilities(blocks)
        runtime.task = asyncio.current_task()
        runtime.request_id = request_id
        if request_id is not None:
            self._pending_session_requests.pop(session_id, None)
        runtime.run_handle = None
        runtime.cancel_requested = request_id in self._cancelled_request_ids
        runtime.cancel_sent = False
        runtime.handle_cancel_sent = False
        runtime.cancel_event = asyncio.Event()
        runtime.finalized = False
        if request_id is not None:
            self._cancelled_request_ids.discard(request_id)
            self._request_runtimes[request_id] = runtime
        return await self._execute_prompt(runtime, blocks)

    async def _await_agent_prompt(
        self, runtime: _SessionRuntime, prompt: Any
    ) -> tuple[Any, bool]:
        """Await an agent turn while bounding cancellation cleanup."""
        agent_task = asyncio.create_task(self._invoke_agent_prompt(runtime, prompt))
        cancel_task = asyncio.create_task(runtime.cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {agent_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and runtime.cancel_requested:
                try:
                    await asyncio.wait_for(
                        self._cancel_agent(runtime),
                        timeout=min(self.timeout_s, _CANCEL_GRACE_SECONDS),
                    )
                except (asyncio.TimeoutError, ACPError):
                    pass
                try:
                    await asyncio.wait_for(
                        asyncio.shield(agent_task),
                        timeout=_CANCEL_GRACE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    agent_task.cancel()
                    await asyncio.gather(agent_task, return_exceptions=True)
                return None, True
            return await agent_task, False
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

    async def _execute_prompt(
        self,
        runtime: _SessionRuntime,
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Invoke a public agent API and map its stream/result to ACP."""
        if runtime.cancel_requested:
            if runtime.run_handle is not None:
                await self._cancel_agent(runtime)
            runtime.finalized = True
            return self._cancelled_result().to_wire_result()
        prompt_value: Any = blocks
        if len(blocks) == 1 and blocks[0].get("type") == "text":
            prompt_value = str(blocks[0].get("text", ""))
        final_result: Any = None
        error_text = ""
        try:
            final_result, cancelled = await self._await_agent_prompt(
                runtime, prompt_value
            )
            if cancelled:
                runtime.cancel_requested = True
        except ACPError as exc:
            error_text = exc.message
        except Exception as exc:
            error_text = redact_text(str(exc))
        if runtime.cancel_requested:
            result = self._cancelled_result()
        elif error_text:
            result = ACPPromptResult(
                stop_reason=STOP_REASON_REFUSAL,
                status="failed",
                error=error_text,
                result=None,
            )
        else:
            result = self._result_model(final_result)
        runtime.finalized = True
        return result.to_wire_result()

    async def _invoke_agent_prompt(self, runtime: _SessionRuntime, prompt: Any) -> Any:
        """Call stream/query/run public methods in preference order."""
        method, name = _public_method(self.agent, ("stream", "query", "run"))
        if method is None:
            raise ACPError(
                ACP_ERROR_INTERNAL, "agent does not expose a public prompt method"
            )
        if name == "stream":
            try:
                value = await _invoke_agent_method(
                    method, prompt, runtime.session_id, runtime.cwd
                )
                self._capture_run_handle(runtime, value)
            except NotImplementedError:
                method, name = _public_method(self.agent, ("query", "run"))
                if method is None:
                    raise ACPError(
                        ACP_ERROR_INTERNAL, "agent stream method is unavailable"
                    ) from None
                value = await _invoke_agent_method(
                    method, prompt, runtime.session_id, runtime.cwd
                )
                self._capture_run_handle(runtime, value)
                return await self._consume_public_value(value, runtime)
            if value is None:
                method, name = _public_method(self.agent, ("query", "run"))
                if method is None:
                    return None
                value = await _invoke_agent_method(
                    method, prompt, runtime.session_id, runtime.cwd
                )
                self._capture_run_handle(runtime, value)
            return await self._consume_public_value(value, runtime)
        value = await _invoke_agent_method(
            method, prompt, runtime.session_id, runtime.cwd
        )
        self._capture_run_handle(runtime, value)
        return await self._consume_public_value(value, runtime)

    async def _consume_public_value(self, value: Any, runtime: _SessionRuntime) -> Any:
        """Consume a stream returned by query/run while preserving sync/async behavior."""
        if hasattr(value, "__aiter__") or (
            hasattr(value, "__iter__")
            and not isinstance(value, (str, bytes, bytearray, Mapping))
        ):
            return await self._consume_stream(value, runtime)
        return value

    async def _consume_stream(self, value: Any, runtime: _SessionRuntime) -> Any:
        """Consume sync or async public streams without blocking the loop."""
        self._capture_run_handle(runtime, value)
        if hasattr(value, "__aiter__"):
            final: Any = None
            async for item in value:
                self._capture_run_handle(runtime, item)
                if runtime.cancel_requested:
                    break
                permission = _permission_request_payload(item)
                if permission is not None:
                    await self._request_permission(runtime, permission)
                    continue
                terminal = _terminal_item(item)
                if terminal is not None:
                    final = terminal
                    break
                update = normalize_update(item)
                if update is not None:
                    await self._send_update(runtime.session_id, update)
            return final
        if hasattr(value, "__iter__") and not isinstance(value, (str, bytes, Mapping)):
            iterator = iter(value)
            final = None
            while True:
                if runtime.cancel_requested:
                    break
                # `next(iterator)` raises StopIteration, and StopIteration
                # cannot cross a Future boundary: asyncio converts it to a
                # TypeError ("StopIteration interacts badly with generators"),
                # which escaped this loop and left every session/prompt
                # hanging against a real agent. `next(it, sentinel)` does not
                # raise at all.
                item = await asyncio.to_thread(next, iterator, _ITERATOR_DONE)
                if item is _ITERATOR_DONE:
                    break
                self._capture_run_handle(runtime, item)
                permission = _permission_request_payload(item)
                if permission is not None:
                    await self._request_permission(runtime, permission)
                    continue
                if runtime.cancel_requested:
                    break
                terminal = _terminal_item(item)
                if terminal is not None:
                    final = terminal
                    break
                update = normalize_update(item)
                if update is not None:
                    await self._send_update(runtime.session_id, update)
            return final
        return value

    async def _send_update(self, session_id: str, update: Any) -> None:
        """Send one ordered session/update notification without wire redaction."""
        session_id = validate_session_id(session_id)
        if isinstance(update, str):
            update = {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": update},
            }
        if not isinstance(update, Mapping):
            return
        runtime = self.sessions.get(session_id)
        if runtime is not None and runtime.finalized:
            return
        params = {"sessionId": session_id, "update": dict(update)}
        await self._send_transport(
            {"jsonrpc": JSONRPC_VERSION, "method": "session/update", "params": params}
        )

    def _result_model(self, value: Any) -> ACPPromptResult:
        """Map an arbitrary public agent result to a safe prompt result."""
        if isinstance(value, ACPPromptResult):
            return value
        if value is None:
            return ACPPromptResult(
                status="completed_unverified", stop_reason=STOP_REASON_END_TURN
            )
        if isinstance(value, str):
            return ACPPromptResult(
                status="completed_unverified",
                stop_reason=STOP_REASON_END_TURN,
                result=value,
                text=value,
            )
        status_value = _status_value(value)
        status = normalize_status(status_value, verified=True, result=value)
        evidence = clean_verification_evidence(value)
        if status == "completed_verified" and not evidence:
            status = "completed_unverified"
        stop_reason = stop_reason_for_status(status)
        raw_stop = _get_public(
            value, "stopReason", _get_public(value, "stop_reason", "")
        )
        if raw_stop in VALID_STOP_REASONS:
            stop_reason = raw_stop
        error = str(_get_public(value, "error", "") or "")
        answer = result_answer(value)
        return ACPPromptResult(
            stop_reason=stop_reason,
            status=status,
            verified=status == "completed_verified" and evidence,
            result=_json_safe_public(value),
            error=error,
            text=answer,
        )

    def _cancelled_result(self) -> ACPPromptResult:
        """Return the stable cancelled prompt result."""
        return ACPPromptResult(
            stop_reason=STOP_REASON_CANCELLED,
            status="cancelled",
            verified=False,
        )

    def _current_request_id(self) -> int | str | None:
        """Return the JSON-RPC ID owned by the current prompt task."""
        current = asyncio.current_task()
        for request_id in tuple(self._active_request_ids):
            runtime = self._request_runtimes.get(request_id)
            if runtime is None or runtime.task is current:
                return request_id
        return None

    def _capture_run_handle(self, runtime: _SessionRuntime, value: Any) -> None:
        """Capture the public run handle returned or exposed by an agent."""
        if _looks_like_run_handle(value):
            if runtime.run_handle is None:
                runtime.run_handle = value
                if runtime.cancel_requested and not runtime.handle_cancel_sent:
                    task = asyncio.create_task(self._cancel_agent(runtime, force=True))
                    self._cancel_tasks.add(task)
                    task.add_done_callback(self._cancel_tasks.discard)
            return
        if runtime.run_handle is not None:
            return
        for name in ("handle", "run_handle", "current_handle", "current_run"):
            candidate = _get_public(value, name, None)
            if _looks_like_run_handle(candidate):
                runtime.run_handle = candidate
                if runtime.cancel_requested and not runtime.handle_cancel_sent:
                    task = asyncio.create_task(self._cancel_agent(runtime, force=True))
                    self._cancel_tasks.add(task)
                    task.add_done_callback(self._cancel_tasks.discard)
                return
        for name in ("handle", "run_handle", "current_handle", "current_run"):
            candidate = _get_public(self.agent, name, None)
            if _looks_like_run_handle(candidate):
                runtime.run_handle = candidate
                if runtime.cancel_requested and not runtime.handle_cancel_sent:
                    task = asyncio.create_task(self._cancel_agent(runtime, force=True))
                    self._cancel_tasks.add(task)
                    task.add_done_callback(self._cancel_tasks.discard)
                return

    async def _request_permission(
        self, runtime: _SessionRuntime, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Request permission from the peer or use an injected local policy."""
        if not isinstance(params, Mapping):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "permission params must be an object"
            )
        options = params.get("options")
        if not isinstance(options, list) or not options:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "permission options are required"
            )
        if self.permission_handler is not None:
            local = await _invoke_callback(self.permission_handler, dict(params))
            if local is not None:
                return _permission_result(local, options)
        if self.transport is None:
            return {"outcome": {"outcome": "cancelled"}}
        request_id = f"permission-{self._next_permission_id}"
        self._next_permission_id += 1
        future = asyncio.get_running_loop().create_future()
        self._outbound_permissions[request_id] = future
        self._permission_sessions[request_id] = runtime.session_id
        self._permission_payloads[request_id] = dict(params)
        try:
            await self._send_transport(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": request_id,
                    "method": "session/request_permission",
                    "params": dict(params),
                }
            )
            try:
                result = await asyncio.wait_for(future, timeout=self.timeout_s)
            except (asyncio.TimeoutError, ACPError) as exc:
                self.last_error = redact_text(str(exc))
                return {"outcome": {"outcome": "cancelled"}}
            return _permission_result(result, options)
        finally:
            self._outbound_permissions.pop(request_id, None)
            self._permission_sessions.pop(request_id, None)
            self._permission_payloads.pop(request_id, None)

    async def _cancel_session(self, params: Mapping[str, Any]) -> bool:
        """Mark an active session cancelled without closing its transport."""
        if not isinstance(params, Mapping):
            return False
        request_id = params.get("requestId", params.get("request_id"))
        if request_id is not None:
            await self._cancel_request({"id": request_id})
            return True
        session_id = validate_session_id(
            params.get("sessionId", params.get("session_id", ""))
        )
        runtime = self.sessions.get(session_id)
        if runtime is None:
            return False
        active = runtime.task is not None and not runtime.task.done()
        pending_request = self._pending_session_requests.get(session_id)
        if active or pending_request is not None:
            runtime.cancel_requested = True
            runtime.cancel_event.set()
            if pending_request is not None:
                self._cancelled_request_ids.add(pending_request)
        await self._cancel_permission_requests(session_id)
        try:
            await asyncio.wait_for(
                self._cancel_agent(runtime, force=True),
                timeout=_CANCEL_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            self.last_error = "ACP cancellation grace period expired"
        return active

    async def _cancel_request(self, params: Mapping[str, Any]) -> None:
        """Cancel the request identified by a v1 ``$/cancel_request``."""
        if not isinstance(params, Mapping):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "cancel params must be an object"
            )
        request_id = params.get("id", params.get("requestId", params.get("request_id")))
        if (
            request_id is None
            or isinstance(request_id, bool)
            or not isinstance(request_id, (int, str))
        ):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "cancel request id is invalid"
            )
        runtime = self._request_runtimes.get(request_id)
        if runtime is None:
            pending_sessions = [
                session_id
                for session_id, pending_id in self._pending_session_requests.items()
                if pending_id == request_id
            ]
            if pending_sessions:
                self._cancelled_request_ids.add(request_id)
                return
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "no active ACP request mapping"
            )
        if (
            runtime.request_id != request_id
            or runtime.task is None
            or runtime.task.done()
            or runtime.finalized
        ):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "no active ACP request mapping"
            )
        runtime.cancel_requested = True
        runtime.cancel_event.set()
        await self._cancel_permission_requests(runtime.session_id)
        try:
            await asyncio.wait_for(
                self._cancel_agent(runtime), timeout=_CANCEL_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            self.last_error = "ACP cancellation grace period expired"

    async def _cancel_agent(
        self, runtime: _SessionRuntime, *, force: bool = False
    ) -> None:
        """Cancel the captured run handle, falling back to the public agent API."""
        if runtime.cancel_sent and not force:
            return
        runtime.cancel_sent = True
        handle = runtime.run_handle
        if handle is None:
            for name in ("handle", "run_handle", "current_handle", "current_run"):
                candidate = _get_public(self.agent, name, None)
                if _looks_like_run_handle(candidate):
                    runtime.run_handle = candidate
                    handle = candidate
                    break
        waiting_for_handle = (
            runtime.task is not None and not runtime.task.done()
        ) or runtime.session_id in self._pending_session_requests
        if handle is None and waiting_for_handle:
            for _ in range(20):
                await asyncio.sleep(0.005)
                handle = runtime.run_handle
                if handle is not None:
                    break
        cancel = getattr(handle, "cancel", None) if handle is not None else None
        if callable(cancel) and not runtime.handle_cancel_sent:
            runtime.handle_cancel_sent = True
            try:
                result = await asyncio.wait_for(
                    _resolve_public_call(cancel), self.timeout_s
                )
                if result is not False:
                    return
            except Exception as exc:
                self.last_error = redact_text(str(exc))
        method, _name = _public_method(self.agent, ("cancel",))
        if method is None:
            return
        try:
            await asyncio.wait_for(
                _invoke_agent_method(method, None, runtime.session_id, runtime.cwd),
                self.timeout_s,
            )
        except Exception as exc:
            self.last_error = redact_text(str(exc))

    async def _cancel_permission_requests(self, session_id: str) -> None:
        """Resolve server-originated permission requests when a turn is cancelled."""
        for request_id, future in list(self._outbound_permissions.items()):
            if self._permission_sessions.get(request_id) != session_id or future.done():
                continue
            future.set_result({"outcome": {"outcome": "cancelled"}})
            self._outbound_permissions.pop(request_id, None)
            self._permission_sessions.pop(request_id, None)
            self._permission_payloads.pop(request_id, None)

    async def replay(self, session_id: str, prompt: Any = None) -> ACPPromptResult:
        """Replay a public agent stream when the optional public API exists."""
        session_id = validate_session_id(session_id)
        runtime = self.sessions.get(session_id)
        if runtime is None:
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "unknown ACP session")
        method, _name = _public_method(self.agent, ("replay",))
        if method is None:
            raise ACPError(
                ACP_ERROR_METHOD_NOT_FOUND, "agent does not expose public replay"
            )
        value = await _invoke_agent_method(
            method, prompt, runtime.session_id, runtime.cwd
        )
        self._capture_run_handle(runtime, value)
        final = await self._consume_stream(value, runtime)
        return self._result_model(final)

    async def _send_result(self, request_id: Any, result: Any) -> None:
        """Send one successful response for a request ID."""
        response_id = _safe_response_id(request_id)
        if response_id is not None and response_id in self._completed_request_ids:
            return
        if response_id is not None:
            self._completed_request_ids.add(response_id)
        await self._send_transport(ACPResponse(id=response_id, result=result).to_dict())

    async def _send_error(
        self, request_id: Any, error: ACPError, *, force: bool = False
    ) -> None:
        """Send one redacted JSON-RPC error response."""
        response_id = _safe_response_id(request_id)
        if (
            response_id is not None
            and response_id in self._completed_request_ids
            and not force
        ):
            return
        if response_id is not None and not force:
            self._completed_request_ids.add(response_id)
        try:
            await self._send_transport(
                ACPResponse(id=response_id, error=error).to_dict()
            )
        except Exception as exc:
            self.last_error = redact_text(str(exc))

    async def aclose(self) -> None:
        """Asynchronously cancel active turns and release all owned resources."""
        if self.closed:
            return
        self.closed = True
        current = asyncio.current_task()
        current_loop = asyncio.get_running_loop()
        for runtime in list(self.sessions.values()):
            runtime.finalized = True
            runtime.cancel_requested = True
            runtime.cancel_event.set()
            for request_id, future in list(self._outbound_permissions.items()):
                if (
                    self._permission_sessions.get(request_id) == runtime.session_id
                    and not future.done()
                ):
                    future.set_result({"outcome": {"outcome": "cancelled"}})
            task = runtime.task
            if (
                task is not None
                and task is not current
                and not task.done()
                and task.get_loop() is current_loop
            ):
                task.cancel()
        for task in list(self._request_tasks):
            if (
                task is not current
                and not task.done()
                and task.get_loop() is current_loop
            ):
                task.cancel()
        for task in list(self._cancel_tasks):
            if (
                task is not current
                and not task.done()
                and task.get_loop() is current_loop
            ):
                task.cancel()
        if (
            self._reader_task is not None
            and self._reader_task is not current
            and not self._reader_task.done()
            and self._reader_task.get_loop() is current_loop
        ):
            self._reader_task.cancel()
        if self.owns_transport and self.transport is not None:
            close = getattr(self.transport, "close", None)
            if callable(close):
                await call_maybe_async(self.transport, "close")
        close_agent, _name = _public_method(self.agent, ("close",))
        if close_agent is not None:
            try:
                await _invoke_agent_method(close_agent, None, "", "")
            except Exception as exc:
                self.last_error = redact_text(str(exc))
        if self._closed_event is not None:
            self._closed_event.set()

    def close(self) -> ImmediateAwaitable[None]:
        """Close the server from sync or async code without leaking a coroutine."""
        if self.closed:
            return ImmediateAwaitable(None)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            run_sync(self.aclose())
            return ImmediateAwaitable(None)
        task = asyncio.create_task(self.aclose())
        return ImmediateAwaitable(task)

    shutdown = close

    def __enter__(self) -> "ACPServer":
        """Enter a synchronous server context."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the server on synchronous context exit."""
        run_sync(self.close())
        return False

    async def __aenter__(self) -> "ACPServer":
        """Start the server for an asynchronous context manager."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the server on asynchronous context exit."""
        await self.aclose()
        return False


def _safe_response_id(value: Any) -> int | str | None:
    """Return an ID safe to place in an error response."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return value


def _is_loopback_host(value: str) -> bool:
    """Return whether a host descriptor names an explicit loopback address."""
    host = str(value or "").strip().lower()
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_host_auth_methods(
    methods: Sequence[Mapping[str, Any]],
    allow_localhost: bool,
    allow_non_loopback: bool,
) -> None:
    """Apply the explicit loopback policy to host-owned bearer descriptors."""
    for method in methods:
        if str(method.get("type", "agent")).lower() not in {"host", "bearer"} and str(
            method.get("id", "")
        ).lower() not in {"host", "bearer"}:
            continue
        descriptor = method.get("host", method.get("url"))
        if descriptor is None:
            continue
        try:
            parsed = urlsplit(str(descriptor))
            host = parsed.hostname or str(descriptor).split(":", 1)[0]
        except ValueError as exc:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "invalid host authentication descriptor"
            ) from exc
        if _is_loopback_host(host):
            if not allow_localhost and not allow_non_loopback:
                raise ACPProtocolError(
                    ACP_ERROR_INVALID_PARAMS, "localhost authentication is not allowed"
                )
        elif not allow_non_loopback:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS,
                "non-loopback host authentication is not allowed",
            )


def _capability_enabled(capabilities: Mapping[str, Any], path: str) -> bool:
    """Return a nested client capability flag."""
    value: Any = capabilities
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False
        value = value[part]
    return bool(value)


def _validate_required_capability_names(value: Any) -> None:
    """Reject unknown required capability names before negotiation."""
    if value is None:
        return
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ACPProtocolError(
            ACP_ERROR_INVALID_PARAMS, "requiredCapabilities must be an array"
        )
    known = {
        "auth",
        "auth.logout",
        "auth.terminal",
        "fs",
        "fs.readTextFile",
        "fs.writeTextFile",
        "terminal",
        "elicitation",
        "session.configOptions.boolean",
        "promptCapabilities.image",
        "promptCapabilities.audio",
        "promptCapabilities.embeddedContext",
        "mcpCapabilities",
        "mcpCapabilities.http",
        "mcpCapabilities.sse",
        "loadSession",
        "sessionCapabilities",
        "sessionCapabilities.close",
        "sessionCapabilities.resume",
        "sessionCapabilities.delete",
        "sessionCapabilities.list",
    }
    for item in value:
        if str(item) not in known:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS,
                "unknown required ACP capability",
                {"capability": str(item)},
            )


def _validate_required_capabilities(
    required: Sequence[str], capabilities: Mapping[str, Any]
) -> None:
    """Reject unknown or unavailable required client capabilities."""
    known = {
        "auth",
        "auth.logout",
        "auth.terminal",
        "fs",
        "fs.readTextFile",
        "fs.writeTextFile",
        "terminal",
        "elicitation",
        "session.configOptions.boolean",
        "promptCapabilities.image",
        "promptCapabilities.audio",
        "promptCapabilities.embeddedContext",
        "mcpCapabilities",
        "mcpCapabilities.http",
        "mcpCapabilities.sse",
        "loadSession",
        "sessionCapabilities",
        "sessionCapabilities.close",
        "sessionCapabilities.resume",
        "sessionCapabilities.delete",
        "sessionCapabilities.list",
    }
    values = [required] if isinstance(required, str) else required
    for item in values:
        name = str(item or "")
        if name not in known:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS,
                "unknown required ACP capability",
                {"capability": name},
            )
        if not _capability_enabled(capabilities, name):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS,
                "required ACP capability is unavailable",
                {"capability": name},
            )


def _looks_like_run_handle(value: Any) -> bool:
    """Return whether a public value exposes run-handle behavior."""
    if value is None or isinstance(value, (str, bytes, bytearray, Mapping)):
        return False
    try:
        if not hasattr(value, "run_id"):
            return False
        return any(
            callable(getattr(value, name, None))
            for name in ("cancel", "wait", "stream", "events", "__iter__")
        )
    except Exception:
        return False


def _permission_request_payload(value: Any) -> dict[str, Any] | None:
    """Recognize a public stream item that requests client permission."""
    if not isinstance(value, Mapping):
        method = str(_get_public(value, "method", "")).lower()
        kind = str(_get_public(value, "type", _get_public(value, "kind", ""))).lower()
        if method == "session/request_permission" or kind in {
            "permission_request",
            "permission-request",
            "request_permission",
        }:
            candidate = _get_public(
                value, "params", _get_public(value, "request", value)
            )
            if isinstance(candidate, Mapping):
                return dict(candidate)
            fields = {
                name: _get_public(value, name, None)
                for name in (
                    "sessionId",
                    "session_id",
                    "toolCall",
                    "tool_call",
                    "options",
                )
            }
            if fields["options"] is not None:
                return {key: item for key, item in fields.items() if item is not None}
        return None
    kind = str(value.get("type", value.get("kind", value.get("event", "")))).lower()
    if kind not in {
        "permission_request",
        "permission-request",
        "session/request_permission",
        "request_permission",
    } and not ("options" in value and ("toolCall" in value or "tool_call" in value)):
        return None
    candidate = value.get("request", value.get("params", value))
    return dict(candidate) if isinstance(candidate, Mapping) else None


def _permission_result(value: Any, options: list[Any]) -> dict[str, Any]:
    """Normalize a permission decision to the stable v1 result shape."""
    if isinstance(value, ACPResponse):
        value = value.result
    if isinstance(value, str):
        value = {"optionId": value}
    elif isinstance(value, bool):
        value = {"allow": value}
    elif value is None:
        value = {}
    if not isinstance(value, Mapping):
        value = {}
    data = dict(value)
    outcome = data.get("outcome")
    if outcome == "cancelled":
        return {"outcome": {"outcome": "cancelled"}}
    if isinstance(outcome, Mapping):
        outcome_data = dict(outcome)
        if outcome_data.get("outcome") == "cancelled":
            return {"outcome": outcome_data}
        if outcome_data.get("outcome") == "selected" and outcome_data.get(
            "optionId"
        ) in {
            str(item.get("optionId", item.get("id", "")))
            for item in options
            if isinstance(item, Mapping)
        }:
            return {"outcome": outcome_data}
        return {"outcome": {"outcome": "cancelled"}}
    option_id = data.get("optionId", data.get("option_id", data.get("decision")))
    if not option_id and data.get("allow") is True:
        option_id = _permission_option_id(options, allow=True)
    if not option_id:
        option_id = _permission_option_id(options, allow=False)
    offered_ids = {
        str(item.get("optionId", item.get("id", "")))
        for item in options
        if isinstance(item, Mapping)
    }
    if option_id and str(option_id) in offered_ids:
        return {"outcome": {"outcome": "selected", "optionId": str(option_id)}}
    return {"outcome": {"outcome": "cancelled"}}


def _permission_option_id(options: list[Any], *, allow: bool) -> str:
    """Choose an allow or reject option without trusting callback text."""
    kinds_allow = {"allow", "allow_once", "allow_always"}
    kinds_reject = {"deny", "reject", "reject_once", "reject_always"}
    for item in options:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind", "")).lower()
        if (allow and kind in kinds_allow) or (not allow and kind in kinds_reject):
            return str(item.get("optionId", item.get("id", "")))
    return ""


async def _resolve_public_call(value: Any) -> Any:
    """Resolve a public zero-argument call without blocking the event loop."""
    if callable(value):
        if inspect.iscoroutinefunction(value):
            result = value()
        else:
            result = await asyncio.to_thread(value)
    else:
        result = value
    if inspect.isawaitable(result):
        return await result
    return result


def _is_known_method(method: str) -> bool:
    """Return whether a method is part of this adapter's public ACP surface."""
    return method in {
        "initialize",
        "authenticate",
        "session/new",
        "session/prompt",
        "session/cancel",
        "session/set_mode",
        "shutdown",
        "exit",
        "$/shutdown",
        "$/cancel_request",
        "ping",
    }


def _looks_like_transport(value: Any) -> bool:
    """Return whether a value exposes a transport read or write method."""
    return any(
        callable(getattr(value, name, None))
        for name in ("send", "receive", "recv", "write", "read")
    )


def _capability_mapping(
    value: Mapping[str, Any] | ACPCapabilities | None,
) -> dict[str, Any]:
    """Normalize stable v1 agent capability options without wire redaction."""
    if isinstance(value, ACPCapabilities):
        value = value.agent_capabilities
    if value is not None and not isinstance(value, Mapping):
        raise ACPProtocolError(
            ACP_ERROR_INVALID_PARAMS, "capabilities must be an object"
        )
    data = dict(value or {})
    data.setdefault("loadSession", False)
    data.setdefault(
        "promptCapabilities",
        {"image": False, "audio": False, "embeddedContext": False},
    )
    data.setdefault("mcpCapabilities", {"http": False, "sse": False})
    data.setdefault("sessionCapabilities", {})
    data.setdefault("auth", {})
    return data


def _auth_methods(
    values: Sequence[Mapping[str, Any] | str] | Mapping[str, Any] | str | None,
) -> list[dict[str, Any]]:
    """Normalize authentication declarations without invoking an agent SDK."""
    if isinstance(values, (Mapping, str)):
        values = [values]
    model = ACPCapabilities(auth_methods=values or [])
    return [dict(item) for item in model.auth_methods]


def _modes(
    value: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Normalize a session mode state mapping."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    available = [
        {"id": str(item)} if isinstance(item, str) else dict(item)
        for item in value
        if isinstance(item, (Mapping, str))
    ]
    return {
        "currentModeId": available[0].get("id", "") if available else "",
        "availableModes": available,
    }


def _public_method(
    owner: Any, names: Sequence[str]
) -> tuple[Callable[..., Any] | None, str]:
    """Find a callable public method without importing implementation modules."""
    for name in names:
        method = getattr(owner, name, None)
        if callable(method):
            return method, name
    return None, ""


async def _invoke_agent_method(
    method: Callable[..., Any],
    prompt: Any,
    session_id: str,
    cwd: str,
) -> Any:
    """Invoke a public agent method using only signature-compatible arguments."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        if inspect.iscoroutinefunction(method):
            result = method(prompt, session_id=session_id)
        else:
            result = await asyncio.to_thread(method, prompt, session_id=session_id)
        return await _resolve_agent_result(result)
    parameters = list(signature.parameters.values())
    accepts_kwargs = any(
        item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters
    )
    positional_names = {"prompt", "message", "text", "request", "query", "input"}
    # A CONVERSATION id and a RUN id are different things, and conflating
    # them is how "prompt the agent" silently becomes "replay a run that does
    # not exist" (which fails closed with a missing-journal refusal).
    # Strong names carry the conversation; a `run_id` parameter asks for one
    # specific run and is only a LAST resort, after neither a strong
    # parameter nor **kwargs is available to receive the session id.
    strong_session_names = {
        "session_id",
        "sessionId",
        "session",
        "conversation_id",
        "conversation",
    }
    weak_session_names = {"run_id"}
    declared = {item.name for item in parameters}
    session_names = strong_session_names
    deferred_weak: str = ""
    cwd_names = {"cwd", "working_directory", "workspace"}
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    prompt_bound = False
    for parameter in parameters:
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        name = parameter.name
        if name in positional_names and prompt is not None and not prompt_bound:
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                args.append(prompt)
            else:
                kwargs[name] = prompt
            prompt_bound = True
        elif name in session_names and session_id:
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                args.append(session_id)
            else:
                kwargs[name] = session_id
        elif name in weak_session_names and session_id:
            deferred_weak = name
        elif name in cwd_names and cwd:
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                args.append(cwd)
            else:
                kwargs[name] = cwd
        elif (
            parameter.default is inspect.Parameter.empty
            and parameter.kind != inspect.Parameter.POSITIONAL_ONLY
        ):
            if name in {"prompt", "message", "text", "request", "query", "input"}:
                kwargs[name] = prompt
                prompt_bound = True
    if not prompt_bound and prompt is not None:
        positional = [
            item
            for item in parameters
            if item.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ]
        if positional and positional[0].name not in kwargs:
            args.insert(0, prompt)
    if accepts_kwargs:
        kwargs.setdefault("session_id", session_id)
        # `cwd` is NOT injected through **kwargs. A method with **kwargs
        # frequently forwards them into a typed constructor (agent_sdk's
        # RunRequest is the real example) that has no `cwd` field, and
        # guessing an argument name is how "prompt the agent" turns into a
        # TypeError. An agent that wants the working directory names a
        # cwd-ish parameter, which the loop above already binds.
    elif deferred_weak and session_id and not (declared & strong_session_names):
        # Neither a conversation parameter nor **kwargs exists: a bare
        # `run_id` is the only carrier left, and delivering nothing is worse
        # than delivering the conversation id under a loose name.
        kwargs[deferred_weak] = session_id
    if inspect.iscoroutinefunction(method):
        result = method(*args, **kwargs)
    else:
        result = await asyncio.to_thread(method, *args, **kwargs)
    return await _resolve_agent_result(result)


async def _resolve_agent_result(value: Any) -> Any:
    """Resolve a public method's immediate or asynchronous return value."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _invoke_callback(callback: Callable[..., Any], *args: Any) -> Any:
    """Invoke an injected callback with as many public arguments as it accepts."""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        if inspect.iscoroutinefunction(callback):
            return await callback(*args)
        return await _resolve_agent_result(await asyncio.to_thread(callback, *args))
    parameters = list(signature.parameters.values())
    if any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters):
        if inspect.iscoroutinefunction(callback):
            return await callback(*args)
        return await _resolve_agent_result(await asyncio.to_thread(callback, *args))
    if len(args) > len(parameters):
        args = args[: len(parameters)]
    if inspect.iscoroutinefunction(callback):
        return await callback(*args)
    return await _resolve_agent_result(await asyncio.to_thread(callback, *args))


def _status_value(value: Any) -> Any:
    """Read a public status field from a result object."""
    status = _get_public(value, "status", _get_public(value, "state", ""))
    if status:
        return status
    payload = _get_public(value, "payload", None)
    if isinstance(payload, Mapping):
        payload_status = _get_public(
            payload, "status", _get_public(payload, "state", "")
        )
        if payload_status:
            return payload_status
    return _get_public(
        value,
        "completion_status",
        _get_public(value, "event_type", "completed_unverified"),
    )


def _get_public(value: Any, name: str, default: Any = None) -> Any:
    """Read a public mapping key or object attribute without leaking failures."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _terminal_item(item: Any) -> Any | None:
    """Return a terminal result item, or None for an update chunk."""
    if isinstance(item, tuple) and len(item) == 2 and is_terminal_result(item[1]):
        return item[1]
    if is_terminal_result(item):
        return item
    return None


ACPServerAdapter = ACPServer

__all__ = ["ACPServer", "ACPServerAdapter", "PublicAgent"]
