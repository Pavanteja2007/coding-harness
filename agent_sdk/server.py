"""Local threaded HTTP, SSE, WebSocket, workspace, and OpenAI-compatible server."""

from __future__ import annotations

import base64
import hashlib
import json
import select
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from shared.security import redact_secrets, redact_text

from .catalog import (
    catalog_get,
    catalog_list,
    catalog_schema,
    catalog_search,
    safe_catalog_json,
    tool_to_wire,
)
from .errors import (
    ConflictError,
    EventReplayError,
    InvalidRequestError,
    MissingVersionError,
    RunNotFoundError,
    ToolCatalogError,
    ToolNotFoundError,
    ToolResolutionError,
    UnsupportedVersionError,
    WorkspaceActiveError,
    WorkspaceNotFoundError,
)
from .local import LocalTransport
from .models import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    SUPPORTED_SCHEMA_VERSIONS,
    Event,
    EventEnvelope,
    Result,
    RunRequest,
    VersionNegotiation,
    new_session_id,
)
from .transport import RunHandle

_MAX_WEBSOCKET_PAYLOAD = 1024 * 1024

__all__ = ["AgentServer", "Server"]


class _HTTPProblem(Exception):
    """Internal typed HTTP failure used by the request handler."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Store a redacted HTTP status, code, and safe details."""
        self.status = int(status)
        self.code = str(code)
        self.message = redact_text(str(message))[:1000]
        self.details = dict(details or {})
        super().__init__(self.message)


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server with reusable loopback bindings."""

    allow_reuse_address = True
    daemon_threads = True


class AgentServer:
    """Serve the public SDK protocol from a loopback-only threaded HTTP server."""

    def __init__(
        self,
        repo_path: str = "",
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        log_root: str = "",
        config: Mapping[str, Any] | None = None,
        model: Any = None,
        call_fn: Any = None,
        model_fn: Any = None,
        verifier: Any = None,
        workspace_root: str = "",
        token: str = "",
        auth_token: str = "",
        transport: Any = None,
        allow_non_loopback: bool = False,
        hooks: Any = None,
        hook_manager: Any = None,
        tool_catalog: Any = None,
        catalog: Any = None,
    ) -> None:
        """Create a server without binding until start is called."""
        if hooks is not None and hook_manager is not None and hooks is not hook_manager:
            raise InvalidRequestError(
                "hooks and hook_manager must identify the same manager"
            )
        if (
            tool_catalog is not None
            and catalog is not None
            and tool_catalog is not catalog
        ):
            raise InvalidRequestError(
                "tool_catalog and catalog must identify the same catalog"
            )
        self.hooks = hook_manager if hook_manager is not None else hooks
        self.hook_manager = self.hooks
        self.tool_catalog = tool_catalog if tool_catalog is not None else catalog
        self.catalog = self.tool_catalog
        self.host = str(host or "127.0.0.1")
        if not allow_non_loopback and not _is_loopback(self.host):
            raise InvalidRequestError(
                "AgentServer binds to loopback addresses by default"
            )
        self.port = int(port or 0)
        self.token = str(token or auth_token or "")
        self._owns_transport = transport is None
        self._transport_options = {
            "repo_path": repo_path,
            "log_root": log_root or None,
            "config": config,
            "model": model,
            "call_fn": call_fn,
            "model_fn": model_fn,
            "verifier": verifier,
            "workspace_root": workspace_root or None,
        }
        self.transport = transport or self._new_transport()
        if self.tool_catalog is None:
            self.tool_catalog = getattr(
                self.transport, "tool_catalog", getattr(self.transport, "catalog", None)
            )
            self.catalog = self.tool_catalog
        if self.hooks is None:
            self.hooks = getattr(
                self.transport, "hooks", getattr(self.transport, "hook_manager", None)
            )
            self.hook_manager = self.hooks
        if self.hooks is not None and hasattr(self.transport, "hooks"):
            self.transport.hooks = self.hooks
            self.transport.hook_manager = self.hooks
        if self.tool_catalog is not None and hasattr(self.transport, "tool_catalog"):
            self.transport.tool_catalog = self.tool_catalog
            self.transport.catalog = self.tool_catalog
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        self._lock = threading.RLock()

    def _new_transport(self) -> LocalTransport:
        """Create a fresh local transport for a restartable server."""
        options = dict(self._transport_options)
        return LocalTransport(
            **options,
            hooks=self.hooks,
            tool_catalog=self.tool_catalog,
        )

    @property
    def capabilities(self) -> dict[str, Any]:
        """Return server capabilities for protocol discovery."""
        return {
            "local": True,
            "rest": True,
            "events": True,
            "sse": True,
            "websocket": True,
            "openai": True,
            "workspaces": True,
            "query": True,
            "run": True,
            "tools": True,
            "tool_catalog": self.tool_catalog is not None,
            "hooks": self.hooks is not None,
        }

    @property
    def address(self) -> tuple[str, int]:
        """Return the bound host and port."""
        if self._httpd is None:
            return self.host, self.port
        server_address = self._httpd.server_address
        return str(server_address[0]), int(server_address[1])

    @property
    def url(self) -> str:
        """Return the server base URL."""
        host, port = self.address
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{port}"

    @property
    def base_url(self) -> str:
        """Return the server base URL under a compatibility alias."""
        return self.url

    def list_tools(self) -> list[dict[str, Any]]:
        """List configured catalog descriptors without resolving deferred schemas."""
        return [tool_to_wire(tool) for tool in catalog_list(self.tool_catalog)]

    def search_tools(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Search configured catalog metadata without resolving deferred schemas."""
        selected_limit = max_results if limit is None else limit
        return [
            tool_to_wire(tool)
            for tool in catalog_search(self.tool_catalog, query, selected_limit)
        ]

    def get_tool(self, name: str) -> dict[str, Any]:
        """Return one exact configured catalog descriptor without schema resolution."""
        return tool_to_wire(catalog_get(self.tool_catalog, name))

    def search(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Search configured catalog metadata under the short alias."""
        return self.search_tools(query, max_results, limit=limit)

    def get(self, name: str) -> dict[str, Any]:
        """Return one exact configured catalog descriptor under the short alias."""
        return self.get_tool(name)

    def resolve_tool_schema(self, name: str) -> Any:
        """Resolve one exact configured catalog schema on explicit request."""
        return catalog_schema(self.tool_catalog, name)

    def resolve(self, name: str) -> Any:
        """Resolve one exact configured catalog schema under a short alias."""
        return self.resolve_tool_schema(name)

    def resolve_schema(self, name: str) -> Any:
        """Resolve one exact configured catalog schema under a short alias."""
        return self.resolve_tool_schema(name)

    def resolve_tool(self, name: str) -> Any:
        """Resolve one exact configured catalog schema under a tool alias."""
        return self.resolve_tool_schema(name)

    def version(self) -> dict[str, Any]:
        """Return the server's version-negotiation payload."""
        return {
            "server": "neo-agent-server",
            "protocol_version": PROTOCOL_VERSION,
            "protocol": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "schema": SCHEMA_VERSION,
            "event_schema_version": SCHEMA_VERSION,
            "version": PROTOCOL_VERSION,
            "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "supported_schema_versions": list(SUPPORTED_SCHEMA_VERSIONS),
            "capabilities": self.capabilities,
        }

    def _bind(self) -> ThreadingHTTPServer:
        """Bind one HTTP listener and attach this server facade."""
        if self._httpd is not None:
            return self._httpd
        self._httpd = _LoopbackHTTPServer((self.host, self.port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.agent_server = self
        self.host, self.port = self.address
        self._stopped = False
        return self._httpd

    def start(self) -> "AgentServer":
        """Bind the loopback server and serve it on a background thread."""
        with self._lock:
            if self._httpd is not None:
                return self
            if self._stopped and not self._owns_transport:
                raise InvalidRequestError(
                    "injected server transports are not restartable"
                )
            if self._stopped and self._owns_transport:
                self.transport = self._new_transport()
            httpd = self._bind()
            self._thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="neo-agent-server",
                daemon=True,
            )
            self._thread.start()
            return self

    def start_background(self) -> "AgentServer":
        """Start the server and return this instance."""
        return self.start()

    def serve_forever(self) -> None:
        """Bind if needed and serve synchronously until stopped."""
        with self._lock:
            if self._httpd is None:
                if self._stopped and not self._owns_transport:
                    raise InvalidRequestError(
                        "injected server transports are not restartable"
                    )
                if self._stopped and self._owns_transport:
                    self.transport = self._new_transport()
                httpd = self._bind()
            else:
                httpd = self._httpd
        httpd.serve_forever(poll_interval=0.05)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop HTTP serving and close the local transport."""
        with self._lock:
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, float(timeout)))
        self.transport.close()
        self._stopped = True

    shutdown = stop

    def close(self) -> None:
        """Stop the server under a context-manager-compatible alias."""
        self.stop()

    def __enter__(self) -> "AgentServer":
        """Start the server on context entry."""
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """Stop the server on context exit."""
        self.stop()


Server = AgentServer


class _Handler(BaseHTTPRequestHandler):
    """Protocol handler; all responses are JSON except explicit SSE/WebSocket streams."""

    protocol_version = "HTTP/1.1"
    server_version = "NeoAgentServer/1"

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default request logging so bearer headers and prompts are not exposed."""

    @property
    def agent_server(self) -> AgentServer:
        """Return the owning AgentServer."""
        return self.server.agent_server

    def do_GET(self) -> None:
        """Handle GET, health, events, replay, and WebSocket routes."""
        self._handle("GET")

    def do_POST(self) -> None:
        """Handle POST mutation and run routes."""
        self._handle("POST")

    def do_DELETE(self) -> None:
        """Handle DELETE workspace routes."""
        self._handle("DELETE")

    def do_PUT(self) -> None:
        """Handle PUT as an alias for JSON mutation routes."""
        self._handle("PUT")

    def do_OPTIONS(self) -> None:
        """Return a minimal CORS preflight response."""
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            body = self._read_json()
            self._check_auth()
            if method == "GET" and _is_websocket(self.headers):
                self._check_version(query, body, required=True)
                self._websocket(path, query)
                return
            self._dispatch(method, path, query, body)
        except _HTTPProblem as exc:
            self._send_problem(exc)
        except MissingVersionError as exc:
            self._send_problem(_HTTPProblem(400, "missing_version", str(exc)))
        except UnsupportedVersionError as exc:
            self._send_problem(
                _HTTPProblem(
                    409,
                    "unsupported_version",
                    str(exc),
                    {
                        "supported_protocol_versions": list(
                            SUPPORTED_PROTOCOL_VERSIONS
                        ),
                        "supported_schema_versions": list(SUPPORTED_SCHEMA_VERSIONS),
                    },
                )
            )
        except RunNotFoundError as exc:
            self._send_problem(_HTTPProblem(404, "run_not_found", str(exc)))
        except WorkspaceNotFoundError as exc:
            self._send_problem(_HTTPProblem(404, "workspace_not_found", str(exc)))
        except WorkspaceActiveError as exc:
            self._send_problem(_HTTPProblem(409, "workspace_active", str(exc)))
        except ConflictError as exc:
            self._send_problem(
                _HTTPProblem(409, getattr(exc, "code", "conflict"), str(exc))
            )
        except (InvalidRequestError, EventReplayError) as exc:
            self._send_problem(_HTTPProblem(400, "invalid_request", str(exc)))
        except TimeoutError as exc:
            self._send_problem(_HTTPProblem(504, "timeout", str(exc)))
        except Exception as exc:
            self._send_problem(_HTTPProblem(500, "internal_error", str(exc)))

    def _dispatch(
        self, method: str, path: str, query: dict[str, Any], body: dict[str, Any]
    ) -> None:
        parts = [part for part in path.split("/") if part]
        if parts and parts[0] == "api":
            parts = parts[1:]
        if not parts:
            raise _HTTPProblem(404, "not_found", "route not found")
        original_parts = list(parts)
        if (
            original_parts[0] in {"runs", "conversations", "workspaces", "tools"}
            and len(original_parts) > 1
        ):
            _safe_route_id(original_parts[1])
        if parts and parts[0] == "v1":
            parts = parts[1:]
        if parts and parts[0] == "openai":
            parts = parts[1:]
        if (
            parts
            and parts[0] in {"runs", "conversations", "workspaces", "tools"}
            and len(parts) > 1
        ):
            _safe_route_id(parts[1])
        if not parts:
            raise _HTTPProblem(404, "not_found", "route not found")
        public_discovery = parts[0] in {"health", "capabilities", "version"}
        is_openai = original_parts[:2] == ["v1", "models"] or original_parts[:3] == [
            "v1",
            "chat",
            "completions",
        ]
        requires_version = not public_discovery and not is_openai
        self._check_version(query, body, required=requires_version)
        if is_openai and original_parts[:2] == ["v1", "models"] and method == "GET":
            self._openai_models()
            return
        if (
            is_openai
            and original_parts[:3] == ["v1", "chat", "completions"]
            and method == "POST"
        ):
            self._openai_chat(body, query)
            return
        if parts[0] == "health" and method == "GET":
            self._send_json(
                {"status": "ok", "healthy": True, "server": "neo-agent-server"}
            )
            return
        if parts[0] in {"capabilities", "version"} and method in {"GET", "POST"}:
            payload = self.agent_server.version()
            payload["negotiation"] = VersionNegotiation(
                capabilities=self.agent_server.capabilities,
                server="neo-agent-server",
            ).to_dict()
            self._send_json(payload)
            return
        if parts[0] == "query" and method == "POST":
            self._query(body)
            return
        if parts[0] in {"run", "runs"} and method == "POST" and len(parts) == 1:
            self._start_run(body)
            return
        if parts[0] == "runs" and method == "GET" and len(parts) == 1:
            self._send_json({"runs": self.agent_server.transport.list_runs()})
            return
        if parts[0] == "runs" and len(parts) >= 2:
            run_id = parts[1]
            if len(parts) == 2 and method == "GET":
                self._run_status(run_id)
                return
            if len(parts) == 3 and parts[2] in {"status", "state"} and method == "GET":
                self._run_status(run_id)
                return
            if (
                len(parts) == 3
                and parts[2] in {"cancel", "stop"}
                and method in {"POST", "DELETE"}
            ):
                self._cancel_run(run_id)
                return
            if len(parts) == 3 and parts[2] == "resume" and method == "POST":
                self._resume_run(run_id, body)
                return
            if len(parts) == 3 and parts[2] == "replay" and method == "GET":
                projection = self.agent_server.transport.replay(run_id)
                self._send_json(
                    projection.to_dict()
                    if hasattr(projection, "to_dict")
                    else projection
                )
                return
            if len(parts) == 3 and parts[2] in {"events", "stream"} and method == "GET":
                self._events(run_id, query, self.headers)
                return
        if parts[0] == "conversations" and len(parts) >= 2:
            session_id = parts[1]
            if len(parts) == 3 and parts[2] == "history" and method == "GET":
                limit = _int_or_none(query.get("limit"))
                self._send_json(
                    {
                        "history": self.agent_server.transport.history(
                            session_id, limit=limit
                        )
                    }
                )
                return
            if len(parts) == 3 and parts[2] == "close" and method == "POST":
                closer = getattr(
                    self.agent_server.transport, "close_conversation", None
                )
                if callable(closer):
                    closer(session_id)
                self._send_json({"session_id": session_id, "closed": True})
                return
        if parts[0] == "tools" and method == "GET":
            self._tools(parts[1:], query)
            return
        if parts[0] == "workspaces":
            self._workspaces(method, parts[1:], query, body)
            return
        raise _HTTPProblem(404, "not_found", "route not found")

    def _check_auth(self) -> None:
        expected = self.agent_server.token
        if not expected:
            return
        value = self.headers.get("Authorization", "")
        if value != f"Bearer {expected}":
            raise _HTTPProblem(
                401, "authentication_required", "bearer authentication required"
            )

    def _check_version(
        self, query: Mapping[str, Any], body: Mapping[str, Any], *, required: bool
    ) -> VersionNegotiation:
        try:
            return VersionNegotiation.from_request(
                dict(self.headers.items()), dict(body), dict(query), required=required
            )
        except MissingVersionError as exc:
            raise _HTTPProblem(400, "missing_version", str(exc)) from exc
        except UnsupportedVersionError as exc:
            raise _HTTPProblem(409, "unsupported_version", str(exc)) from exc

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _HTTPProblem(400, "invalid_json", "invalid content length") from exc
        if length < 0 or length > 8 * 1024 * 1024:
            raise _HTTPProblem(413, "body_too_large", "request body is too large")
        if not length:
            return {}
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise _HTTPProblem(
                400, "invalid_json", "request body is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise _HTTPProblem(400, "invalid_json", "request body must be an object")
        return value

    def _repo(self, body: Mapping[str, Any]) -> str:
        configured = str(getattr(self.agent_server.transport, "repo_path", "") or "")
        if not configured:
            raise _HTTPProblem(400, "missing_repo", "repo_path is required")
        value = str(body.get("repo_path", body.get("repository", "")) or "")
        if value:
            raw = Path(value).expanduser().absolute()
            candidate = raw.resolve()
            root = Path(configured).expanduser().absolute().resolve()
            if raw.is_symlink() or candidate != root:
                raise _HTTPProblem(
                    403, "repo_forbidden", "repo_path is outside the configured root"
                )
        return configured

    def _request(
        self, body: Mapping[str, Any], *, strategy: str, wait: bool
    ) -> RunRequest:
        return RunRequest(
            request=str(
                body.get("request", body.get("prompt", body.get("input", ""))) or ""
            ),
            repo_path=self._repo(body),
            session_id=str(
                body.get("session_id", body.get("conversation_id", "")) or ""
            ),
            run_id=str(body.get("run_id", "") or ""),
            strategy=str(body.get("strategy", strategy) or strategy),
            config=dict(body.get("config", {}) or {}),
            verification_policy=dict(
                body.get("verification_policy", body.get("verification", {})) or {}
            ),
            workspace_policy=dict(body.get("workspace_policy", {}) or {}),
            workspace_id=str(body.get("workspace_id", body.get("workspace", "")) or ""),
            metadata=dict(body.get("metadata", {}) or {}),
            wait=bool(body.get("wait", wait)),
            resume=bool(body.get("resume", False)),
        )

    def _query(self, body: dict[str, Any]) -> None:
        request = self._request(body, strategy="question", wait=True)
        if not request.request:
            raise _HTTPProblem(400, "missing_request", "request is required")
        result = self.agent_server.transport.query(request)
        result_data = result.to_dict()
        self._send_json(
            {
                **result_data,
                "result": result_data,
                "run_id": result.run_result.run_id,
                "session_id": result.run_result.session_id,
            }
        )

    def _start_run(self, body: dict[str, Any]) -> None:
        request = self._request(body, strategy="daily", wait=False)
        if not request.request:
            raise _HTTPProblem(400, "missing_request", "request is required")
        if request.wait:
            result = self.agent_server.transport.run(request, wait=True)
            result_data = result.to_dict()
            self._send_json(
                {
                    **result_data,
                    "result": result_data,
                    "run_id": result.run_result.run_id,
                    "session_id": result.run_result.session_id,
                    "done": True,
                }
            )
            return
        handle = self.agent_server.transport.run(request, wait=False)
        if isinstance(handle, RunHandle):
            payload = self.agent_server.transport.status(handle.run_id)
        else:
            result = handle if isinstance(handle, Result) else Result(handle)
            payload = result.to_dict()
            payload["done"] = True
        payload = dict(payload)
        payload.setdefault("run_id", request.run_id)
        self._send_json(payload, status=202)

    def _run_status(self, run_id: str) -> None:
        status = self.agent_server.transport.status(run_id)
        if status.get("done") or status.get("terminal"):
            try:
                result = self.agent_server.transport.wait(run_id, timeout=0.1)
                status["result"] = result.to_dict()
            except RunNotFoundError:
                raise
            except EventReplayError as exc:
                raise _HTTPProblem(400, "invalid_run_result", str(exc)) from exc
            except TimeoutError as exc:
                raise _HTTPProblem(504, "result_timeout", str(exc)) from exc
            except Exception as exc:
                raise _HTTPProblem(500, "result_unavailable", str(exc)) from exc
        self._send_json({"run": status, **status})

    def _cancel_run(self, run_id: str) -> None:
        accepted = self.agent_server.transport.cancel(run_id)
        self._send_json(
            {"run_id": run_id, "cancelled": bool(accepted), "accepted": bool(accepted)}
        )

    def _resume_run(self, run_id: str, body: dict[str, Any]) -> None:
        request = str(body.get("request", "") or "")
        wait = bool(body.get("wait", True))
        resume_options = {
            key: body[key]
            for key in (
                "strategy",
                "config",
                "metadata",
                "verification_policy",
                "workspace_policy",
                "workspace_id",
                "session_id",
            )
            if key in body
        }
        result = self.agent_server.transport.resume(
            run_id, request, wait=wait, **resume_options
        )
        if isinstance(result, RunHandle):
            self._send_json(
                self.agent_server.transport.status(result.run_id), status=202
            )
            return
        result_data = result.to_dict()
        self._send_json(
            {
                **result_data,
                "result": result_data,
                "run_id": result.run_result.run_id,
                "session_id": result.run_result.session_id,
                "done": True,
            }
        )

    def _events(self, run_id: str, query: Mapping[str, Any], headers: Any) -> None:
        after = _sequence_from_headers(query, headers)
        accept = str(headers.get("Accept", "")).casefold()
        wants_sse = "text/event-stream" in accept or str(
            query.get("stream", "")
        ).lower() in {"1", "true", "yes"}
        try:
            if wants_sse:
                self._sse(run_id, after)
            else:
                rows = self.agent_server.transport.read_events(
                    run_id, after_sequence=after
                )
                payload = {
                    "run_id": run_id,
                    "events": [
                        {
                            **item.to_dict(),
                            "protocol_version": PROTOCOL_VERSION,
                        }
                        for item in rows
                    ],
                    "last_sequence": rows[-1].sequence if rows else after,
                    "protocol_version": PROTOCOL_VERSION,
                    "schema_version": SCHEMA_VERSION,
                }
                self._send_json(payload)
        except (KeyError, ValueError) as exc:
            raise _HTTPProblem(400, "invalid_sequence", str(exc)) from exc

    def _sse(self, run_id: str, after: int) -> None:
        events = self.agent_server.transport.events(run_id, after_sequence=after)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Neo-Protocol-Version", str(PROTOCOL_VERSION))
        self.send_header("X-Neo-Schema-Version", str(SCHEMA_VERSION))
        self.end_headers()
        self.wfile.write(b"retry: 1000\n\n")
        self.wfile.flush()
        try:
            for event in events.iter_after_sequence(after, wait=True, timeout=30.0):
                self._write_sse(event)
                if event.event_type == "run_finished":
                    break
        except (BrokenPipeError, ConnectionResetError, EventReplayError, OSError):
            return
        self.close_connection = True

    def _write_sse(self, event: Event) -> None:
        envelope = EventEnvelope(event=event)
        data = json.dumps(envelope.to_dict(), ensure_ascii=False, default=str)
        block = f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"
        self.wfile.write(block.encode("utf-8"))
        self.wfile.flush()

    def _tools(self, parts: list[str], query: Mapping[str, Any]) -> None:
        """Serve bounded local catalog discovery and explicit schema resolution."""
        try:
            if not parts:
                tools = catalog_list(self.agent_server.tool_catalog)
                self._send_json(
                    {
                        "tools": [tool_to_wire(tool) for tool in tools],
                        "count": len(tools),
                    }
                )
                return
            if parts == ["search"]:
                raw_limit = query.get("limit", "10")
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError) as exc:
                    raise _HTTPProblem(
                        400, "invalid_tool_query", "limit must be an integer"
                    ) from exc
                if limit < 0:
                    raise _HTTPProblem(
                        400, "invalid_tool_query", "limit must be non-negative"
                    )
                tools = catalog_search(
                    self.agent_server.tool_catalog, str(query.get("q", "")), limit
                )
                self._send_json(
                    {
                        "tools": [tool_to_wire(tool) for tool in tools],
                        "query": safe_catalog_json(str(query.get("q", ""))),
                        "count": len(tools),
                    }
                )
                return
            if len(parts) == 1:
                tool = catalog_get(self.agent_server.tool_catalog, parts[0])
                payload = tool_to_wire(tool)
                self._send_json({"tool": payload, **payload})
                return
            if len(parts) == 2 and parts[1] == "schema":
                name = parts[0]
                descriptor = catalog_get(self.agent_server.tool_catalog, name)
                schema = catalog_schema(self.agent_server.tool_catalog, name)
                self._send_json(
                    {
                        "name": safe_catalog_json(name),
                        "schema": schema,
                        "deferred": bool(getattr(descriptor, "deferred", False)),
                    }
                )
                return
        except ToolNotFoundError as exc:
            raise _HTTPProblem(404, "tool_not_found", str(exc)) from exc
        except ToolResolutionError as exc:
            raise _HTTPProblem(502, "tool_resolution_failed", str(exc)) from exc
        except ToolCatalogError as exc:
            raise _HTTPProblem(503, "tool_catalog_error", str(exc)) from exc
        except _HTTPProblem:
            raise
        except Exception as exc:
            raise _HTTPProblem(
                503, "tool_catalog_error", "tool catalog operation failed"
            ) from exc
        raise _HTTPProblem(404, "not_found", "tool route not found")

    def _workspaces(
        self,
        method: str,
        parts: list[str],
        query: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> None:
        if not parts and method == "POST":
            source_path = str(body.get("source_path", body.get("repo_path", "")) or "")
            if source_path:
                self._repo({"repo_path": source_path})
            record = self.agent_server.transport.create_workspace(
                str(body.get("name", "") or ""),
                workspace_id=str(body.get("workspace_id", body.get("id", "")) or ""),
                source_path=source_path,
                metadata=dict(body.get("metadata", {}) or {}),
            )
            self._send_json({"workspace": record, **record}, status=201)
            return
        if not parts and method == "GET":
            include_deleted = str(query.get("include_deleted", "")).lower() in {
                "1",
                "true",
                "yes",
            }
            self._send_json(
                {
                    "workspaces": self.agent_server.transport.list_workspaces(
                        include_deleted=include_deleted
                    )
                }
            )
            return
        if len(parts) == 1:
            workspace_id = parts[0]
            if method == "GET":
                include_deleted = str(query.get("include_deleted", "")).lower() in {
                    "1",
                    "true",
                    "yes",
                }
                record = self.agent_server.transport.get_workspace(
                    workspace_id, include_deleted=include_deleted
                )
                self._send_json({"workspace": record, **record})
                return
            if method in {"DELETE", "PUT"}:
                record = self.agent_server.transport.delete_workspace(workspace_id)
                self._send_json({"workspace": record, **record})
                return
        raise _HTTPProblem(404, "not_found", "workspace route not found")

    def _openai_models(self) -> None:
        now = int(time.time())
        self._send_json(
            {
                "object": "list",
                "data": [
                    {
                        "id": "neo-agent",
                        "object": "model",
                        "created": now,
                        "owned_by": "local",
                    }
                ],
            },
            include_protocol=False,
        )

    def _openai_chat(self, body: dict[str, Any], query: Mapping[str, Any]) -> None:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise _HTTPProblem(
                400, "invalid_messages", "messages must be a non-empty array"
            )
        prompt_parts: list[str] = []
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(item.get("text", ""))
                    if isinstance(item, Mapping)
                    else str(item)
                    for item in content
                )
            prompt_parts.append(f"{message.get('role', 'user')}: {content}")
        prompt = "\n".join(prompt_parts)
        if not prompt.strip():
            raise _HTTPProblem(400, "invalid_messages", "messages contain no text")
        request = RunRequest(
            request=prompt,
            repo_path=self._repo(body),
            session_id=str(body.get("session_id", "") or "") or new_session_id(),
            strategy="question",
            wait=True,
        )
        result = self.agent_server.transport.query(request)
        if result.status not in {"completed_unverified", "completed_verified"}:
            status_code = 409 if result.status in {"needs_input", "blocked"} else 502
            raise _HTTPProblem(
                status_code,
                f"agent_{result.status}",
                redact_text(result.error or result.status),
            )
        answer = redact_text(result.answer)
        model = str(body.get("model", "neo-agent") or "neo-agent")
        created = int(time.time())
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        if bool(body.get("stream", False)):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": answer},
                        "finish_reason": None,
                    }
                ],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            final = dict(chunk)
            final["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            self.wfile.write(f"data: {json.dumps(final)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        prompt_tokens = max(1, len(prompt.split()))
        completion_tokens = max(1, len(answer.split()))
        self._send_json(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
            include_protocol=False,
        )

    def _websocket(self, path: str, query: Mapping[str, Any]) -> None:
        upgrade = str(self.headers.get("Upgrade", "")).casefold()
        connection = str(self.headers.get("Connection", "")).casefold()
        key = self.headers.get("Sec-WebSocket-Key", "")
        version = str(self.headers.get("Sec-WebSocket-Version", ""))
        if (
            upgrade != "websocket"
            or "upgrade" not in connection
            or not key
            or version != "13"
        ):
            raise _HTTPProblem(
                400, "websocket_handshake", "invalid websocket handshake"
            )
        parts = [part for part in path.split("/") if part]
        if parts and parts[0] == "api":
            parts = parts[1:]
        if parts and parts[0] == "v1":
            parts = parts[1:]
        run_id = query.get("run_id", "")
        if len(parts) >= 3 and parts[0] == "runs" and parts[2] in {"events", "stream"}:
            run_id = parts[1]
        if not run_id:
            raise _HTTPProblem(400, "missing_run", "websocket run_id is required")
        _safe_route_id(str(run_id))
        after = _int_or_none(query.get("after"))
        if after is None:
            after = 0
        try:
            self.agent_server.transport.status(str(run_id))
        except RunNotFoundError as exc:
            raise _HTTPProblem(404, "run_not_found", str(exc)) from exc
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self._serve_websocket_frames(str(run_id), query)

    def _serve_websocket_frames(self, run_id: str, query: Mapping[str, Any]) -> None:
        sock = self.connection
        sock.settimeout(0.05)
        cursor = _int_or_none(query.get("after")) or 0
        buffer = b""
        last_ping = time.monotonic()
        try:
            while True:
                try:
                    rows = self.agent_server.transport.read_events(
                        run_id, after_sequence=cursor
                    )
                except (RunNotFoundError, EventReplayError):
                    rows = []
                for event in rows:
                    _ws_send(
                        sock,
                        1,
                        json.dumps(
                            EventEnvelope(event=event).to_dict(), default=str
                        ).encode("utf-8"),
                    )
                    cursor = event.sequence
                status = self.agent_server.transport.status(run_id)
                if status.get("terminal") and not rows:
                    _ws_send(sock, 8, struct_pack_close(1000, "complete"))
                    return
                if time.monotonic() - last_ping >= 15.0:
                    _ws_send(sock, 9, b"neo-server")
                    last_ping = time.monotonic()
                readable, _, _ = select.select([sock], [], [], 0.03)
                if not readable:
                    continue
                chunk = sock.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                while True:
                    frame = _ws_read_frame(buffer)
                    if frame is None:
                        break
                    buffer = frame[2]
                    opcode, payload = frame[0], frame[1]
                    if opcode == 8:
                        _ws_send(sock, 8, struct_pack_close(1000, "bye"))
                        return
                    if opcode == 9:
                        _ws_send(sock, 10, payload)
                        continue
                    if opcode == 10:
                        continue
                    if opcode == 1:
                        try:
                            message = json.loads(payload.decode("utf-8"))
                        except (UnicodeError, ValueError):
                            message = {}
                        if not isinstance(message, Mapping):
                            message = {}
                        action = str(
                            message.get("type", message.get("action", ""))
                        ).casefold()
                        if action in {"cancel", "stop"}:
                            accepted = bool(self.agent_server.transport.cancel(run_id))
                            message_type = "cancelled" if accepted else "cancel_pending"
                            _ws_send(
                                sock,
                                1,
                                json.dumps(
                                    {
                                        "type": message_type,
                                        "run_id": run_id,
                                        "cancelled": accepted,
                                        "accepted": accepted,
                                        "protocol_version": PROTOCOL_VERSION,
                                        "schema_version": SCHEMA_VERSION,
                                    }
                                ).encode("utf-8"),
                            )
                        elif action in {"ping", "keepalive"}:
                            _ws_send(
                                sock, 1, json.dumps({"type": "pong"}).encode("utf-8")
                            )
        except (OSError, ValueError, KeyError):
            return
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Neo-Protocol-Version, X-Neo-Schema-Version, Last-Event-ID",
        )
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
        )

    def _send_json(
        self,
        value: Any,
        *,
        status: int = 200,
        include_protocol: bool = True,
    ) -> None:
        redacted = redact_secrets(value)
        if include_protocol and isinstance(redacted, dict):
            redacted = dict(redacted)
            redacted.setdefault("protocol_version", PROTOCOL_VERSION)
            redacted.setdefault("schema_version", SCHEMA_VERSION)
        data = json.dumps(redacted, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Neo-Protocol-Version", str(PROTOCOL_VERSION))
        self.send_header("X-Neo-Schema-Version", str(SCHEMA_VERSION))
        self._cors()
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _send_problem(self, problem: _HTTPProblem) -> None:
        payload = {
            "error": {
                "code": problem.code,
                "message": problem.message,
                "status": problem.status,
                "details": redact_secrets(problem.details),
            },
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
        }
        self._send_json(payload, status=problem.status)


def _safe_route_id(value: str) -> str:
    """Validate one decoded route identifier before using it in a path lookup."""
    text = str(value or "")
    if (
        not text
        or text in {".", ".."}
        or any(char in text for char in '/\\:*?"<>|')
        or any(ord(char) < 32 for char in text)
    ):
        raise _HTTPProblem(400, "invalid_identifier", "route identifier is unsafe")
    return text


def _is_loopback(host: str) -> bool:
    """Return whether a bind host is a literal loopback or localhost name."""
    value = str(host or "").casefold()
    return value in {"localhost", "127.0.0.1", "::1", "[::1]"} or value.startswith(
        "127."
    )


def _is_websocket(headers: Any) -> bool:
    """Return whether request headers request a WebSocket upgrade."""
    return (
        "websocket" in str(headers.get("Upgrade", "")).casefold()
        and "upgrade" in str(headers.get("Connection", "")).casefold()
    )


def _int_or_none(value: Any) -> int | None:
    """Parse an optional non-negative integer parameter."""
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _HTTPProblem(
            400, "invalid_sequence", "sequence must be an integer"
        ) from exc
    if parsed < 0:
        raise _HTTPProblem(400, "invalid_sequence", "sequence must be non-negative")
    return parsed


def _sequence_from_headers(query: Mapping[str, Any], headers: Any) -> int:
    """Resolve Last-Event-ID or an after query parameter deterministically."""
    last = headers.get("Last-Event-ID")
    if last not in (None, ""):
        return int(_int_or_none(last))
    return int(_int_or_none(query.get("after", query.get("after_sequence", 0))) or 0)


def _ws_send(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Send one unmasked server WebSocket frame."""
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | int(opcode), length))
    elif length < 65536:
        header = bytes((0x80 | int(opcode), 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((0x80 | int(opcode), 127)) + length.to_bytes(8, "big")
    sock.sendall(header + payload)


def _ws_read_frame(buffer: bytes) -> Optional[tuple[int, bytes, bytes]]:
    """Parse one masked client frame, returning opcode, payload, and remaining bytes."""
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    if not first & 0x80 or first & 0x70:
        raise ValueError("fragmented websocket frames are not supported")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            return None
        length = int.from_bytes(buffer[offset : offset + 2], "big")
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            return None
        length = int.from_bytes(buffer[offset : offset + 8], "big")
        offset += 8
    if length > _MAX_WEBSOCKET_PAYLOAD:
        raise ValueError("websocket frame exceeds the payload limit")
    if opcode >= 0x8 and length > 125:
        raise ValueError("websocket control frame is too large")
    if not masked:
        raise ValueError("client websocket frames must be masked")
    if len(buffer) < offset + 4:
        return None
    mask = buffer[offset : offset + 4]
    offset += 4
    if len(buffer) < offset + length:
        return None
    encoded = buffer[offset : offset + length]
    payload = bytes(value ^ mask[index % 4] for index, value in enumerate(encoded))
    return opcode, payload, buffer[offset + length :]


def struct_pack_close(code: int, reason: str) -> bytes:
    """Pack a standards-shaped WebSocket close frame payload."""
    return int(code).to_bytes(2, "big") + str(reason).encode("utf-8")[:123]
