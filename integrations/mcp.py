"""Official-SDK MCP stdio, SSE, and Streamable HTTP adapter surfaces."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import io
import json
import math
import os
import re
import shlex
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Sequence,
    Union,
)
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .tools import ToolDescriptor

__all__ = [
    "MCPAdapter",
    "MCPAuthError",
    "MCPAuthenticationError",
    "MCPAuthenticationRequiredError",
    "MCPConfigurationError",
    "MCPConnection",
    "MCPConnectionError",
    "MCPError",
    "MCPFailureError",
    "MCPIntegrationError",
    "MCPOutputError",
    "MCPProtocolError",
    "MCPTimeoutError",
    "MCPToolError",
    "MCPTransportConfig",
    "MCPTransportError",
    "MCPTransportFailure",
    "MCPUnsupportedAuthError",
    "MCPUnsupportedTransportError",
    "ToolDescriptor",
    "normalize_tool_result",
    "parse_argv",
    "parse_command",
    "parse_mcp_command",
    "parse_server_command",
    "safe_child_environment",
]

_MAX_ERROR_CHARS = 4_096
_MAX_OUTPUT_CHARS = 1_000_000
_MAX_COMMAND_CHARS = 4_096
_MAX_NAME_CHARS = 512
_SAFE_CHILD_ENV = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONUNBUFFERED",
        "HARNESS_HOME",
        "HARNESS_LOGS_DIR",
        "HARNESS_DECISIONS_DB",
        "HARNESS_EXEC_SKIP_DOCKER",
        "NEO_CONFIG",
        "NEO_PROJECT_DIR",
        "NEO_GLOBAL_ROOT",
        "NEO_TRACE_DIR",
        "NEO_NOTIFY",
    }
)
_SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
_SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]+|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|password|passwd|secret|credential|token)\s*[=:]\s*[^\s,;]+|sk-[A-Za-z0-9_-]{6,})"
)
_QUOTED_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|credential|token)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+"
)


def _truncate(text: str, limit: int) -> str:
    """Return text whose final length never exceeds the requested bound."""
    if len(text) <= limit:
        return text
    marker = "...[truncated]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _redact_text(value: Any, limit: int = _MAX_ERROR_CHARS) -> str:
    """Return bounded text with common credential forms removed."""
    text = str(value or "")
    text = _SECRET_PATTERN.sub("[REDACTED_SECRET]", text)
    text = _QUOTED_SECRET_PATTERN.sub(r"\1[REDACTED_SECRET]", text)
    return _truncate(text, limit)


def _redact_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a recursively redacted JSON-like value with bounded depth."""
    if depth > 12:
        return "[TRUNCATED]"
    upper_key = str(key).lower()
    if any(
        marker in upper_key
        for marker in (
            "password",
            "secret",
            "token",
            "credential",
            "authorization",
            "api_key",
        )
    ):
        return "[REDACTED_SECRET]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item, key=str(item_key), depth=depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, key=key, depth=depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _redact_text(value) if isinstance(value, str) else value
    return _redact_text(value)


def _safe_json(value: Any, limit: int) -> Any:
    """Normalize a result payload to bounded redacted JSON-compatible data."""
    redacted = _redact_value(value)
    try:
        encoded = json.dumps(
            redacted, ensure_ascii=False, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        encoded = _redact_text(redacted)
    if len(encoded) <= limit:
        return redacted
    return _truncate(encoded, limit)


def _safe_exception(error: BaseException) -> str:
    """Return a bounded exception description without a traceback or cause text."""
    text = _redact_text(str(error))
    name = type(error).__name__
    return f"{name}: {text}" if text else name


def _safe_status(error: BaseException) -> Optional[int]:
    """Extract an HTTP status code from common SDK exception shapes."""
    for attribute in ("status_code", "status", "code"):
        value = getattr(error, attribute, None)
        try:
            if value is not None:
                number = int(value)
                if 100 <= number <= 599:
                    return number
        except (TypeError, ValueError):
            pass
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_unauthorized(error: BaseException) -> bool:
    """Return whether an exception represents an HTTP 401 response."""
    if _safe_status(error) == 401:
        return True
    text = _redact_text(str(error)).lower()
    return "401" in text or "unauthorized" in text or "unauthorised" in text


def _is_authentication_exception(error: BaseException) -> bool:
    """Recognize typed or SDK-shaped authentication exceptions."""
    if isinstance(error, MCPAuthenticationError):
        return True
    if getattr(error, "authorization_url", None) is not None:
        return True
    name = type(error).__name__.lower()
    return (
        "auth" in name
        or "oauth" in name
        or "unauthorized" in name
        or "unauthorised" in name
    )


def _bound_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Clamp a finite duration or size to a safe operational range."""
    try:
        number = float(default if value is None else value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(minimum, min(number, maximum))


def _bound_size(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Clamp an integer output or error bound."""
    try:
        number = int(default if value is None else value)
    except (TypeError, ValueError):
        number = int(default)
    return max(minimum, min(number, maximum))


def _mapping(value: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Copy a mapping while accepting generic mapping implementations."""
    return dict(value) if isinstance(value, Mapping) else {}


def _accepts_parameter(parameters: Mapping[str, inspect.Parameter], name: str) -> bool:
    """Return whether an SDK signature accepts a named or variadic keyword."""
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _field(value: Any, *names: str, default: Any = None) -> Any:
    """Read the first present field from a mapping or SDK object."""
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _server_errlog() -> Any:
    """Return a real-file-descriptor stderr sink for the official SDK."""
    candidate = sys.__stderr__
    if candidate is not None:
        try:
            if int(candidate.fileno()) >= 0:
                return candidate
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            pass
    return subprocess.DEVNULL


def parse_mcp_command(
    command: Union[str, Sequence[str], None], args: Optional[Sequence[str]] = None
) -> tuple[str, ...]:
    """Parse a server command into argv without invoking a shell.

    String commands use Windows-compatible ``shlex`` parsing.  A malformed
    quote, NUL byte, empty command, or non-string argv item fails closed.
    """
    values: list[str]
    if command is None:
        values = []
    elif isinstance(command, str):
        if "\x00" in command:
            raise ValueError("server command contains a NUL byte")
        try:
            values = shlex.split(command, posix=False)
        except ValueError as exc:
            raise ValueError(
                f"invalid server command: {_redact_text(exc, 256)}"
            ) from None
        cleaned: list[str] = []
        for token in values:
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
                token = token[1:-1]
            elif token.startswith(("'", '"')):
                raise ValueError("server command contains an unmatched quote")
            if "\x00" in token:
                raise ValueError("server command contains a NUL byte")
            if token:
                cleaned.append(token)
        values = cleaned
    elif isinstance(command, Sequence):
        values = []
        for item in command:
            if not isinstance(item, str):
                raise TypeError("server command argv items must be strings")
            if "\x00" in item:
                raise ValueError("server command contains a NUL byte")
            if item:
                values.append(item)
    else:
        raise TypeError("server command must be a string or argv sequence")
    if args is not None:
        if isinstance(args, (str, bytes)):
            raise TypeError("server command args must be an argv sequence")
        for item in args:
            if not isinstance(item, str):
                raise TypeError("server command args must be strings")
            if "\x00" in item:
                raise ValueError("server command contains a NUL byte")
            values.append(item)
    if not values:
        raise ValueError("server command must not be empty")
    if len(values) > 256 or sum(len(item) for item in values) > 32_768:
        raise ValueError("server command exceeds the supported bound")
    if len(values[0]) > _MAX_COMMAND_CHARS:
        raise ValueError("server command executable is too long")
    return tuple(values)


def safe_child_environment(
    overrides: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Return a scrubbed inherited child environment or explicit caller values.

    ``None`` uses a narrow non-secret allowlist.  An explicit mapping is
    copied as supplied because callers may intentionally authorize a child.
    """
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise TypeError("child environment must be a mapping")
        result: dict[str, str] = {}
        for key, value in overrides.items():
            name = str(key)
            text = str(value)
            if not name or "\x00" in name or "\x00" in text:
                raise ValueError("child environment contains an invalid entry")
            result[name] = text
        return result
    result = {}
    for key, value in os.environ.items():
        upper = str(key).upper()
        if upper in _SAFE_CHILD_ENV and not any(
            marker in upper for marker in _SECRET_MARKERS
        ):
            result[str(key)] = str(value)
    return result


_child_environment = safe_child_environment
parse_argv = parse_mcp_command
parse_command = parse_mcp_command
parse_server_command = parse_mcp_command
_parse_server_command = parse_mcp_command


class MCPError(Exception):
    """Base class for public MCP integration failures with safe messages."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        transport: str = "",
        status_code: Optional[int] = None,
    ) -> None:
        """Create a redacted public error and retain only safe metadata."""
        self.safe_message = _redact_text(message)
        self.operation = _redact_text(operation, 128)
        self.transport = _redact_text(transport, 64)
        self.status_code = status_code
        super().__init__(self.safe_message)

    def __repr__(self) -> str:
        """Return a safe representation without raw exception details."""
        return f"{type(self).__name__}({self.safe_message!r})"


class MCPIntegrationError(MCPError):
    """Alias base retained for callers using the integration terminology."""


class MCPConfigurationError(MCPIntegrationError, ValueError):
    """Report invalid transport or adapter configuration."""


class MCPTransportError(MCPIntegrationError):
    """Report a bounded transport or session failure without fallback."""


class MCPConnectionError(MCPTransportError):
    """Report a failure while opening or using an MCP connection."""


class MCPTimeoutError(MCPTransportError, TimeoutError):
    """Report an operation that exceeded its configured deadline."""


class MCPProtocolError(MCPTransportError):
    """Report a malformed response from an MCP SDK or injected session."""


class MCPOutputError(MCPTransportError):
    """Report a result that cannot be safely normalized within its bound."""


class MCPToolError(MCPTransportError):
    """Report a tool-level failure when strict exception behavior is requested."""


class MCPUnsupportedTransportError(MCPTransportError):
    """Report that the selected official transport is unavailable."""


class MCPUnsupportedAuthError(MCPTransportError):
    """Report that the installed SDK cannot accept the requested auth handoff."""


class MCPAuthenticationError(MCPTransportError):
    """Report an authentication challenge with an optional safe authorization URL."""

    def __init__(
        self,
        message: str = "MCP authentication is required",
        *,
        authorization_url: Optional[str] = None,
        operation: str = "",
        transport: str = "",
        status_code: Optional[int] = 401,
    ) -> None:
        """Create an authentication error without putting authorization material in its text."""
        self.authorization_url = _safe_authorization_url(authorization_url)
        self.safe_authorization_url = self.authorization_url
        super().__init__(
            message, operation=operation, transport=transport, status_code=status_code
        )

    def to_diagnostic_dict(self) -> dict[str, Any]:
        """Return bounded diagnostics without the operational authorization URL."""
        return {
            "message": self.safe_message,
            "operation": self.operation,
            "transport": self.transport,
            "status_code": self.status_code,
            "has_authorization_url": self.authorization_url is not None,
        }

    def __repr__(self) -> str:
        """Return a safe representation that omits the authorization URL."""
        return f"{type(self).__name__}({self.safe_message!r})"


MCPAuthError = MCPAuthenticationError
MCPAuthenticationRequiredError = MCPAuthenticationError
MCPFailureError = MCPTransportError
MCPTransportFailure = MCPTransportError


def _safe_authorization_url(value: Optional[str]) -> Optional[str]:
    """Validate, bound, and redact secret-shaped authorization query values."""
    if value is None:
        return None
    text = str(value)
    parsed = urlparse(text)
    if len(text) > 16_384 or not parsed.scheme or parsed.fragment:
        raise MCPConfigurationError("authorization URL is invalid")
    query = []
    redacted = False
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if any(
            marker in key.lower()
            for marker in ("token", "secret", "password", "credential", "api_key")
        ):
            item = "[REDACTED_SECRET]"
            redacted = True
        query.append((key, item))
    if not redacted:
        return text
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), "")
    )


def _canonical_transport(value: Any) -> str:
    """Normalize supported MCP transport names without selecting a fallback."""
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"stdio", "std_io"}:
        return "stdio"
    if text == "sse":
        return "sse"
    if text in {"streamable_http", "streamablehttp", "http", "https"}:
        return "streamable_http"
    raise MCPConfigurationError(
        f"unsupported MCP transport: {_redact_text(value, 128)}"
    )


@dataclass(frozen=True, init=False)
class MCPTransportConfig:
    """Immutable transport selection and bounded connection settings."""

    kind: str
    command: tuple[str, ...]
    url: Optional[str]
    cwd: Optional[str]
    env: Optional[Mapping[str, str]]
    headers: Optional[Mapping[str, str]]
    timeout_s: float
    connect_timeout_s: float
    max_output_chars: int
    max_error_chars: int
    terminate_on_close: bool

    def __init__(
        self,
        kind: Optional[str] = None,
        command: Union[str, Sequence[str], None] = None,
        url: Optional[str] = None,
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout_s: float = 30.0,
        connect_timeout_s: Optional[float] = None,
        max_output_chars: int = 65_536,
        max_error_chars: int = _MAX_ERROR_CHARS,
        terminate_on_close: bool = True,
        *,
        transport: Optional[str] = None,
        transport_type: Optional[str] = None,
        endpoint: Optional[str] = None,
        base_url: Optional[str] = None,
        server_url: Optional[str] = None,
        http_url: Optional[str] = None,
        server: Optional[Union[str, Sequence[str]]] = None,
        server_command: Optional[Union[str, Sequence[str]]] = None,
        args: Optional[Sequence[str]] = None,
        timeout: Optional[float] = None,
        operation_timeout_s: Optional[float] = None,
        operation_deadline_s: Optional[float] = None,
        deadline_s: Optional[float] = None,
        max_output: Optional[int] = None,
        max_error: Optional[int] = None,
    ) -> None:
        """Create a validated immutable transport configuration."""
        selected = kind or transport or transport_type
        if selected is None:
            if (
                endpoint
                or server_url
                or http_url
                or (isinstance(url, str) and urlparse(url).scheme in {"http", "https"})
            ):
                selected = "streamable_http"
            elif (
                command is not None or server is not None or server_command is not None
            ):
                selected = "stdio"
            else:
                raise MCPConfigurationError("MCP transport kind is required")
        canonical = _canonical_transport(selected)
        effective_command = (
            command if command is not None else (server or server_command)
        )
        effective_url = url or endpoint or base_url or server_url or http_url
        if canonical == "stdio":
            if effective_url:
                raise MCPConfigurationError("stdio transport does not accept a URL")
            try:
                argv = parse_mcp_command(effective_command, args)
            except (TypeError, ValueError) as exc:
                raise MCPConfigurationError(_redact_text(exc, 512)) from None
        else:
            if effective_command:
                raise MCPConfigurationError(
                    f"{canonical} transport does not accept a command"
                )
            if not isinstance(effective_url, str) or not effective_url.strip():
                raise MCPConfigurationError(f"{canonical} transport requires a URL")
            parsed = urlparse(effective_url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                raise MCPConfigurationError(
                    f"{canonical} transport requires an HTTP(S) URL"
                )
            if parsed.username or parsed.password:
                raise MCPConfigurationError(
                    "transport URLs must not contain credentials"
                )
            if any(
                marker in key.lower()
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
                for marker in ("token", "secret", "password", "credential", "api_key")
            ):
                raise MCPConfigurationError("transport URLs must not contain secrets")
            argv = ()
        if cwd is not None:
            cwd_text = str(cwd)
            if "\x00" in cwd_text:
                raise MCPConfigurationError("cwd contains a NUL byte")
        else:
            cwd_text = None
        if env is not None and not isinstance(env, Mapping):
            raise MCPConfigurationError("env must be a mapping")
        if headers is not None and not isinstance(headers, Mapping):
            raise MCPConfigurationError("headers must be a mapping")
        env_copy = (
            None
            if env is None
            else {str(key): str(value) for key, value in env.items()}
        )
        header_copy = (
            None
            if headers is None
            else {str(key): str(value) for key, value in headers.items()}
        )
        operation_timeout = timeout if timeout is not None else operation_timeout_s
        if operation_timeout is None:
            operation_timeout = operation_deadline_s
        if operation_timeout is None:
            operation_timeout = deadline_s if deadline_s is not None else timeout_s
        operation_timeout = _bound_number(operation_timeout, 30.0, 0.01, 300.0)
        connect_timeout = _bound_number(
            connect_timeout_s, operation_timeout, 0.01, 300.0
        )
        object.__setattr__(self, "kind", canonical)
        object.__setattr__(self, "command", argv)
        object.__setattr__(self, "url", str(effective_url) if effective_url else None)
        object.__setattr__(self, "cwd", cwd_text)
        object.__setattr__(
            self, "env", None if env_copy is None else MappingProxyType(env_copy)
        )
        object.__setattr__(
            self,
            "headers",
            None if header_copy is None else MappingProxyType(header_copy),
        )
        object.__setattr__(self, "timeout_s", operation_timeout)
        object.__setattr__(self, "connect_timeout_s", connect_timeout)
        object.__setattr__(
            self,
            "max_output_chars",
            _bound_size(
                max_output if max_output is not None else max_output_chars,
                65_536,
                1,
                _MAX_OUTPUT_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "max_error_chars",
            _bound_size(
                max_error if max_error is not None else max_error_chars,
                _MAX_ERROR_CHARS,
                128,
                _MAX_ERROR_CHARS,
            ),
        )
        object.__setattr__(self, "terminate_on_close", bool(terminate_on_close))

    @property
    def transport(self) -> str:
        """Return the canonical transport kind."""
        return self.kind

    @property
    def transport_type(self) -> str:
        """Return an alias for the canonical transport kind."""
        return self.kind

    @property
    def server_command(self) -> tuple[str, ...]:
        """Return an alias for the safe stdio argv tuple."""
        return self.command

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the safe stdio argv tuple."""
        return self.command

    @property
    def endpoint(self) -> Optional[str]:
        """Return the HTTP endpoint, if selected."""
        return self.url

    @property
    def operation_timeout_s(self) -> float:
        """Return the effective per-operation deadline."""
        return self.timeout_s

    @property
    def child_environment(self) -> dict[str, str]:
        """Return the environment that would be passed to a stdio child."""
        return safe_child_environment(self.env)

    def to_dict(self) -> dict[str, Any]:
        """Return a safe, JSON-compatible configuration projection."""
        headers = {}
        for key, value in (self.headers or {}).items():
            headers[key] = (
                "[REDACTED_SECRET]"
                if any(marker in key.upper() for marker in _SECRET_MARKERS)
                else _redact_text(value, 512)
            )
        return {
            "transport": self.kind,
            "command": [_redact_text(item, 4_096) for item in self.command],
            "url": _redact_text(self.url, 4_096) if self.url else None,
            "cwd": self.cwd,
            "headers": headers,
            "timeout_s": self.timeout_s,
            "connect_timeout_s": self.connect_timeout_s,
            "max_output_chars": self.max_output_chars,
            "max_error_chars": self.max_error_chars,
            "terminate_on_close": self.terminate_on_close,
            "explicit_env": self.env is not None,
        }

    def __repr__(self) -> str:
        """Return a safe representation without environment or header values."""
        return f"MCPTransportConfig(transport={self.kind!r}, url={_redact_text(self.url, 512) if self.url else None!r}, timeout_s={self.timeout_s!r})"


class MCPConnection:
    """Small connection facade shared by :class:`MCPAdapter` consumers."""

    def __init__(
        self,
        config: Union[MCPTransportConfig, Mapping[str, Any]],
        session: Any = None,
        transport: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Create a connection facade around an optional injected session."""
        if isinstance(config, Mapping):
            config = MCPTransportConfig(**dict(config))
        if not isinstance(config, MCPTransportConfig):
            raise MCPConfigurationError("config must be MCPTransportConfig or mapping")
        self.config = config
        self.session = session
        self.transport = transport
        self.metadata = MappingProxyType(dict(metadata or {}))
        self._closed = session is None

    @property
    def connected(self) -> bool:
        """Return whether this facade owns an active session."""
        return self.session is not None and not self._closed

    @property
    def closed(self) -> bool:
        """Return whether this facade has been closed."""
        return self._closed

    async def close(self) -> None:
        """Close injected session and transport resources in reverse order."""
        if self._closed:
            return
        session = self.session
        transport = self.transport
        self._closed = True
        self.session = None
        self.transport = None
        resources = (session,) if session is transport else (session, transport)
        for resource in resources:
            if resource is None:
                continue
            if hasattr(resource, "__aexit__"):
                result = resource.__aexit__(None, None, None)
            else:
                closer = getattr(resource, "aclose", None) or getattr(
                    resource, "close", None
                )
                result = closer() if closer is not None else None
            if inspect.isawaitable(result):
                await result

    async def __aenter__(self) -> "MCPConnection":
        """Enter a pre-connected facade."""
        if not self.connected:
            raise MCPConnectionError(
                "MCP connection facade is not connected", transport=self.config.kind
            )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the facade and preserve any body exception."""
        await self.close()
        return False

    async def list_tools(self) -> list[ToolDescriptor]:
        """List normalized tools from an already connected session."""
        if not self.connected or self.session is None:
            raise MCPConnectionError(
                "MCP connection is not connected", transport=self.config.kind
            )
        response = await self.session.list_tools()
        raw_tools = _field(response, "tools", default=None)
        if (
            raw_tools is None
            and isinstance(response, Sequence)
            and not isinstance(response, (str, bytes, Mapping))
        ):
            raw_tools = response
        if (
            raw_tools is None
            or not isinstance(raw_tools, Sequence)
            or isinstance(raw_tools, (str, bytes, Mapping))
        ):
            raise MCPProtocolError(
                "MCP list_tools response has no tool list",
                operation="list_tools",
                transport=self.config.kind,
            )
        return [
            normalize_tool_descriptor(item, server=self.config.kind)
            for item in raw_tools
        ]

    async def call_tool(
        self, tool: str, arguments: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        """Call a tool through an already connected session."""
        if not self.connected or self.session is None:
            raise MCPConnectionError(
                "MCP connection is not connected",
                operation="call_tool",
                transport=self.config.kind,
            )
        if not str(tool or "").strip():
            raise MCPToolError(
                "MCP tool name must not be empty",
                operation="call_tool",
                transport=self.config.kind,
            )
        values = dict(arguments or {})
        response = await self.session.call_tool(str(tool).strip(), values)
        return normalize_tool_result(response, tool=str(tool).strip())


@asynccontextmanager
async def _stdio_connector(
    config: MCPTransportConfig,
    auth: Any = None,
    headers: Optional[Mapping[str, str]] = None,
) -> AsyncIterator[Any]:
    """Open the official MCP stdio transport with an explicit safe environment."""
    if auth is not None or headers:
        raise MCPUnsupportedAuthError(
            "OAuth authentication is only supported by HTTP transports",
            transport="stdio",
        )
    try:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception as exc:
        raise MCPUnsupportedTransportError(
            f"official MCP stdio transport is unavailable: {_safe_exception(exc)}",
            transport="stdio",
        ) from None
    try:
        parameters = StdioServerParameters(
            command=config.command[0],
            args=list(config.command[1:]),
            env=config.child_environment,
            cwd=config.cwd or str(Path.cwd()),
        )
        async with stdio_client(parameters, errlog=_server_errlog()) as streams:
            yield streams
    except (MCPError, asyncio.CancelledError):
        raise
    except Exception as exc:
        raise MCPTransportError(
            f"MCP stdio transport failed: {_safe_exception(exc)}",
            operation="connect",
            transport="stdio",
        ) from None


@asynccontextmanager
async def _sse_connector(
    config: MCPTransportConfig,
    auth: Any = None,
    headers: Optional[Mapping[str, str]] = None,
) -> AsyncIterator[Any]:
    """Open the official MCP SSE transport when the installed SDK provides it."""
    try:
        from mcp.client.sse import sse_client
    except Exception as exc:
        raise MCPUnsupportedTransportError(
            f"official MCP SSE transport is unavailable: {_safe_exception(exc)}",
            transport="sse",
        ) from None
    kwargs: dict[str, Any] = {"url": config.url}
    try:
        signature = inspect.signature(sse_client)
        parameters = signature.parameters
    except (TypeError, ValueError):
        parameters = {}
    merged_headers = dict(config.headers or {})
    merged_headers.update(dict(headers or {}))
    if merged_headers:
        if not _accepts_parameter(parameters, "headers"):
            raise MCPUnsupportedAuthError(
                "installed MCP SSE transport does not accept custom headers",
                transport="sse",
            )
        kwargs["headers"] = merged_headers
    if auth is not None:
        if not _accepts_parameter(parameters, "auth"):
            raise MCPUnsupportedAuthError(
                "installed MCP SSE transport requires a different auth callback; provide an Authorization header or SDK auth object",
                transport="sse",
            )
        kwargs["auth"] = auth
    if _accepts_parameter(parameters, "timeout"):
        kwargs["timeout"] = config.timeout_s
    if _accepts_parameter(parameters, "sse_read_timeout"):
        kwargs["sse_read_timeout"] = config.timeout_s
    try:
        async with sse_client(**kwargs) as streams:
            yield streams
    except (MCPError, asyncio.CancelledError):
        raise
    except Exception as exc:
        raise MCPTransportError(
            f"MCP SSE transport failed: {_safe_exception(exc)}",
            operation="connect",
            transport="sse",
        ) from None


@asynccontextmanager
async def _streamable_http_connector(
    config: MCPTransportConfig,
    auth: Any = None,
    headers: Optional[Mapping[str, str]] = None,
) -> AsyncIterator[Any]:
    """Open the official MCP Streamable HTTP transport for the installed SDK."""
    try:
        module = importlib.import_module("mcp.client.streamable_http")
    except Exception as exc:
        raise MCPUnsupportedTransportError(
            f"official MCP Streamable HTTP transport is unavailable: {_safe_exception(exc)}",
            transport="streamable_http",
        ) from None
    factory = getattr(module, "streamable_http_client", None)
    if factory is None:
        factory = getattr(module, "streamablehttp_client", None)
    if factory is None:
        raise MCPUnsupportedTransportError(
            "installed MCP SDK has no Streamable HTTP client transport",
            transport="streamable_http",
        )
    try:
        signature = inspect.signature(factory)
        parameters = signature.parameters
    except (TypeError, ValueError):
        parameters = {}
    http_client: Any = None
    owns_client = False
    kwargs: dict[str, Any] = {"url": config.url}
    merged_headers = dict(config.headers or {})
    merged_headers.update(dict(headers or {}))
    if merged_headers or auth is not None:
        if _accepts_parameter(parameters, "http_client"):
            try:
                from mcp.shared._httpx_utils import create_mcp_http_client
            except Exception as exc:
                raise MCPUnsupportedAuthError(
                    f"installed MCP SDK HTTP client factory is unavailable: {_safe_exception(exc)}",
                    transport="streamable_http",
                ) from None
            try:
                http_client = create_mcp_http_client(
                    headers=merged_headers,
                    auth=None if isinstance(auth, Mapping) else auth,
                )
                owns_client = True
            except Exception as exc:
                raise MCPUnsupportedAuthError(
                    f"could not configure official MCP HTTP client: {_safe_exception(exc)}",
                    transport="streamable_http",
                ) from None
            kwargs["http_client"] = http_client
        else:
            if _accepts_parameter(parameters, "headers"):
                kwargs["headers"] = merged_headers
            if auth is not None and not _accepts_parameter(parameters, "auth"):
                raise MCPUnsupportedAuthError(
                    "installed MCP Streamable HTTP transport requires a preconfigured HTTP client for auth",
                    transport="streamable_http",
                )
            if auth is not None:
                kwargs["auth"] = auth
    if _accepts_parameter(parameters, "terminate_on_close"):
        kwargs["terminate_on_close"] = config.terminate_on_close
    try:
        if http_client is not None and owns_client:
            async with http_client:
                async with factory(**kwargs) as streams:
                    yield streams
        else:
            async with factory(**kwargs) as streams:
                yield streams
    except (MCPError, asyncio.CancelledError):
        raise
    except Exception as exc:
        raise MCPTransportError(
            f"MCP Streamable HTTP transport failed: {_safe_exception(exc)}",
            operation="connect",
            transport="streamable_http",
        ) from None
    finally:
        if http_client is not None and not owns_client:
            closer = getattr(http_client, "aclose", None)
            if closer is not None:
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    raise MCPTransportError(
                        f"MCP HTTP client cleanup failed: {_safe_exception(exc)}",
                        operation="close",
                        transport="streamable_http",
                    ) from None


def _default_connector(
    config: MCPTransportConfig,
    auth: Any = None,
    headers: Optional[Mapping[str, str]] = None,
) -> Any:
    """Select exactly one official transport implementation."""
    if config.kind == "stdio":
        return _stdio_connector(config, auth, headers)
    if config.kind == "sse":
        return _sse_connector(config, auth, headers)
    return _streamable_http_connector(config, auth, headers)


def normalize_tool_descriptor(
    value: Any, *, server: str = "", max_description_chars: int = 2_000
) -> ToolDescriptor:
    """Normalize an SDK or mapping tool definition into a stable descriptor."""
    name = _field(value, "name", default="")
    if not str(name).strip():
        raise MCPProtocolError(
            "MCP tool definition has no name", operation="list_tools"
        )
    description = _field(value, "description", default="") or ""
    schema = _field(value, "inputSchema", "input_schema", "schema", default={})
    if schema is None:
        schema = {}
    if not isinstance(schema, Mapping):
        raise MCPProtocolError(
            f"MCP tool {_redact_text(name, _MAX_NAME_CHARS)!r} has a non-mapping input schema",
            operation="list_tools",
        )
    tags_value = _field(value, "tags", default=None)
    metadata_value = _field(value, "metadata", "_meta", default=None)
    if tags_value is None and isinstance(metadata_value, Mapping):
        tags_value = metadata_value.get("tags")
    try:
        return ToolDescriptor(
            str(name).strip(),
            str(description)[: max(0, int(max_description_chars))],
            schema,
            tags_value or (),
            False,
            server,
            metadata_value if isinstance(metadata_value, Mapping) else None,
        )
    except (TypeError, ValueError) as exc:
        raise MCPProtocolError(
            f"invalid MCP tool descriptor: {_safe_exception(exc)}",
            operation="list_tools",
        ) from None


def _content_blocks(response: Any) -> list[Any]:
    """Extract content blocks from an SDK or mapping tool response."""
    content = _field(response, "content", default=None)
    if content is None:
        return []
    if isinstance(content, (str, bytes)):
        return [
            {
                "type": "text",
                "text": content.decode("utf-8", "replace")
                if isinstance(content, bytes)
                else content,
            }
        ]
    if not isinstance(content, Sequence):
        return []
    return list(content)


def _block_dict(block: Any, limit: int = 8_192) -> dict[str, Any]:
    """Normalize one MCP content block without retaining binary payloads."""
    if isinstance(block, Mapping):
        value = dict(block)
    else:
        value = {}
        for name in (
            "type",
            "text",
            "mimeType",
            "mime_type",
            "uri",
            "name",
            "data",
            "annotations",
        ):
            if hasattr(block, name):
                value[name] = getattr(block, name)
    block_type = str(value.get("type", "text"))
    if block_type in {"image", "audio"}:
        value.pop("data", None)
        value["data"] = "[OMITTED_BINARY]"
    if "text" in value:
        value["text"] = _redact_text(value.get("text"), limit)
    if "annotations" in value:
        value["annotations"] = _safe_json(value["annotations"], 4_096)
    return _safe_json(value, limit)


def normalize_tool_result(
    response: Any,
    *,
    max_output_chars: int = 65_536,
    max_error_chars: int = _MAX_ERROR_CHARS,
    tool: str = "",
) -> dict[str, Any]:
    """Normalize an SDK or mapping tool result into a bounded stable dictionary."""
    output_limit = _bound_size(max_output_chars, 65_536, 1, _MAX_OUTPUT_CHARS)
    error_limit = _bound_size(max_error_chars, _MAX_ERROR_CHARS, 128, _MAX_ERROR_CHARS)
    error_value = _field(response, "error", default=None)
    message_value = _field(response, "message", default=None)
    is_error = bool(_field(response, "is_error", "isError", default=False))
    if is_error and not error_value:
        error_value = message_value
    is_error = is_error or error_value is not None
    blocks = _content_blocks(response)
    block_limit = min(128, max(0, output_limit // 64))
    block_budget = min(8_192, max(1, output_limit))
    normalized_blocks = [
        _block_dict(block, block_budget) for block in blocks[:block_limit]
    ]
    text_parts: list[str] = []
    for block in normalized_blocks:
        value = block.get("text") if isinstance(block, Mapping) else None
        if value is not None:
            text_parts.append(str(value))
    text = "\n".join(text_parts)
    text = _truncate(text, output_limit)
    structured = _field(
        response, "structuredContent", "structured_content", "structured", default=None
    )
    normalized_structured = (
        _safe_json(structured, output_limit) if structured is not None else None
    )
    if is_error and not error_value:
        error_value = text or "MCP tool reported an error"
    error = _redact_text(error_value, error_limit) if error_value else None
    result: dict[str, Any] = {
        "ok": not is_error,
        "is_error": is_error,
        "isError": is_error,
        "text": text,
        "content": normalized_blocks,
        "structured_content": normalized_structured,
        "structuredContent": normalized_structured,
        "error": error,
        "tool": _redact_text(tool, _MAX_NAME_CHARS),
    }
    if normalized_structured is not None:
        try:
            result["result"] = normalized_structured
        except Exception:
            pass
    return result


async def _call_flexible(
    factory: Callable[..., Any],
    candidates: Sequence[tuple[Any, ...]],
    kwargs: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Call an injected async/sync factory with a compatible signature."""
    keyword_arguments = dict(kwargs or {})
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        result = (
            factory(*candidates[0], **keyword_arguments)
            if candidates
            else factory(**keyword_arguments)
        )
        return await result if inspect.isawaitable(result) else result
    attempts: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for arguments in candidates:
        attempts.append((arguments, keyword_arguments))
        if keyword_arguments:
            attempts.append((arguments, {}))
    if not attempts:
        attempts.append(((), keyword_arguments))
    for arguments, selected_kwargs in attempts:
        try:
            signature.bind(*arguments, **selected_kwargs)
        except TypeError:
            continue
        result = factory(*arguments, **selected_kwargs)
        return await result if inspect.isawaitable(result) else result
    arguments = candidates[0] if candidates else ()
    result = factory(*arguments, **keyword_arguments)
    return await result if inspect.isawaitable(result) else result


class MCPAdapter(MCPConnection):
    """Async MCP adapter with deterministic official-SDK transport lifecycle."""

    def __init__(
        self,
        config: Optional[Union[MCPTransportConfig, Mapping[str, Any]]] = None,
        *,
        transport: Optional[str] = None,
        kind: Optional[str] = None,
        command: Union[str, Sequence[str], None] = None,
        url: Optional[str] = None,
        endpoint: Optional[str] = None,
        base_url: Optional[str] = None,
        server_url: Optional[str] = None,
        http_url: Optional[str] = None,
        server: Optional[Union[str, Sequence[str]]] = None,
        server_command: Optional[Union[str, Sequence[str]]] = None,
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout_s: float = 30.0,
        timeout: Optional[float] = None,
        operation_timeout_s: Optional[float] = None,
        deadline_s: Optional[float] = None,
        connect_timeout_s: Optional[float] = None,
        max_output_chars: int = 65_536,
        max_error_chars: int = _MAX_ERROR_CHARS,
        terminate_on_close: bool = True,
        connector_factory: Optional[Callable[..., Any]] = None,
        session_factory: Optional[Callable[..., Any]] = None,
        transport_factory: Optional[Callable[..., Any]] = None,
        connector: Optional[Callable[..., Any]] = None,
        session: Optional[Callable[..., Any]] = None,
        auth: Any = None,
        oauth_provider: Any = None,
        token_provider: Any = None,
        auth_provider: Any = None,
        raise_tool_errors: bool = False,
        raise_on_cleanup_error: bool = False,
    ) -> None:
        """Create an adapter without importing the MCP SDK or opening a connection."""
        if config is None:
            config = MCPTransportConfig(
                kind=kind or transport,
                command=command if command is not None else (server or server_command),
                url=url or endpoint or base_url or server_url or http_url,
                cwd=cwd,
                env=env,
                headers=headers,
                timeout_s=timeout if timeout is not None else timeout_s,
                operation_timeout_s=operation_timeout_s,
                deadline_s=deadline_s,
                connect_timeout_s=connect_timeout_s,
                max_output_chars=max_output_chars,
                max_error_chars=max_error_chars,
                terminate_on_close=terminate_on_close,
            )
        elif any(
            value is not None
            for value in (
                transport,
                kind,
                command,
                url,
                endpoint,
                base_url,
                server_url,
                http_url,
                server,
                server_command,
                cwd,
                env,
                headers,
            )
        ):
            raise MCPConfigurationError(
                "pass either config or direct transport options, not both"
            )
        if isinstance(config, Mapping):
            config = MCPTransportConfig(**dict(config))
        if not isinstance(config, MCPTransportConfig):
            raise MCPConfigurationError("config must be MCPTransportConfig or mapping")
        super().__init__(config)
        self.connector_factory = connector_factory or transport_factory or connector
        self.session_factory = session_factory or session
        self.auth = auth
        self.oauth_provider = oauth_provider or token_provider or auth_provider
        self.token_provider = self.oauth_provider
        self.raise_tool_errors = bool(raise_tool_errors)
        self.raise_on_cleanup_error = bool(raise_on_cleanup_error)
        self.cleanup_errors: list[str] = []
        self.last_authorization_url: Optional[str] = None
        self._connector_resource: Any = None
        self._connector_entered = False
        self._session_resource: Any = None
        self._session_entered = False
        self._streams: Any = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._connect_attempted = False
        self.session_id = f"mcp-{id(self):x}"

    @property
    def connection(self) -> "MCPAdapter":
        """Return this adapter as its connection facade."""
        return self

    @property
    def transport_kind(self) -> str:
        """Return the selected transport kind without exposing SDK internals."""
        return self.config.kind

    @property
    def is_connected(self) -> bool:
        """Return whether the underlying MCP session is ready."""
        return bool(self.connected and self.session is not None and not self._closed)

    @property
    def last_error(self) -> Optional[str]:
        """Return the most recent safe cleanup or operation error."""
        return self.cleanup_errors[-1] if self.cleanup_errors else None

    @property
    def authorization_url(self) -> Optional[str]:
        """Return the latest operational OAuth URL, if authorization was initiated."""
        return self.last_authorization_url

    async def _run_deadline(
        self,
        operation: str,
        factory: Callable[[], Any],
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Await one operation under a finite deadline and type all failures."""
        effective_timeout = timeout_s
        if effective_timeout is None:
            effective_timeout = (
                self.config.connect_timeout_s
                if operation in {"connect", "session", "initialize", "auth", "oauth"}
                else self.config.timeout_s
            )

        async def invoke() -> Any:
            result = factory()
            return await result if inspect.isawaitable(result) else result

        try:
            return await asyncio.wait_for(invoke(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            raise MCPTimeoutError(
                f"MCP {self.config.kind} operation timed out",
                operation=operation,
                transport=self.config.kind,
            ) from None
        except asyncio.CancelledError:
            raise
        except MCPError:
            raise
        except Exception as exc:
            status = _safe_status(exc)
            if (
                _is_authentication_exception(exc)
                or status == 401
                or _is_unauthorized(exc)
            ):
                raise MCPAuthenticationError(
                    "MCP authentication is required",
                    operation=operation,
                    transport=self.config.kind,
                ) from None
            raise MCPTransportError(
                f"MCP {self.config.kind} {operation} failed: {_safe_exception(exc)}",
                operation=operation,
                transport=self.config.kind,
                status_code=status,
            ) from None

    async def _auth_material(self) -> tuple[dict[str, str], Any]:
        """Resolve token-provider headers without exposing token values."""
        if self.config.kind == "stdio" and (
            self.auth is not None or self.oauth_provider is not None
        ):
            raise MCPUnsupportedAuthError(
                "OAuth authentication is only supported by HTTP transports",
                transport="stdio",
            )
        headers = dict(self.config.headers or {})
        provider = self.oauth_provider
        if provider is None:
            if isinstance(self.auth, Mapping):
                headers.update(
                    {str(key): str(value) for key, value in self.auth.items()}
                )
                return headers, None
            return headers, self.auth
        getter = getattr(provider, "get_access_token", None) or getattr(
            provider, "get_token", None
        )
        if getter is None and callable(provider):
            getter = provider
        if getter is not None:
            value = await self._run_deadline(
                "auth", lambda: _call_flexible(getter, ((),))
            )
            if isinstance(value, Mapping):
                value = value.get("access_token", value.get("accessToken"))
            elif hasattr(value, "access_token"):
                value = value.access_token
            if value:
                headers["Authorization"] = f"Bearer {value!s}"
                return headers, None
        if isinstance(provider, Mapping):
            value = provider.get("access_token", provider.get("accessToken"))
            if value:
                headers["Authorization"] = f"Bearer {value!s}"
                return headers, None
        provider_token = getattr(provider, "access_token", None)
        if provider_token:
            headers["Authorization"] = f"Bearer {provider_token!s}"
            return headers, None
        if hasattr(provider, "auth_flow") or hasattr(provider, "sync_auth_flow"):
            return headers, provider
        return headers, self.auth

    async def _begin_oauth(self) -> Optional[str]:
        """Initiate OAuth through an injected provider and return its safe URL."""
        provider = self.oauth_provider
        if provider is None:
            return None
        method = (
            getattr(provider, "begin_authorization", None)
            or getattr(provider, "get_authorization_url", None)
            or getattr(provider, "authorize", None)
            or getattr(provider, "initiate", None)
        )
        if method is None:
            authorization_url = getattr(provider, "authorization_url", None)
            if authorization_url:
                return _safe_authorization_url(str(authorization_url))
            if hasattr(provider, "auth_flow") or hasattr(provider, "sync_auth_flow"):
                raise MCPUnsupportedAuthError(
                    "installed MCP SDK auth provider owns its callback; configure a token provider with an authorization hook",
                    transport=self.config.kind,
                )
            return None
        try:
            result = await self._run_deadline(
                "oauth",
                lambda: _call_flexible(
                    method, ((self.session_id,), ()), {"session_id": self.session_id}
                ),
            )
        except MCPError:
            raise
        except Exception:
            raise MCPAuthenticationError(
                "OAuth authorization could not be initiated", transport=self.config.kind
            ) from None
        if isinstance(result, str):
            return _safe_authorization_url(result)
        if isinstance(result, Mapping):
            value = result.get("authorization_url", result.get("authorizationUrl"))
            return _safe_authorization_url(str(value)) if value else None
        value = getattr(result, "authorization_url", None)
        return _safe_authorization_url(str(value)) if value else None

    async def _enter_resource(
        self, resource: Any, operation: str
    ) -> tuple[Any, Any, bool]:
        """Enter an async context manager or await a resource factory result."""
        if hasattr(resource, "__aenter__") and hasattr(resource, "__aexit__"):
            entered = await self._run_deadline(operation, lambda: resource.__aenter__())
            return resource, entered, True
        if inspect.isawaitable(resource):
            resource = await self._run_deadline(operation, lambda: resource)
        return None, resource, False

    async def _exit_resource(self, resource: Any, operation: str) -> None:
        """Exit one entered context with a bounded cleanup deadline."""
        if resource is None or not hasattr(resource, "__aexit__"):
            return
        try:
            await self._run_deadline(
                operation, lambda: resource.__aexit__(None, None, None)
            )
        except MCPError as exc:
            self.cleanup_errors.append(exc.safe_message)

    async def _cleanup_resources(self) -> None:
        """Close session then transport in deterministic reverse order."""
        session_resource = self._session_resource if self._session_entered else None
        connector_resource = (
            self._connector_resource if self._connector_entered else None
        )
        self._session_entered = False
        self._connector_entered = False
        self._session_resource = None
        self._connector_resource = None
        session_object = self.session
        transport_object = self.transport
        self.session = None
        self.transport = None
        self._streams = None
        if session_resource is not None:
            await self._exit_resource(session_resource, "close")
        if connector_resource is not None:
            await self._exit_resource(connector_resource, "close")
        if session_object is not None and not hasattr(session_object, "__aexit__"):
            closer = getattr(session_object, "aclose", None)
            if closer is not None:
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await self._run_deadline("close", lambda: result)
                except MCPError as exc:
                    self.cleanup_errors.append(exc.safe_message)
        if connector_resource is None and transport_object is not None:
            closer = getattr(transport_object, "aclose", None) or getattr(
                transport_object, "close", None
            )
            if closer is not None:
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await self._run_deadline("close", lambda: result)
                except MCPError as exc:
                    self.cleanup_errors.append(exc.safe_message)

    async def _connect_once(self) -> None:
        """Open the selected connector, create a session, and initialize it."""
        headers, auth = await self._auth_material()
        connector_factory = self.connector_factory
        if connector_factory is None:
            connector = _default_connector(self.config, auth, headers)
        elif callable(connector_factory):
            connector = await self._run_deadline(
                "connect",
                lambda: _call_flexible(
                    connector_factory,
                    (
                        (self.config, headers, auth),
                        (self.config, headers),
                        (self.config, auth),
                        (self.config,),
                        (),
                    ),
                ),
            )
        else:
            connector = connector_factory
        connector_resource, streams, connector_entered = await self._enter_resource(
            connector, "connect"
        )
        self._connector_resource = connector_resource
        self._connector_entered = connector_entered
        self._streams = streams
        if streams is None:
            raise MCPConnectionError(
                "MCP connector returned no streams", transport=self.config.kind
            )
        if hasattr(streams, "list_tools") and hasattr(streams, "call_tool"):
            session_value = streams
        else:
            if not isinstance(streams, Sequence) or len(streams) < 2:
                raise MCPProtocolError(
                    "MCP connector did not return read and write streams",
                    transport=self.config.kind,
                )
            read_stream, write_stream = streams[0], streams[1]
            if self.session_factory is None:
                try:
                    from mcp import ClientSession
                except Exception as exc:
                    raise MCPUnsupportedTransportError(
                        f"official MCP client session is unavailable: {_safe_exception(exc)}",
                        transport=self.config.kind,
                    ) from None
                session_value = ClientSession(read_stream, write_stream)
            elif callable(self.session_factory):
                session_value = await self._run_deadline(
                    "session",
                    lambda: _call_flexible(
                        self.session_factory,
                        (
                            (read_stream, write_stream, self.config),
                            (read_stream, write_stream),
                            (),
                        ),
                    ),
                )
            else:
                session_value = self.session_factory
        if session_value is streams and connector_entered:
            session_resource = None
            session_object = streams
            session_entered = False
        else:
            (
                session_resource,
                session_object,
                session_entered,
            ) = await self._enter_resource(session_value, "session")
        self._session_resource = session_resource
        self._session_entered = session_entered
        if session_object is None:
            session_object = session_resource
        if (
            session_object is None
            or not hasattr(session_object, "list_tools")
            or not hasattr(session_object, "call_tool")
        ):
            raise MCPProtocolError(
                "MCP session factory did not provide a usable session",
                transport=self.config.kind,
            )
        self.session = session_object
        self.transport = (
            connector_resource if connector_resource is not None else connector
        )
        initialize = getattr(session_object, "initialize", None)
        if initialize is None or not callable(initialize):
            raise MCPProtocolError(
                "MCP session does not expose initialize", transport=self.config.kind
            )
        await self._run_deadline("initialize", lambda: initialize())
        self._closed = False
        self._connect_attempted = True

    async def _connect_attempt(self, *, retry_auth: bool) -> None:
        """Connect once and retry exactly once after an HTTP 401 when authorized."""
        try:
            await self._connect_once()
            return
        except asyncio.CancelledError:
            await self._cleanup_resources()
            raise
        except MCPAuthenticationError:
            if not retry_auth or self.config.kind == "stdio":
                await self._cleanup_resources()
                raise
        except Exception as exc:
            if not retry_auth or not _is_unauthorized(exc):
                await self._cleanup_resources()
                if isinstance(exc, MCPError):
                    raise
                raise MCPTransportError(
                    f"MCP {self.config.kind} connect failed: {_safe_exception(exc)}",
                    operation="connect",
                    transport=self.config.kind,
                ) from None
        await self._cleanup_resources()
        authorization_url = await self._begin_oauth()
        if not authorization_url:
            raise MCPAuthenticationError(
                "MCP authentication is required and no OAuth provider is configured",
                authorization_url=None,
                operation="connect",
                transport=self.config.kind,
            )
        self.last_authorization_url = authorization_url
        try:
            await self._connect_once()
        except asyncio.CancelledError:
            await self._cleanup_resources()
            raise
        except Exception as exc:
            await self._cleanup_resources()
            if _is_unauthorized(exc):
                raise MCPAuthenticationError(
                    "MCP authentication is still required after OAuth retry",
                    authorization_url=authorization_url,
                    operation="connect",
                    transport=self.config.kind,
                ) from None
            if isinstance(exc, MCPError):
                raise
            raise MCPTransportError(
                f"MCP {self.config.kind} connect failed after OAuth retry: {_safe_exception(exc)}",
                operation="connect",
                transport=self.config.kind,
            ) from None

    async def connect(self) -> "MCPAdapter":
        """Connect and initialize the selected transport with bounded deadlines."""
        if self.is_connected:
            return self
        if self._closed:
            raise MCPConnectionError(
                "MCP adapter is closed", transport=self.config.kind
            )
        async with self._lock:
            if self.is_connected:
                return self
            self.cleanup_errors.clear()
            await self._connect_attempt(retry_auth=True)
            return self

    async def __aenter__(self) -> "MCPAdapter":
        """Enter the adapter and connect before yielding it."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close all owned resources while preserving a body exception."""
        await self.close()
        return False

    async def close(self) -> None:
        """Close the session and connector exactly once, recording cleanup errors."""
        if self._closed and not self._session_resource and not self._connector_resource:
            return
        async with self._lock:
            if (
                self._closed
                and not self._session_resource
                and not self._connector_resource
            ):
                return
            await self._cleanup_resources()
            self._closed = True
            if self.cleanup_errors and self.raise_on_cleanup_error:
                raise MCPTransportError(
                    f"MCP cleanup failed: {'; '.join(self.cleanup_errors)}",
                    operation="close",
                    transport=self.config.kind,
                )

    async def _require_session(self, operation: str) -> Any:
        """Return the active session or raise a typed connection error."""
        if not self.is_connected or self.session is None:
            raise MCPConnectionError(
                "MCP adapter is not connected",
                operation=operation,
                transport=self.config.kind,
            )
        return self.session

    async def _run_auth_retry(
        self,
        operation: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one operation and retry once after an OAuth authorization challenge."""
        try:
            return await self._run_deadline(operation, factory)
        except MCPAuthenticationError as first_error:
            authorization_url = getattr(first_error, "authorization_url", None)
            if self.oauth_provider is None:
                await self._cleanup_resources()
                raise
            try:
                initiated_url = await self._begin_oauth()
            except Exception:
                await self._cleanup_resources()
                raise
            authorization_url = initiated_url or authorization_url
            if not authorization_url:
                await self._cleanup_resources()
                raise MCPAuthenticationError(
                    "MCP authentication is required and the OAuth provider returned no authorization URL",
                    operation=operation,
                    transport=self.config.kind,
                ) from None
            await self._cleanup_resources()
            self._closed = False
            try:
                await self._connect_once()
                return await self._run_deadline(operation, factory)
            except MCPAuthenticationError:
                await self._cleanup_resources()
                raise MCPAuthenticationError(
                    "MCP authentication is still required after OAuth retry",
                    authorization_url=authorization_url,
                    operation=operation,
                    transport=self.config.kind,
                ) from None
            except asyncio.CancelledError:
                await self._cleanup_resources()
                raise
            except Exception as exc:
                await self._cleanup_resources()
                if isinstance(exc, MCPError):
                    raise
                raise MCPTransportError(
                    f"MCP {self.config.kind} {operation} failed after OAuth retry: {_safe_exception(exc)}",
                    operation=operation,
                    transport=self.config.kind,
                ) from None

    async def list_tools(self) -> list[ToolDescriptor]:
        """List and normalize tools from the active MCP session."""
        await self._require_session("list_tools")
        response = await self._run_auth_retry(
            "list_tools", lambda: self.session.list_tools()
        )
        raw_tools = _field(response, "tools", default=None)
        if (
            raw_tools is None
            and isinstance(response, Sequence)
            and not isinstance(response, (str, bytes, Mapping))
        ):
            raw_tools = response
        if (
            raw_tools is None
            or not isinstance(raw_tools, Sequence)
            or isinstance(raw_tools, (str, bytes, Mapping))
        ):
            raise MCPProtocolError(
                "MCP list_tools response has no tool list",
                operation="list_tools",
                transport=self.config.kind,
            )
        return [
            normalize_tool_descriptor(item, server=self.config.kind)
            for item in raw_tools
        ]

    async def list(self) -> list[ToolDescriptor]:
        """Alias for :meth:`list_tools`."""
        return await self.list_tools()

    async def call_tool(
        self, tool: str, arguments: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        """Call one tool and return a bounded, redacted stable result dictionary."""
        tool_name = str(tool or "").strip()
        if not tool_name:
            raise MCPToolError(
                "MCP tool name must not be empty",
                operation="call_tool",
                transport=self.config.kind,
            )
        if arguments is not None and not isinstance(arguments, Mapping):
            raise MCPToolError(
                "MCP tool arguments must be a mapping",
                operation="call_tool",
                transport=self.config.kind,
            )
        await self._require_session("call_tool")
        call_arguments = dict(arguments or {})
        response = await self._run_auth_retry(
            "call_tool",
            lambda: self.session.call_tool(tool_name, call_arguments),
        )
        result = normalize_tool_result(
            response,
            max_output_chars=self.config.max_output_chars,
            max_error_chars=self.config.max_error_chars,
            tool=tool_name,
        )
        if self.raise_tool_errors and not result["ok"]:
            raise MCPToolError(
                result.get("error") or "MCP tool reported an error",
                operation="call_tool",
                transport=self.config.kind,
            )
        return result

    async def call(
        self, tool: str, arguments: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        """Alias for :meth:`call_tool`."""
        return await self.call_tool(tool, arguments)

    async def call_tool_or_raise(
        self, tool: str, arguments: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        """Call a tool and raise a typed error when the tool reports failure."""
        old = self.raise_tool_errors
        self.raise_tool_errors = True
        try:
            return await self.call_tool(tool, arguments)
        finally:
            self.raise_tool_errors = old
