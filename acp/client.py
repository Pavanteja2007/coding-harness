"""ACP v1 client adapter with concurrent request, update, and cancellation handling."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from shared.security import redact_secrets, redact_text

from ._compat import (
    ImmediateAwaitable,
    call_maybe_async,
    call_optional,
    decode_wire_message,
    run_sync,
)
from .models import (
    ACP_ERROR_AUTH_REQUIRED,
    ACP_ERROR_INVALID_PARAMS,
    ACP_ERROR_METHOD_NOT_FOUND,
    JSONRPC_VERSION,
    PROTOCOL_VERSION,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_MAX_TURN_REQUESTS,
    STOP_REASON_REFUSAL,
    ACPAuthenticationError,
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
    ACPUnsupportedVersionError,
    auth_method_is_terminal,
    auth_method_requires_protocol_auth,
    negotiate_protocol_version,
    normalize_prompt,
    normalize_protocol_version,
    validate_session_id,
)
from .transport import ACPTransport, StdioACPTransport

_UPDATE_HISTORY_LIMIT = 2048
_DEFERRED_RESPONSE_LIMIT = 2048
_TERMINAL_ID_LIMIT = 4096
_RECEIVE_POLL_SECONDS = 0.1


@dataclass
class _PromptState:
    """Mutable client-side state for one in-flight prompt turn."""

    session_id: str
    request_id: int | str | None = None
    updates: list[dict[str, Any]] = field(default_factory=list)
    callback: Callable[[dict[str, Any]], Any] | None = None
    callback_tasks: list[asyncio.Task[Any]] = field(default_factory=list)


class ACPClient:
    """A concurrent ACP v1 client over an injectable JSON-RPC transport."""

    def __init__(
        self,
        transport: ACPTransport | Any = None,
        *,
        protocol_version: int = PROTOCOL_VERSION,
        version: int | None = None,
        client_capabilities: Mapping[str, Any] | None = None,
        client_info: Mapping[str, Any] | None = None,
        timeout_s: float = 30.0,
        timeout: float | None = None,
        on_update: Callable[[dict[str, Any]], Any] | None = None,
        on_permission_request: Callable[..., Any] | None = None,
        permission_handler: Callable[..., Any] | None = None,
        permission_hook: Callable[..., Any] | None = None,
        on_permission: Callable[..., Any] | None = None,
        on_permission_response: Callable[..., Any] | None = None,
        permission_response_handler: Callable[..., Any] | None = None,
        permission_response: Callable[..., Any] | None = None,
        owns_transport: bool = True,
        argv: Any = None,
        cwd: Any = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Create a client, optionally constructing a safe stdio transport."""
        if version is not None:
            protocol_version = version
        if timeout is not None:
            timeout_s = timeout
        if transport is None and argv is not None:
            transport = StdioACPTransport(argv, cwd=cwd, env=env, timeout_s=timeout_s)
        if transport is None:
            raise ValueError("ACPClient requires an injectable transport or argv")
        self.transport = transport
        self.protocol_version = normalize_protocol_version(protocol_version)
        self.client_capabilities = dict(client_capabilities or {})
        self.client_info = dict(
            client_info or {"name": "neo-acp-client", "version": "1"}
        )
        self.timeout_s = max(0.01, float(timeout_s))
        self.on_update = on_update
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
        self.owns_transport = bool(owns_transport)
        self.capabilities: ACPCapabilities | None = None
        self.negotiated_version: int | None = None
        self.authenticated = False
        self.closed = False
        self.last_error = ""
        self.sessions: dict[str, ACPSession] = {}
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._deferred_responses: deque[Mapping[str, Any]] = deque()
        self._terminal_ids: set[int | str] = set()
        self._terminal_order: deque[int | str] = deque()
        self._permission_requests: dict[int | str, dict[str, Any]] = {}
        self._permission_terminal_ids: set[int | str] = set()
        self._permission_cancelled_ids: set[int | str] = set()
        self._permission_task_by_id: dict[int | str, asyncio.Task[Any]] = {}
        self._permission_tasks: set[asyncio.Task[Any]] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._reader_started = False
        self._closed_event: asyncio.Event | None = None
        self._update_history: dict[str, list[dict[str, Any]]] = {}
        self._active_prompts: dict[str, _PromptState] = {}
        self._callback_tasks: set[asyncio.Task[Any]] = set()

    @property
    def initialized(self) -> bool:
        """Return whether version negotiation completed successfully."""
        return self.capabilities is not None and self.negotiated_version is not None

    @property
    def protocolVersion(self) -> int | None:
        """Return the negotiated ACP protocol version."""
        return self.negotiated_version

    @property
    def protocol_version_value(self) -> int | None:
        """Return the negotiated version under a descriptive alias."""
        return self.negotiated_version

    @property
    def auth_methods(self) -> list[dict[str, Any]]:
        """Return authentication methods advertised by the agent."""
        return list(self.capabilities.auth_methods) if self.capabilities else []

    @property
    def updates(self) -> dict[str, list[dict[str, Any]]]:
        """Return a copy of ordered updates grouped by session id."""
        return {key: list(value) for key, value in self._update_history.items()}

    async def _start_transport(self) -> None:
        """Start the underlying transport once, if it exposes a lifecycle hook."""
        if self.closed:
            raise ACPTransportClosed(-32000, "ACP client is closed")
        if not self._reader_started:
            await call_optional(self.transport, ("start", "open"), default=None)
            self._reader_started = True
            self._closed_event = asyncio.Event()
            self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        """Read and dispatch responses/notifications without closing on idle timeouts."""
        try:
            while not self.closed:
                try:
                    message = await self._receive_transport()
                except ACPTransportClosed:
                    break
                except ACPProtocolError as exc:
                    self.last_error = redact_text(str(exc))
                    await asyncio.sleep(0)
                    continue
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
                if message is None:
                    if bool(getattr(self.transport, "closed", False)):
                        break
                    await asyncio.sleep(0.001)
                    continue
                await self._dispatch_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = redact_text(str(exc))
            self._fail_pending(
                ACPTransportClosed(-32000, self.last_error or "ACP transport closed")
            )
        finally:
            if self._closed_event is not None:
                self._closed_event.set()

    async def _receive_transport(self) -> Mapping[str, Any] | None:
        """Receive from the first available transport read method."""
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
        raise ACPTransportError(-32000, "ACP transport cannot receive messages")

    async def _send_transport(self, message: Mapping[str, Any]) -> None:
        """Send through the first available transport write method."""
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
        for name in ("notify", "request"):
            method = getattr(self.transport, name, None)
            if callable(method):
                await call_maybe_async(self.transport, name, message)
                return
        raise ACPTransportError(-32000, "ACP transport cannot send messages")

    def _allocate_id(self) -> int:
        """Allocate a monotonically increasing JSON-RPC request id."""
        value = self._next_id
        self._next_id += 1
        return value

    def _mark_terminal(self, request_id: int | str) -> None:
        """Remember a terminal request ID with bounded retention."""
        if request_id in self._terminal_ids:
            return
        self._terminal_ids.add(request_id)
        self._terminal_order.append(request_id)
        while len(self._terminal_order) > _TERMINAL_ID_LIMIT:
            self._terminal_ids.discard(self._terminal_order.popleft())

    def _take_deferred(self, request_id: int | str) -> Mapping[str, Any] | None:
        """Remove and return the first response queued for one request id."""
        for index, message in enumerate(self._deferred_responses):
            if message.get("id") == request_id and not isinstance(
                message.get("id"), bool
            ):
                del self._deferred_responses[index]
                return message
        return None

    def _defer_response(self, message: Mapping[str, Any]) -> None:
        """Preserve one out-of-order response per ID until its request arrives."""
        request_id = message.get("id")
        if request_id in self._terminal_ids:
            return
        if any(item.get("id") == request_id for item in self._deferred_responses):
            return
        if len(self._deferred_responses) >= _DEFERRED_RESPONSE_LIMIT:
            self._deferred_responses.popleft()
        self._deferred_responses.append(dict(message))

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        *,
        request_id: int | str | None = None,
    ) -> Any:
        """Send one request and await only its matching terminal response."""
        await self._start_transport()
        request = ACPRequest(
            id=self._allocate_id() if request_id is None else request_id,
            method=method,
            params=params or {},
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request.id] = future
        deferred = self._take_deferred(request.id)
        try:
            if deferred is None:
                await self._send_transport(request.to_dict())
            else:
                await self._dispatch_message(deferred)
            try:
                return await asyncio.wait_for(
                    future,
                    timeout=self.timeout_s
                    if timeout_s is None
                    else max(0.0, float(timeout_s)),
                )
            except asyncio.TimeoutError as exc:
                self._mark_terminal(request.id)
                try:
                    await self._send_transport(
                        {
                            "jsonrpc": JSONRPC_VERSION,
                            "method": "$/cancel_request",
                            "params": {"id": request.id},
                        }
                    )
                except Exception:
                    pass
                raise ACPTimeoutError(
                    -32000, f"ACP request timed out: {method}"
                ) from exc
        finally:
            self._pending.pop(request.id, None)
            self._mark_terminal(request.id)

    async def _notification(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> None:
        """Send one JSON-RPC notification without assigning an id."""
        await self._start_transport()
        await self._send_transport(
            ACPRequest(method=method, params=params or {}, id=None).to_dict()
        )

    async def _dispatch_message(self, message: Mapping[str, Any]) -> None:
        """Dispatch one response or notification while preserving wire order."""
        if not isinstance(message, Mapping):
            return
        if message.get("jsonrpc") != JSONRPC_VERSION:
            self.last_error = "ACP peer emitted an unsupported JSON-RPC version"
            return
        if "method" not in message:
            if "id" not in message:
                self.last_error = "ACP peer response has no request id"
                return
            request_id = message.get("id")
            if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
                self.last_error = "ACP peer response id is invalid"
                return
            if request_id is None:
                self.last_error = "ACP peer response has a null request id"
                return
            if request_id in self._terminal_ids:
                return
            future = self._pending.get(request_id)
            if future is None or future.done():
                self._defer_response(message)
                return
            try:
                parsed = ACPResponse.from_dict(message)
            except ACPError as exc:
                future.set_exception(exc)
                return
            self._mark_terminal(request_id)
            if parsed.error is not None:
                future.set_exception(parsed.error)
            else:
                future.set_result(parsed.result)
            return
        method = message.get("method")
        if not isinstance(method, str) or not method:
            self.last_error = "ACP peer notification has an invalid method"
            return
        if "params" in message and not isinstance(message.get("params"), Mapping):
            await self._send_client_error(
                _safe_client_id(message.get("id")),
                ACPProtocolError(-32602, "JSON-RPC params must be an object"),
            )
            return
        if method == "session/request_permission":
            if "id" not in message or message.get("id") is None:
                await self._send_client_error(
                    None,
                    ACPProtocolError(-32600, "permission request id is required"),
                )
                return
            request_id = message.get("id")
            if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
                await self._send_client_error(
                    None,
                    ACPProtocolError(-32600, "permission request id is invalid"),
                )
                return
            if (
                request_id in self._permission_requests
                or request_id in self._permission_terminal_ids
            ):
                await self._send_client_error(
                    request_id,
                    ACPProtocolError(-32600, "duplicate ACP permission request id"),
                )
                return
            raw_params = message.get("params")
            if not isinstance(raw_params, Mapping):
                await self._send_client_error(
                    request_id,
                    ACPProtocolError(-32602, "permission params must be an object"),
                )
                return
            params = dict(raw_params)
            self._permission_requests[request_id] = params
            task = asyncio.create_task(
                self._handle_permission_request(request_id, params)
            )
            self._permission_tasks.add(task)
            self._permission_task_by_id[request_id] = task
            task.add_done_callback(self._permission_tasks.discard)
            return
        if "id" in message and message.get("id") is not None:
            request_id = message.get("id")
            if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
                await self._send_client_error(
                    None,
                    ACPProtocolError(-32600, "request id is invalid"),
                )
                return
            await self._send_client_error(
                request_id,
                ACPError(
                    ACP_ERROR_METHOD_NOT_FOUND, "ACP client method is not supported"
                ),
            )
            return
        if method == "session/update":
            await self._handle_update(message.get("params", {}))
        return

    async def _send_client_error(self, request_id: Any, error: ACPError) -> None:
        """Send a redacted error for an inbound client request."""
        try:
            await self._send_transport(
                ACPResponse(id=request_id, error=error).to_dict()
            )
        except Exception as exc:
            self.last_error = redact_text(str(exc))

    async def _handle_permission_request(
        self, request_id: int | str, params: dict[str, Any]
    ) -> None:
        """Resolve an optional ACP permission request and preserve its response id."""
        try:
            if not isinstance(params, Mapping):
                raise ACPProtocolError(-32602, "permission params must be an object")
            session_id = params.get("sessionId", params.get("session_id", ""))
            if not isinstance(session_id, str) or not session_id:
                raise ACPProtocolError(-32602, "permission sessionId is required")
            options = params.get("options")
            if not isinstance(options, list) or not options:
                raise ACPProtocolError(-32602, "permission options are required")
            if not isinstance(params.get("toolCall", params.get("tool_call")), Mapping):
                raise ACPProtocolError(-32602, "permission toolCall is required")
            value: Any = None
            if self.permission_handler is not None:
                value = await self._call_permission_hook(
                    self.permission_handler, params
                )
            if request_id not in self._permission_requests:
                return
            result = self._permission_result(value, options)
            await self._send_transport(
                ACPResponse(id=request_id, result=result).to_dict()
            )
            if self.permission_response_handler is not None:
                await self._call_permission_hook(
                    self.permission_response_handler,
                    params,
                    result,
                    request_id,
                    response=True,
                )
        except ACPError as exc:
            await self._send_client_error(request_id, exc)
        except Exception as exc:
            self.last_error = redact_text(str(exc))
            await self._send_client_error(
                request_id,
                ACPProtocolError(-32603, "permission request handling failed"),
            )
        finally:
            self._permission_terminal_ids.add(request_id)
            self._permission_requests.pop(request_id, None)
            self._permission_task_by_id.pop(request_id, None)
            self._permission_cancelled_ids.discard(request_id)

    async def _call_permission_hook(
        self,
        callback: Callable[..., Any],
        params: Mapping[str, Any],
        result: Any = None,
        request_id: int | str | None = None,
        *,
        response: bool = False,
    ) -> Any:
        """Invoke a permission hook with the largest compatible public signature."""
        args: list[Any] = [dict(params)]
        if response:
            args.append(result)
            args.append(request_id)
        try:
            signature = inspect.signature(callback)
            parameters = list(signature.parameters.values())
            if any(
                item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters
            ):
                return await self._resolve_hook(callback, *args)
            args = args[: len(parameters)] if parameters else []
            return await self._resolve_hook(callback, *args)
        except (TypeError, ValueError):
            return await self._resolve_hook(callback, *args[:1])

    async def _resolve_hook(self, callback: Callable[..., Any], *args: Any) -> Any:
        """Resolve a sync or async permission hook without blocking the reader."""
        if inspect.iscoroutinefunction(callback):
            value = callback(*args)
        else:
            value = await asyncio.to_thread(callback, *args)
        return await value if inspect.isawaitable(value) else value

    def _permission_result(self, value: Any, options: list[Any]) -> dict[str, Any]:
        """Normalize a permission hook result to the v1 response shape."""
        if isinstance(value, ACPResponse):
            value = value.result
        elif hasattr(value, "to_dict") and callable(value.to_dict):
            value = value.to_dict()
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
            valid_outcome = outcome_data.get("outcome") == "cancelled" or (
                outcome_data.get("outcome") == "selected"
                and outcome_data.get("optionId")
                in {
                    str(item.get("optionId", item.get("id", "")))
                    for item in options
                    if isinstance(item, Mapping)
                }
            )
            if valid_outcome:
                result = {"outcome": outcome_data}
            else:
                result = {"outcome": {"outcome": "cancelled"}}
        else:
            option_id = data.get(
                "optionId", data.get("option_id", data.get("decision"))
            )
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
                result = {
                    "outcome": {"outcome": "selected", "optionId": str(option_id)}
                }
            else:
                result = {"outcome": {"outcome": "cancelled"}}
        return result

    async def _handle_update(self, params: Any) -> None:
        """Store, redact, and dispatch one session/update notification."""
        if not isinstance(params, Mapping):
            return
        try:
            session_id = validate_session_id(
                params.get("sessionId", params.get("session_id", ""))
            )
        except ACPError:
            return
        public = dict(redact_secrets(dict(params)))
        history = self._update_history.setdefault(session_id, [])
        history.append(public)
        if len(history) > _UPDATE_HISTORY_LIMIT:
            del history[:-_UPDATE_HISTORY_LIMIT]
        state = self._active_prompts.get(session_id)
        callback = self.on_update
        if state is not None:
            state.updates.append(public)
            callback = state.callback or callback
        if callback is not None:
            task = asyncio.create_task(self._run_callback(callback, public))
            self._callback_tasks.add(task)
            if state is not None:
                state.callback_tasks.append(task)
            task.add_done_callback(self._callback_tasks.discard)

    async def _run_callback(
        self, callback: Callable[[dict[str, Any]], Any], payload: dict[str, Any]
    ) -> None:
        """Run a user update callback without allowing it to break protocol I/O."""
        try:
            if inspect.iscoroutinefunction(callback):
                await callback(payload)
            else:
                value = await asyncio.to_thread(callback, payload)
                if inspect.isawaitable(value):
                    await value
        except Exception as exc:
            self.last_error = redact_text(str(exc))

    def _fail_pending(self, error: Exception) -> None:
        """Fail every pending request with one redacted connection error."""
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(error)
            self._mark_terminal(request_id)
        self._pending.clear()

    async def initialize(
        self,
        *,
        protocol_version: int | None = None,
        client_capabilities: Mapping[str, Any] | None = None,
        client_info: Mapping[str, Any] | None = None,
        auth_method_id: str | None = None,
    ) -> ACPCapabilities:
        """Negotiate ACP version 1 and return the agent capabilities."""
        requested = (
            self.protocol_version
            if protocol_version is None
            else normalize_protocol_version(protocol_version)
        )
        capabilities = (
            self.client_capabilities
            if client_capabilities is None
            else dict(client_capabilities)
        )
        if not isinstance(capabilities, Mapping):
            raise ACPProtocolError(-32602, "client capabilities must be an object")
        info = self.client_info if client_info is None else dict(client_info)
        result = await self._request(
            "initialize",
            {
                "protocolVersion": requested,
                "clientCapabilities": dict(capabilities),
                "clientInfo": dict(info),
            },
        )
        if not isinstance(result, Mapping):
            raise ACPProtocolError(-32602, "initialize result must be an object")
        parsed = ACPCapabilities.from_initialize_result(result)
        try:
            negotiate_protocol_version(parsed.protocol_version, {self.protocol_version})
        except ACPError as exc:
            raise ACPUnsupportedVersionError(-32000, str(exc)) from exc
        terminal_supported = _capability_enabled(capabilities, "auth.terminal")
        for method in parsed.auth_methods:
            if method.get("type") == "terminal" and not terminal_supported:
                raise ACPProtocolError(
                    -32602,
                    "agent advertised terminal authentication without client capability",
                )
        self.capabilities = parsed
        self.negotiated_version = parsed.protocol_version
        self.protocol_version = parsed.protocol_version
        if auth_method_id is not None:
            selected = parsed.auth_method(str(auth_method_id))
            if selected is not None and auth_method_is_terminal(selected):
                raise ACPAuthenticationError(
                    ACP_ERROR_AUTH_REQUIRED,
                    "terminal ACP authentication must be completed externally",
                )
            if selected is not None and not auth_method_requires_protocol_auth(
                selected
            ):
                self.authenticated = True
            else:
                await self.authenticate(auth_method_id)
        return parsed

    async def authenticate(self, method_id: str, **params: Any) -> ACPResponse:
        """Authenticate using one method advertised by initialize."""
        if self.capabilities is None:
            raise ACPProtocolError(
                -32002, "ACP client must initialize before authentication"
            )
        method_id = str(method_id or "")
        method = self.capabilities.auth_method(method_id)
        if method is None:
            raise ACPAuthenticationError(
                ACP_ERROR_AUTH_REQUIRED, "ACP authentication method is not declared"
            )
        if not auth_method_requires_protocol_auth(method):
            raise ACPAuthenticationError(
                ACP_ERROR_AUTH_REQUIRED,
                "the selected ACP v1 authentication method is not protocol-driven",
            )
        result = await self._request(
            "authenticate", {"methodId": method_id, **dict(params)}
        )
        self.authenticated = True
        return ACPResponse(id=None, result=result)

    async def new_session(
        self,
        cwd: str,
        mcp_servers: Any = None,
        *,
        timeout_s: float | None = None,
    ) -> ACPSession:
        """Create an ACP session after successful initialization/authentication."""
        if not self.initialized:
            raise ACPProtocolError(
                -32002, "ACP client must initialize before creating a session"
            )
        auth_required = (
            self.capabilities.requires_authentication if self.capabilities else False
        )
        if auth_required and not self.authenticated:
            raise ACPAuthenticationError(
                ACP_ERROR_AUTH_REQUIRED, "ACP authentication is required"
            )
        if isinstance(cwd, os.PathLike):
            cwd = os.fspath(cwd)
        if not isinstance(cwd, str) or not cwd.strip():
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "session cwd is required")
        if mcp_servers is not None and (
            not isinstance(mcp_servers, Sequence)
            or isinstance(mcp_servers, (str, bytes, bytearray))
        ):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "mcpServers must be an array"
            )
        params: dict[str, Any] = {
            "cwd": cwd,
            "mcpServers": list(mcp_servers or []),
        }
        result = await self._request("session/new", params, timeout_s)
        if not isinstance(result, Mapping):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "session/new result must be an object"
            )
        session = ACPSession.from_new_session_result(
            result,
            cwd=str(cwd),
            protocol_version=self.negotiated_version or PROTOCOL_VERSION,
        )
        self.sessions[session.session_id] = session
        return session

    create_session = new_session
    session_new = new_session

    async def prompt(
        self,
        session_id: str,
        prompt: Any = None,
        *,
        on_update: Callable[[dict[str, Any]], Any] | None = None,
        timeout_s: float | None = None,
        raise_on_timeout: bool = False,
    ) -> ACPPromptResult:
        """Send a prompt, collect ordered updates, and await its terminal result."""
        if prompt is None and isinstance(session_id, Mapping):
            params = dict(session_id)
            session_id = params.get("sessionId", params.get("session_id", ""))
            prompt = params.get("prompt", params.get("message", params.get("text")))
        if not self.initialized:
            raise ACPProtocolError(
                -32002, "ACP client must initialize before prompting"
            )
        try:
            session_id = validate_session_id(session_id)
        except ACPError:
            raise
        if session_id not in self.sessions:
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "unknown ACP session")
        blocks = normalize_prompt(prompt)
        if session_id in self._active_prompts:
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "ACP session already has an active prompt"
            )
        request_id = self._allocate_id()
        state = _PromptState(
            session_id=session_id, request_id=request_id, callback=on_update
        )
        self._active_prompts[session_id] = state
        try:
            result = await self._request(
                "session/prompt",
                {"sessionId": session_id, "prompt": blocks},
                timeout_s,
                request_id=request_id,
            )
            if state.callback_tasks:
                await asyncio.gather(*state.callback_tasks, return_exceptions=True)
            if not isinstance(result, Mapping):
                result = {"stopReason": STOP_REASON_END_TURN, "result": result}
            response = ACPPromptResult(
                stop_reason=result.get(
                    "stopReason", result.get("stop_reason", STOP_REASON_END_TURN)
                ),
                status=result.get("status", "completed_unverified"),
                verified=bool(
                    result.get("verified", result.get("completed_verified", False))
                ),
                updates=state.updates,
                result=result.get("result"),
                error=result.get("error", ""),
                session_id=session_id,
            )
            if response.stop_reason == STOP_REASON_CANCELLED:
                response.status = "cancelled"
            elif (
                response.stop_reason == STOP_REASON_REFUSAL
                and response.status not in {"blocked", "failed"}
            ):
                response.status = "failed"
            return response
        except ACPTimeoutError as exc:
            if raise_on_timeout:
                raise
            return ACPPromptResult(
                stop_reason=STOP_REASON_MAX_TURN_REQUESTS,
                status="timeout",
                updates=state.updates,
                error=redact_text(str(exc)),
                session_id=session_id,
            )
        finally:
            self._active_prompts.pop(session_id, None)

    send_prompt = prompt

    async def cancel(self, session_id: str) -> None:
        """Cancel an in-flight turn without closing the shared connection."""
        session_id = validate_session_id(session_id)
        if self.initialized and session_id not in self.sessions:
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "unknown ACP session")
        state = self._active_prompts.get(session_id)
        try:
            await self._request("session/cancel", {"sessionId": session_id})
        except ACPError as exc:
            if exc.code != ACP_ERROR_INVALID_PARAMS:
                raise
        if state is not None and state.request_id is not None:
            try:
                await self._notification("$/cancel_request", {"id": state.request_id})
            except ACPError:
                pass
        await self._cancel_permission_requests(session_id)

    async def _cancel_permission_requests(self, session_id: str) -> None:
        """Answer pending permission requests with the v1 cancelled outcome."""
        for request_id, params in list(self._permission_requests.items()):
            if params.get("sessionId", params.get("session_id", "")) != session_id:
                continue
            result = {"outcome": {"outcome": "cancelled"}}
            self._permission_cancelled_ids.add(request_id)
            task = self._permission_task_by_id.get(request_id)
            if task is not None and not task.done():
                task.cancel()
            try:
                await self._send_transport(
                    ACPResponse(id=request_id, result=result).to_dict()
                )
            except Exception as exc:
                self.last_error = redact_text(str(exc))
            self._permission_requests.pop(request_id, None)

    cancel_prompt = cancel
    cancel_session = cancel

    async def set_mode(self, session_id: str, mode_id: str) -> dict[str, Any]:
        """Set the active ACP session mode and return the wire result."""
        if not self.initialized:
            raise ACPProtocolError(
                -32002, "ACP client must initialize before setting a mode"
            )
        session_id = validate_session_id(session_id)
        if not isinstance(mode_id, str) or not mode_id or mode_id.strip() != mode_id:
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "mode id is invalid")
        if self.initialized and session_id not in self.sessions:
            raise ACPProtocolError(ACP_ERROR_INVALID_PARAMS, "unknown ACP session")
        result = await self._request(
            "session/set_mode", {"sessionId": session_id, "modeId": mode_id}
        )
        if not isinstance(result, Mapping):
            raise ACPProtocolError(
                ACP_ERROR_INVALID_PARAMS, "session/set_mode result must be an object"
            )
        if session_id in self.sessions:
            self.sessions[session_id].modes["currentModeId"] = mode_id
        return dict(result)

    set_session_mode = set_mode

    def drain_updates(self, session_id: str) -> list[dict[str, Any]]:
        """Return and clear the currently buffered updates for a session."""
        session_id = validate_session_id(session_id)
        values = self._update_history.get(session_id, [])
        self._update_history[session_id] = []
        return [dict(item) for item in values]

    async def updates_for(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield future session updates in transport arrival order."""
        session_id = validate_session_id(session_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        old_handler = self.on_update

        def handler(payload: dict[str, Any]) -> None:
            if (
                str(payload.get("sessionId", payload.get("session_id", "")))
                == session_id
            ):
                queue.put_nowait(dict(payload))

        self.on_update = handler
        try:
            while not self.closed:
                yield await queue.get()
        finally:
            if self.on_update is old_handler:
                self.on_update = old_handler

    receive_updates = updates_for

    async def connect(self) -> "ACPClient":
        """Start the transport reader without changing negotiation state."""
        await self._start_transport()
        return self

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        """Send an arbitrary JSON-RPC request through the active transport."""
        return await self._request(method, params, timeout_s)

    async def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> None:
        """Send an arbitrary JSON-RPC notification through the transport."""
        await self._notification(method, params)

    def close(self) -> ImmediateAwaitable[None]:
        """Close the client, fail pending calls, and close its owned transport."""
        if self.closed:
            return ImmediateAwaitable(None)
        self.closed = True
        reader = self._reader_task
        if reader is not None and not reader.done():
            try:
                reader.cancel()
            except RuntimeError:
                pass
        self._fail_pending(ACPTransportClosed(-32000, "ACP client closed"))
        self._deferred_responses.clear()
        for task in list(self._callback_tasks):
            if not task.done():
                task.cancel()
        for task in list(self._permission_tasks):
            if not task.done():
                task.cancel()
        if not self.owns_transport:
            return ImmediateAwaitable(None)
        try:
            result = self.transport.close()
        except Exception as exc:
            self.last_error = redact_text(str(exc))
            return ImmediateAwaitable(None)
        if isinstance(result, ImmediateAwaitable):
            return result
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                run_sync(result)
            else:

                async def await_transport_close() -> None:
                    await result

                task = asyncio.create_task(await_transport_close())
                self._callback_tasks.add(task)
                task.add_done_callback(self._callback_tasks.discard)
                return ImmediateAwaitable(task)
        return ImmediateAwaitable(None)

    async def shutdown(self) -> None:
        """Send the optional shutdown request and close the transport."""
        if self.initialized and not self.closed:
            try:
                await self._request("shutdown", {})
            except ACPError:
                pass
        await call_maybe_async(self, "close")

    aclose = close

    def close_sync(self) -> None:
        """Close the client from synchronous code."""
        run_sync(self.close())

    def _sync_call(self, operation: Any) -> Any:
        """Resolve an async operation from code without a running loop."""
        return asyncio.run(operation)

    def initialize_sync(self, **kwargs: Any) -> ACPCapabilities:
        """Synchronously negotiate ACP initialization."""
        return self._sync_call(self.initialize(**kwargs))

    def new_session_sync(self, *args: Any, **kwargs: Any) -> ACPSession:
        """Synchronously create an ACP session."""
        return self._sync_call(self.new_session(*args, **kwargs))

    def prompt_sync(self, *args: Any, **kwargs: Any) -> ACPPromptResult:
        """Synchronously send a prompt and collect its result."""
        return self._sync_call(self.prompt(*args, **kwargs))

    def authenticate_sync(self, *args: Any, **kwargs: Any) -> ACPResponse:
        """Synchronously authenticate the connection."""
        return self._sync_call(self.authenticate(*args, **kwargs))

    def set_mode_sync(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Synchronously set a session mode."""
        return self._sync_call(self.set_mode(*args, **kwargs))

    def __enter__(self) -> "ACPClient":
        """Enter a synchronous client context."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the client on synchronous context exit."""
        self.close_sync()
        return False

    async def __aenter__(self) -> "ACPClient":
        """Enter an asynchronous client context without initializing."""
        await self._start_transport()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the client on asynchronous context exit."""
        await call_maybe_async(self, "close")
        return False


def _safe_client_id(value: Any) -> int | str | None:
    """Return a safe ID for errors about malformed peer requests."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return value


def _capability_enabled(capabilities: Mapping[str, Any], path: str) -> bool:
    """Return a nested boolean client capability value."""
    value: Any = capabilities
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False
        value = value[part]
    return value is not None if isinstance(value, Mapping) else bool(value)


def _permission_option_id(options: list[Any], *, allow: bool) -> str:
    """Choose a safe permission option from a v1 option array."""
    for item in options:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        if allow and kind in {"allow_once", "allow_always", "allow"}:
            return str(item.get("optionId", item.get("id", "")))
    for item in options:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        if not allow and kind in {"reject_once", "reject_always", "reject", "deny"}:
            return str(item.get("optionId", item.get("id", "")))
    return ""


ACPClientAdapter = ACPClient

__all__ = ["ACPClient", "ACPClientAdapter"]
