"""Standard-library remote transport and reconnecting event client."""

from __future__ import annotations

import ipaddress
import json
import time
from typing import Any, Dict, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from harness.agent_kernel import ReplayProjection

from .catalog import safe_catalog_json, tool_from_wire
from .errors import (
    AuthenticationError,
    ConflictError,
    InvalidRequestError,
    MissingVersionError,
    ProtocolError,
    RemoteError,
    RunNotFoundError,
    SerializationError,
    ToolCatalogError,
    ToolNotFoundError,
    ToolResolutionError,
    TransportError,
    UnsupportedVersionError,
    WorkspaceActiveError,
    WorkspaceNotFoundError,
)
from .errors import HTTPError as SDKHTTPError
from .events import Events, projection_from_dict
from .models import PROTOCOL_VERSION, Event, Result, RunRequest, Tool, new_session_id
from .transport import RunHandle, Transport

__all__ = ["RemoteAgent", "RemoteClient", "RemoteTransport"]


class _NoRedirect(HTTPRedirectHandler):
    """Disable implicit redirects so bearer credentials cannot cross origins."""

    def redirect_request(self, request: Request, *args: Any, **kwargs: Any) -> None:
        """Reject redirects rather than forwarding authentication to another host."""
        raise TransportError("remote redirects are not allowed")


class RemoteTransport(Transport):
    """Speak the public JSON/SSE protocol using only urllib and the standard library."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        api_key: str = "",
        bearer_token: str = "",
        timeout: float = 30.0,
        protocol_version: int = PROTOCOL_VERSION,
        schema_version: int = 1,
        session_id: str = "",
        headers: Mapping[str, str] | None = None,
        hooks: Any = None,
        hook_manager: Any = None,
        tool_catalog: Any = None,
        catalog: Any = None,
    ) -> None:
        """Create a remote client for a loopback or user-supplied HTTP server."""
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
        parsed = urlsplit(str(base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise InvalidRequestError("remote base_url must be an http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise InvalidRequestError("remote base_url must not contain userinfo")
        hostname = str(parsed.hostname or "").casefold()
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname == "localhost"
        if parsed.scheme == "http" and not loopback:
            raise InvalidRequestError(
                "plain HTTP is allowed only for a loopback remote host"
            )
        self.base_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self.token = str(token or bearer_token or api_key or "")
        self.timeout = max(0.1, float(timeout or 30.0))
        self.protocol_version = int(protocol_version)
        self.schema_version = int(schema_version)
        self.extra_headers = {
            str(key): str(value) for key, value in (headers or {}).items()
        }
        self._opener = build_opener(_NoRedirect())
        self.session_id = str(session_id or new_session_id())
        self.hooks = hook_manager if hook_manager is not None else hooks
        if self.hooks is not None:
            raise InvalidRequestError(
                "remote transports do not execute client-local hooks"
            )
        self.hook_manager = self.hooks
        self.tool_catalog = tool_catalog if tool_catalog is not None else catalog
        self.catalog = self.tool_catalog
        self._conversation: Any = None
        self._closed = False

    @property
    def supports_local_hooks(self) -> bool:
        """Return that client-local hooks cannot execute on a remote peer."""
        return False

    @property
    def client(self) -> "RemoteTransport":
        """Return this transport under a client-compatible alias."""
        return self

    @property
    def conversation(self):
        """Return a session facade for transport-oriented callers."""
        if self._conversation is None:
            from .client import Conversation

            self._conversation = Conversation(self, self.session_id)
        return self._conversation

    def stream(
        self,
        value: Any = "",
        *,
        run_id: str = "",
        after_sequence: int = 0,
        **kwargs: Any,
    ) -> Events:
        """Start or select a remote run and return its event stream."""
        if isinstance(value, RunHandle):
            return value.events(after_sequence)
        if run_id:
            return self.events(
                run_id, session_id=self.session_id, after_sequence=after_sequence
            )
        if isinstance(value, str) and value and self.has_run(value):
            return self.events(
                value, session_id=self.session_id, after_sequence=after_sequence
            )
        handle = self.run(value, wait=False, **kwargs)
        if isinstance(handle, RunHandle):
            return handle.events(after_sequence)
        return self.events(
            handle.run_result.run_id,
            session_id=self.session_id,
            after_sequence=after_sequence,
        )

    @property
    def capabilities(self) -> Dict[str, Any]:
        """Return capabilities known before the first server request."""
        return {
            "local": False,
            "events": True,
            "sse": True,
            "websocket": True,
            "openai": True,
            "workspaces": True,
            "tools": True,
            "tool_catalog": self.tool_catalog is not None,
            "hooks": False,
        }

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        suffix = "/" + str(path or "").lstrip("/")
        values = {
            str(key): value for key, value in (query or {}).items() if value is not None
        }
        return f"{self.base_url}{suffix}" + (f"?{urlencode(values)}" if values else "")

    def _resource(self, kind: str, value: Any, suffix: str = "") -> str:
        """Return a URL-escaped resource path for one user-controlled identifier."""
        encoded = quote(str(value or ""), safe="")
        return f"/v1/{kind}/{encoded}" + (f"/{suffix.lstrip('/')}" if suffix else "")

    def _headers(self, *, accept: str = "application/json") -> Dict[str, str]:
        headers = {
            "Accept": accept,
            "X-Neo-Protocol-Version": str(self.protocol_version),
            "X-Neo-Schema-Version": str(self.schema_version),
            "User-Agent": "neo-agent-sdk/1",
        }
        headers.update(self.extra_headers)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        accept: str = "application/json",
        raw: bool = False,
    ) -> Any:
        if self._closed:
            raise TransportError("remote transport is closed")
        body = dict(payload or {})
        if method.upper() != "GET":
            body.setdefault("protocol_version", self.protocol_version)
            body.setdefault("schema_version", self.schema_version)
        values = dict(query or {})
        if method.upper() == "GET":
            values.setdefault("protocol_version", self.protocol_version)
            values.setdefault("schema_version", self.schema_version)
        request_headers = self._headers(accept=accept)
        if method.upper() != "GET":
            request_headers["Content-Type"] = "application/json"
        request = Request(
            self._url(path, values),
            data=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            if method.upper() != "GET"
            else None,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            self._raise_http_error(exc)
        except URLError as exc:
            raise TransportError(f"remote connection failed: {exc.reason}") from exc
        except OSError as exc:
            raise TransportError(f"remote connection failed: {exc}") from exc
        response_version = {
            "protocol_version": response.headers.get("X-Neo-Protocol-Version"),
            "schema_version": response.headers.get("X-Neo-Schema-Version"),
        }
        self._validate_response_version(
            {key: value for key, value in response_version.items() if value is not None}
        )
        if raw:
            if "text/event-stream" in accept.casefold():
                content_type = str(response.headers.get("Content-Type", "")).casefold()
                if "text/event-stream" not in content_type:
                    response.close()
                    raise ProtocolError("remote response is not an SSE stream")
            return response
        try:
            text = response.read().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SerializationError("remote response could not be read") from exc
        finally:
            response.close()
        if not text.strip():
            return {}
        try:
            decoded = json.loads(text)
        except ValueError as exc:
            raise ProtocolError("remote response was not valid JSON") from exc
        if isinstance(decoded, Mapping):
            self._validate_response_version(decoded)
        return decoded

    def _validate_response_version(self, value: Mapping[str, Any]) -> None:
        protocol = value.get("protocol_version")
        schema = value.get("schema_version")
        if protocol is None or schema is None:
            raise MissingVersionError(
                "remote response omitted protocol or schema version"
            )
        if int(protocol) != self.protocol_version:
            raise UnsupportedVersionError(
                f"remote protocol version {protocol} is incompatible",
                supported=(self.protocol_version,),
                requested=(protocol,),
            )
        if int(schema) != self.schema_version:
            raise UnsupportedVersionError(
                f"remote event schema version {schema} is incompatible",
                supported=(self.schema_version,),
                requested=(schema,),
            )

    def _raise_http_error(self, exc: HTTPError) -> None:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        details: Mapping[str, Any] = {}
        message = f"remote server returned HTTP {exc.code}"
        code = "http_error"
        if body:
            try:
                decoded = json.loads(body)
                error = (
                    decoded.get("error", decoded)
                    if isinstance(decoded, Mapping)
                    else {}
                )
                if isinstance(error, Mapping):
                    message = str(error.get("message", message))
                    code = str(error.get("code", code))
                    details = dict(error.get("details", {}))
            except (ValueError, TypeError):
                pass
        if code in {"missing_version", "unsupported_version"}:
            supported = details.get("supported_protocol_versions", ())
            raise UnsupportedVersionError(
                message,
                supported=supported,
                requested=(exc.code,),
            ) from None
        error_type: type[RemoteError]
        if exc.code == 401:
            error_type = AuthenticationError
        elif exc.code == 404 and code in {"workspace_not_found", "workspace_missing"}:
            error_type = WorkspaceNotFoundError
        elif exc.code == 404 and code in {"tool_not_found", "tool_missing"}:
            error_type = ToolNotFoundError
        elif exc.code == 404:
            error_type = RunNotFoundError
        elif code in {"tool_resolution_failed", "tool_schema_resolution_failed"}:
            error_type = ToolResolutionError
        elif code in {"tool_catalog_error", "tool_catalog_unavailable"}:
            error_type = ToolCatalogError
        elif code == "workspace_active":
            error_type = WorkspaceActiveError
        elif exc.code == 409:
            error_type = ConflictError
        else:
            error_type = SDKHTTPError
        raise error_type(
            message,
            status_code=int(exc.code),
            code=code,
            details=details,
        ) from None

    def _validate_event_version(self, value: Mapping[str, Any]) -> None:
        """Reject event envelopes whose protocol or schema cannot be projected."""
        protocol = value.get("protocol_version")
        schema = value.get("schema_version")
        if protocol is None or schema is None:
            raise MissingVersionError("remote event omitted protocol or schema version")
        if int(protocol) != self.protocol_version:
            raise UnsupportedVersionError(
                f"remote event protocol version {protocol} is incompatible",
                supported=(self.protocol_version,),
                requested=(protocol,),
            )
        if schema is not None and int(schema) != self.schema_version:
            raise UnsupportedVersionError(
                f"remote event schema version {schema} is incompatible",
                supported=(self.schema_version,),
                requested=(schema,),
            )

    def _request_value(self, value: Any, **kwargs: Any) -> RunRequest:
        request = RunRequest.from_value(value, **kwargs)
        if not request.request:
            raise InvalidRequestError("agent request text is required")
        if not request.session_id:
            request.session_id = self.session_id
        if request.strategy == "daily" and kwargs.get("query", False):
            request.strategy = "question"
        return request

    def query(self, value: Any, **kwargs: Any) -> Result:
        """Run a synchronous remote question request."""
        request = self._request_value(value, **kwargs)
        request.wait = True
        if request.strategy == "daily":
            request.strategy = "question"
        response = self._request("POST", "/v1/query", payload=request.to_dict())
        return self._result_from_response(response)

    def run(
        self, value: Any, *, wait: bool | None = None, **kwargs: Any
    ) -> Result | RunHandle:
        """Start a remote run and return a Result or handle according to wait."""
        request = self._request_value(value, **kwargs)
        if wait is not None:
            request.wait = bool(wait)
        response = self._request("POST", "/v1/runs", payload=request.to_dict())
        if request.wait or response.get("result") or response.get("done"):
            return self._result_from_response(response)
        return RunHandle(
            self,
            str(response.get("run_id", request.run_id)),
            session_id=str(response.get("session_id", request.session_id)),
            trace_path=str(response.get("trace_path", "")),
        )

    def start_run(self, value: Any, *, wait: bool = False, **kwargs: Any) -> RunHandle:
        """Start a remote asynchronous run and return its handle."""
        request = self._request_value(value, **kwargs)
        request.wait = bool(wait)
        response = self._request("POST", "/v1/runs", payload=request.to_dict())
        if response.get("result") or response.get("done"):
            result = self._result_from_response(response)
            record = _CompletedRemoteRun(
                self, str(response.get("run_id", request.run_id)), result
            )
            return record.handle
        return RunHandle(
            self,
            str(response.get("run_id", request.run_id)),
            session_id=str(response.get("session_id", request.session_id)),
            trace_path=str(response.get("trace_path", "")),
        )

    def _result_from_response(self, response: Any) -> Result:
        if isinstance(response, Result):
            return response
        if not isinstance(response, Mapping):
            raise SerializationError("remote result response is not an object")
        candidate = response.get("result", response.get("run", response))
        if not isinstance(candidate, Mapping):
            raise SerializationError("remote result response has no result object")
        return Result.from_dict(candidate)

    def wait(self, run_id: str, *, timeout: float | None = None) -> Result:
        """Poll a remote run until its canonical result is available."""
        started = time.monotonic()
        while True:
            status = self.status(run_id)
            if status.get("done") or status.get("terminal"):
                response = self._request("GET", self._resource("runs", run_id))
                return self._result_from_response(response)
            if timeout is not None and time.monotonic() - started >= float(timeout):
                raise TimeoutError(f"timed out waiting for remote run: {run_id}")
            time.sleep(0.02)

    def get_result(self, run_id: str) -> Result | None:
        """Return a remote result only when the run is already terminal."""
        try:
            status = self.status(run_id)
        except RunNotFoundError:
            return None
        if not status.get("done") and not status.get("terminal"):
            return None
        response = self._request("GET", self._resource("runs", run_id))
        return self._result_from_response(response)

    def status(self, run_id: str) -> dict[str, Any]:
        """Fetch a public remote run status projection."""
        value = self._request("GET", self._resource("runs", run_id))
        if not isinstance(value, Mapping):
            raise ProtocolError("remote status response is not an object")
        return dict(value.get("run", value))

    def is_terminal(self, run_id: str) -> bool:
        """Return whether a remote run is terminal."""
        try:
            value = self.status(run_id)
        except (RunNotFoundError, RemoteError):
            return False
        return bool(value.get("done") or value.get("terminal"))

    def has_run(self, run_id: str) -> bool:
        """Return whether a remote run exists."""
        try:
            self.status(run_id)
            return True
        except RunNotFoundError:
            return False

    def cancel(self, run_id: str) -> bool:
        """Request cancellation of a remote run."""
        value = self._request(
            "POST", self._resource("runs", run_id, "cancel"), payload={}
        )
        return bool(value.get("cancelled", value.get("accepted", True)))

    def resume(
        self, run_id: str, request: str = "", *, wait: bool = True, **kwargs: Any
    ) -> Result | RunHandle:
        """Resume a remote run through its public checkpoint endpoint."""
        payload = {"request": str(request or ""), "wait": bool(wait), **kwargs}
        response = self._request(
            "POST", self._resource("runs", run_id, "resume"), payload=payload
        )
        if wait or response.get("done") or response.get("result"):
            return self._result_from_response(response)
        return RunHandle(
            self,
            str(response.get("run_id", run_id)),
            session_id=str(response.get("session_id", "")),
            trace_path=str(response.get("trace_path", "")),
        )

    def events(
        self, run_id: str, *, session_id: str = "", after_sequence: int = 0
    ) -> Events:
        """Return a remote event view with sequence-deduplicated polling."""
        return Events(
            self,
            str(run_id),
            session_id=session_id,
            after_sequence=after_sequence,
        )

    def trace_path(self, run_id: str) -> str:
        """Return the server trace path advertised for a run."""
        value = self.status(run_id)
        return str(value.get("trace_path", ""))

    def read_events(
        self, run_id: str, *, session_id: str = "", after_sequence: int = 0
    ) -> list[Event]:
        """Fetch and validate a remote event backlog."""
        value = self._request(
            "GET",
            self._resource("runs", run_id, "events"),
            query={"after": max(0, int(after_sequence)), "format": "json"},
        )
        if not isinstance(value, Mapping):
            raise ProtocolError("remote event response is not an object")
        rows = value.get("events", [])
        if not isinstance(rows, list):
            raise ProtocolError("remote event response has no event list")
        result: list[Event] = []
        expected = max(0, int(after_sequence)) + 1
        for raw in rows:
            if isinstance(raw, Mapping):
                self._validate_event_version(raw)
            event = Event.from_dict(raw)
            if event.run_id != str(run_id):
                raise ProtocolError("remote event run identity mismatch")
            if session_id and event.session_id != str(session_id):
                raise ProtocolError("remote event session identity mismatch")
            if event.sequence != expected:
                raise ProtocolError(
                    f"remote event gap before sequence {event.sequence}; expected {expected}"
                )
            result.append(event)
            expected += 1
        return result

    def replay(self, run_id: str) -> ReplayProjection:
        """Fetch and return a redacted deterministic remote replay projection."""
        value = self._request("GET", self._resource("runs", run_id, "replay"))
        if not isinstance(value, Mapping):
            raise ProtocolError("remote replay response is not an object")
        projection = projection_from_dict(value)
        if projection.run_id != str(run_id):
            raise ProtocolError("remote replay run identity mismatch")
        return projection

    def iter_sse_events(
        self, run_id: str, *, after_sequence: int = 0, timeout: float | None = None
    ) -> Iterator[Event]:
        """Parse one SSE connection and yield validated public events."""
        response = self._request(
            "GET",
            self._resource("runs", run_id, "events"),
            query={"after": max(0, int(after_sequence)), "stream": "1"},
            accept="text/event-stream",
            raw=True,
        )
        yield from _parse_sse(response, expected_run_id=str(run_id))

    def stream_events(
        self, run_id: str, *, after_sequence: int = 0, timeout: float | None = None
    ) -> Iterator[Event]:
        """Yield one reconnectable SSE event stream."""
        return self.iter_sse_events(
            run_id, after_sequence=after_sequence, timeout=timeout
        )

    def reconnecting_sse_events(
        self, run_id: str, *, after_sequence: int = 0, timeout: float | None = None
    ) -> Iterator[Event]:
        """Reconnect SSE from the last delivered sequence after a dropped connection."""
        cursor = max(0, int(after_sequence))
        started = time.monotonic()
        while True:
            try:
                for event in self.iter_sse_events(
                    run_id, after_sequence=cursor, timeout=timeout
                ):
                    if event.sequence <= cursor:
                        continue
                    if event.sequence != cursor + 1:
                        raise ProtocolError(
                            f"remote SSE gap before sequence {event.sequence}; expected {cursor + 1}"
                        )
                    cursor = event.sequence
                    yield event
                if self.is_terminal(run_id):
                    return
            except (TransportError, OSError):
                if timeout is not None and time.monotonic() - started >= float(timeout):
                    return
            if timeout is not None and time.monotonic() - started >= float(timeout):
                return
            time.sleep(0.02)

    def _catalog_rows(self, value: Any) -> list[Tool]:
        """Validate and decode a remote catalog descriptor collection."""
        if isinstance(value, Mapping):
            value = value.get("tools", [])
        if not isinstance(value, list):
            raise ProtocolError("remote tool catalog response is not an array")
        try:
            return [tool_from_wire(item) for item in value]
        except Exception as exc:
            raise SerializationError("remote tool descriptor is invalid") from exc

    def list_tools(self) -> list[Tool]:
        """List remote catalog descriptors without resolving deferred schemas."""
        return self._catalog_rows(self._request("GET", "/v1/tools"))

    def search_tools(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[Tool]:
        """Search remote catalog metadata without resolving deferred schemas."""
        selected_limit = max_results if limit is None else limit
        return self._catalog_rows(
            self._request(
                "GET",
                "/v1/tools/search",
                query={"q": str(query or ""), "limit": max(0, int(selected_limit))},
            )
        )

    def get_tool(self, name: str) -> Tool:
        """Return one exact remote catalog descriptor without schema resolution."""
        value = self._request("GET", f"/v1/tools/{quote(str(name), safe='')}")
        if isinstance(value, Mapping) and "tool" in value:
            value = value["tool"]
        try:
            return tool_from_wire(value)
        except Exception as exc:
            raise SerializationError("remote tool descriptor is invalid") from exc

    def search(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[Tool]:
        """Search remote catalog metadata under the short alias."""
        return self.search_tools(query, max_results, limit=limit)

    def get(self, name: str) -> Tool:
        """Return one exact remote catalog descriptor under the short alias."""
        return self.get_tool(name)

    def resolve_tool_schema(self, name: str) -> Any:
        """Resolve one exact remote tool schema on explicit request."""
        value = self._request("GET", f"/v1/tools/{quote(str(name), safe='')}/schema")
        if not isinstance(value, Mapping):
            raise ProtocolError("remote tool schema response is not an object")
        schema = value.get("schema")
        if schema is None:
            raise SerializationError("remote tool schema response has no schema")
        return safe_catalog_json(schema)

    def resolve_schema(self, name: str) -> Any:
        """Return one exact remote schema under the short compatibility name."""
        return self.resolve_tool_schema(name)

    def resolve_tool(self, name: str) -> Any:
        """Return one exact remote schema under the resolve-tool alias."""
        return self.resolve_tool_schema(name)

    def resolve(self, name: str) -> Any:
        """Return one exact remote schema under the short resolve alias."""
        return self.resolve_tool_schema(name)

    def list_runs(self) -> list[dict[str, Any]]:
        """List remote run summaries."""
        value = self._request("GET", "/v1/runs")
        if isinstance(value, Mapping):
            value = value.get("runs", [])
        if not isinstance(value, list):
            raise ProtocolError("remote run list is not an array")
        return [dict(item) for item in value if isinstance(item, Mapping)]

    def history(
        self, session_id: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Fetch bounded conversation history from the remote server."""
        value = self._request(
            "GET",
            self._resource("conversations", session_id, "history"),
            query={"limit": limit},
        )
        if isinstance(value, Mapping):
            value = value.get("history", value.get("turns", []))
        if not isinstance(value, list):
            raise ProtocolError("remote history response is not an array")
        return [dict(item) for item in value if isinstance(item, Mapping)]

    def close_conversation(self, session_id: str) -> None:
        """Close a remote conversation lifecycle."""
        self._request(
            "POST", self._resource("conversations", session_id, "close"), payload={}
        )

    def create_workspace(
        self,
        name: str = "",
        *,
        workspace_id: str = "",
        source_path: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a server-managed workspace through REST."""
        value = self._request(
            "POST",
            "/v1/workspaces",
            payload={
                "name": name,
                "workspace_id": workspace_id,
                "source_path": source_path,
                "metadata": dict(metadata or {}),
            },
        )
        return dict(value.get("workspace", value))

    def list_workspaces(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        """List server-managed workspaces through REST."""
        value = self._request(
            "GET",
            "/v1/workspaces",
            query={"include_deleted": str(bool(include_deleted)).lower()},
        )
        if isinstance(value, Mapping):
            value = value.get("workspaces", [])
        return [dict(item) for item in value if isinstance(item, Mapping)]

    def get_workspace(
        self, workspace_id: str, *, include_deleted: bool = False
    ) -> dict[str, Any]:
        """Fetch one server-managed workspace through REST."""
        value = self._request(
            "GET",
            self._resource("workspaces", workspace_id),
            query={"include_deleted": str(bool(include_deleted)).lower()},
        )
        return dict(value.get("workspace", value))

    def delete_workspace(self, workspace_id: str) -> dict[str, Any]:
        """Delete one inactive server-managed workspace through REST."""
        value = self._request(
            "DELETE", self._resource("workspaces", workspace_id), payload={}
        )
        return dict(value.get("workspace", value))

    def close(self) -> None:
        """Mark the remote transport closed without logging bearer credentials."""
        self._closed = True

    def negotiate(self) -> dict[str, Any]:
        """Fetch server capabilities and return the version negotiation payload."""
        value = self._request("GET", "/v1/capabilities")
        payload = value.get("negotiation", value) if isinstance(value, Mapping) else {}
        if not isinstance(payload, Mapping):
            raise ProtocolError("remote negotiation response is not an object")
        selected = payload.get("protocol_version", payload.get("version"))
        selected_schema = payload.get("schema_version", payload.get("schema"))
        if selected is not None:
            self.protocol_version = int(selected)
        if selected_schema is not None:
            self.schema_version = int(selected_schema)
        return dict(payload)


class _CompletedRemoteRun:
    """Small internal holder for a synchronously completed remote start."""

    def __init__(self, transport: RemoteTransport, run_id: str, result: Result) -> None:
        """Store a completed result behind the normal handle interface."""
        self.transport = transport
        self.run_id = str(run_id)
        self.result_value = result
        self.handle = RunHandle(
            transport, self.run_id, session_id=result.run_result.session_id
        )

    def wait(self, timeout: float | None = None) -> Result:
        """Return the already completed result."""
        return self.result_value


RemoteClient = RemoteTransport
RemoteAgent = RemoteTransport


def _parse_sse(response: Any, *, expected_run_id: str = "") -> Iterator[Event]:
    """Parse an HTTP response containing standards-shaped SSE data frames."""
    blocks: dict[str, Any] = {"data": []}
    event_id = ""
    event_name = ""
    try:
        for raw_line in response:
            if len(raw_line) > 1024 * 1024:
                raise ProtocolError("remote SSE line exceeds the size limit")
            line = (
                raw_line.decode("utf-8", errors="replace")
                if isinstance(raw_line, bytes)
                else str(raw_line)
            )
            line = line.rstrip("\r\n")
            if not line:
                if blocks["data"]:
                    payload = "\n".join(blocks["data"])
                    if payload.strip() == "[DONE]":
                        blocks = {"data": []}
                        event_id = ""
                        event_name = ""
                        return
                    if not payload.strip():
                        blocks = {"data": []}
                        event_id = ""
                        event_name = ""
                        continue
                    try:
                        decoded = json.loads(payload)
                    except ValueError as exc:
                        raise ProtocolError(
                            "remote SSE data was not valid JSON"
                        ) from exc
                    if not isinstance(decoded, Mapping):
                        raise ProtocolError("remote SSE data is not an object")
                    if event_id:
                        decoded.setdefault("sequence", event_id)
                    if event_name and "event_type" not in decoded:
                        decoded["event_type"] = event_name
                    if (
                        "protocol_version" not in decoded
                        or "schema_version" not in decoded
                    ):
                        raise ProtocolError(
                            "remote SSE event omitted protocol or schema version"
                        )
                    if int(decoded["protocol_version"]) != PROTOCOL_VERSION:
                        raise UnsupportedVersionError(
                            f"remote SSE protocol version {decoded['protocol_version']} is incompatible",
                            supported=(PROTOCOL_VERSION,),
                            requested=(decoded["protocol_version"],),
                        )
                    if (
                        "schema_version" in decoded
                        and int(decoded["schema_version"]) != 1
                    ):
                        raise UnsupportedVersionError(
                            f"remote SSE schema version {decoded['schema_version']} is incompatible",
                            supported=(1,),
                            requested=(decoded["schema_version"],),
                        )
                    event = Event.from_dict(decoded)
                    if expected_run_id and event.run_id != expected_run_id:
                        raise ProtocolError("remote SSE event run identity mismatch")
                    yield event
                blocks = {"data": []}
                event_id = ""
                event_name = ""
                continue
            if line.startswith(":"):
                continue
            if ":" in line:
                field, value = line.split(":", 1)
                value = value[1:] if value.startswith(" ") else value
            else:
                field, value = line, ""
            if field == "id":
                event_id = value.strip()
            elif field == "event":
                event_name = value.strip()
            elif field == "data":
                blocks["data"].append(value)
        if blocks["data"]:
            payload = "\n".join(blocks["data"])
            if payload.strip() not in {"", "[DONE]"}:
                decoded = json.loads(payload)
                if "protocol_version" not in decoded or "schema_version" not in decoded:
                    raise ProtocolError(
                        "remote SSE event omitted protocol or schema version"
                    )
                if int(decoded["protocol_version"]) != PROTOCOL_VERSION:
                    raise UnsupportedVersionError(
                        f"remote SSE protocol version {decoded['protocol_version']} is incompatible",
                        supported=(PROTOCOL_VERSION,),
                        requested=(decoded["protocol_version"],),
                    )
                if "schema_version" in decoded and int(decoded["schema_version"]) != 1:
                    raise UnsupportedVersionError(
                        f"remote SSE schema version {decoded['schema_version']} is incompatible",
                        supported=(1,),
                        requested=(decoded["schema_version"],),
                    )
                event = Event.from_dict(decoded)
                if expected_run_id and event.run_id != expected_run_id:
                    raise ProtocolError("remote SSE event run identity mismatch")
                yield event
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
