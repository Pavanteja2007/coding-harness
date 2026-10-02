"""Typed stdio MCP client lifecycle, health, and compatibility wrappers.

The public :class:`McpClient` owns one stdio transport and one MCP session
for its complete lifetime.  Callers can use it as an async context manager,
reuse the initialized session for multiple list/call operations, and observe
immutable lifecycle events through ``events`` or an optional callback.

The historical :func:`list_mcp_tools` and :func:`call_mcp_tool` functions
remain small synchronous compatibility facades.  They retain their original
plain-dictionary result shapes and convert expected transport, timeout, and
tool failures into ``ok=False`` data.

Only the standard input/output transport is supported.  The module imports
the MCP SDK lazily so importing the memory package does not initialize a
server or bind a capture-dependent SDK default.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import math
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

__all__ = [
    "MCP_CLIENT_PHASES",
    "MCPClient",
    "MCPClientEvent",
    "MCPClientHealth",
    "MCPClientPhase",
    "MCPHealth",
    "MCPHealthMetadata",
    "MCPHealthStatus",
    "McpClient",
    "McpClientEvent",
    "McpClientHealth",
    "McpClientPhase",
    "McpHealth",
    "McpHealthMetadata",
    "McpHealthStatus",
    "McpLifecycleEvent",
    "McpLifecyclePhase",
    "McpPhase",
    "call_mcp_tool",
    "list_mcp_tools",
    "parse_server_command",
]


Command = Union[str, Sequence[str]]
EventCallback = Callable[["McpLifecycleEvent"], Union[Awaitable[Any], Any]]


class McpHealthStatus(str, Enum):
    """Stable health states exposed by :class:`McpClient`."""

    IDLE = "idle"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CLOSED = "closed"

    def __str__(self) -> str:
        """Return the serialized stable health value."""
        return self.value


class McpClientPhase(str, Enum):
    """Stable lifecycle phase names emitted by :class:`McpClient`."""

    SPAWN = "spawn"
    INITIALIZE = "initialize"
    LIST = "list"
    LIST_TOOLS = "list"
    CALL = "call"
    CALL_TOOL = "call"
    SHUTDOWN = "shutdown"
    FAILURE = "failure"

    def __str__(self) -> str:
        """Return the serialized stable phase value."""
        return self.value


McpLifecyclePhase = McpClientPhase
McpPhase = McpClientPhase
MCPClientPhase = McpClientPhase
MCPHealthStatus = McpHealthStatus
MCP_CLIENT_PHASES = (
    "spawn",
    "initialize",
    "list",
    "call",
    "shutdown",
    "failure",
)


@dataclass(frozen=True)
class McpLifecycleEvent(Mapping[str, Any]):
    """One immutable observation of an MCP client lifecycle phase.

    ``phase`` is always one of the six stable values in
    :data:`MCP_CLIENT_PHASES`.  A phase marker is emitted before its
    operation; an unsuccessful operation also emits a ``failure`` event.
    The mapping methods keep the event convenient for small integrations
    that historically consumed dictionaries.
    """

    phase: McpClientPhase
    timestamp: float = field(default_factory=time.time)
    sequence: int = 0
    ok: bool = True
    error: Optional[str] = None
    elapsed_s: float = 0.0
    tool: str = ""
    tool_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize the typed phase and freeze the event metadata."""
        try:
            phase = (
                self.phase
                if isinstance(self.phase, McpClientPhase)
                else McpClientPhase(self.phase)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"unsupported MCP lifecycle phase: {self.phase!r}"
            ) from exc
        metadata = self.metadata if isinstance(self.metadata, Mapping) else {}
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "timestamp", float(self.timestamp))
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(self, "ok", bool(self.ok))
        object.__setattr__(self, "elapsed_s", max(0.0, float(self.elapsed_s)))
        object.__setattr__(self, "tool", str(self.tool or ""))
        object.__setattr__(self, "tool_count", max(0, int(self.tool_count)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(metadata)))

    @property
    def event(self) -> str:
        """Return the stable serialized phase name."""
        return self.phase.value

    @property
    def name(self) -> str:
        """Return an event-name alias for generic event consumers."""
        return self.event

    @property
    def kind(self) -> str:
        """Return an event-kind alias for trace-style consumers."""
        return self.event

    @property
    def event_type(self) -> str:
        """Return an explicit event-type alias."""
        return self.event

    @property
    def phase_name(self) -> str:
        """Return the stable phase name as a string."""
        return self.event

    @property
    def elapsed_ms(self) -> float:
        """Return elapsed phase time in milliseconds."""
        return round(self.elapsed_s * 1000.0, 3)

    @property
    def status(self) -> str:
        """Return ``ok`` or ``error`` for this observation."""
        return "ok" if self.ok else "error"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible, secret-free event projection."""
        phase = self.phase.value
        return {
            "phase": phase,
            "event": phase,
            "kind": phase,
            "timestamp": round(self.timestamp, 6),
            "ts": round(self.timestamp, 6),
            "sequence": self.sequence,
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "elapsed_s": round(self.elapsed_s, 6),
            "elapsed_ms": self.elapsed_ms,
            "tool": self.tool,
            "tool_count": self.tool_count,
            "metadata": dict(self.metadata),
        }

    def as_dict(self) -> Dict[str, Any]:
        """Return the same projection as :meth:`to_dict`."""
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        """Expose event fields through mapping syntax."""
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over stable event field names."""
        return iter(self.to_dict())

    def __len__(self) -> int:
        """Return the number of projected event fields."""
        return len(self.to_dict())


McpClientEvent = McpLifecycleEvent
MCPClientEvent = McpLifecycleEvent


@dataclass(frozen=True)
class McpHealth(Mapping[str, Any]):
    """Immutable health metadata for one :class:`McpClient` instance.

    The snapshot is a mapping and is callable for compatibility with both
    ``client.health`` and ``client.health()`` callers.  The callable forms
    return a plain dictionary; the property form retains typed attributes.
    An async caller may also use ``await client.health()`` or
    ``await client.check_health()``.
    """

    status: str = McpHealthStatus.IDLE.value
    ok: bool = False
    connected: bool = False
    initialized: bool = False
    tool_count: int = 0
    tool_names: Tuple[str, ...] = ()
    event_count: int = 0
    last_phase: str = ""
    last_error: Optional[str] = None
    elapsed_s: float = 0.0
    timeout_s: float = 30.0
    phases: Tuple[str, ...] = ()
    command: str = ""
    cwd: str = ""
    transport: str = "stdio"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize collection and mapping fields without mutating input."""
        names = tuple(str(item) for item in (self.tool_names or ()))
        phases = tuple(str(item) for item in (self.phases or ()))
        metadata = self.metadata if isinstance(self.metadata, Mapping) else {}
        object.__setattr__(
            self, "status", str(self.status or McpHealthStatus.IDLE.value)
        )
        object.__setattr__(self, "ok", bool(self.ok))
        object.__setattr__(self, "connected", bool(self.connected))
        object.__setattr__(self, "initialized", bool(self.initialized))
        object.__setattr__(self, "tool_count", max(0, int(self.tool_count)))
        object.__setattr__(self, "tool_names", names)
        object.__setattr__(self, "event_count", max(0, int(self.event_count)))
        object.__setattr__(self, "last_phase", str(self.last_phase or ""))
        object.__setattr__(
            self,
            "last_error",
            str(self.last_error) if self.last_error else None,
        )
        object.__setattr__(self, "elapsed_s", max(0.0, float(self.elapsed_s)))
        object.__setattr__(self, "timeout_s", max(0.0, float(self.timeout_s)))
        object.__setattr__(self, "phases", phases)
        object.__setattr__(self, "command", str(self.command or ""))
        object.__setattr__(self, "cwd", str(self.cwd or ""))
        object.__setattr__(self, "transport", str(self.transport or "stdio"))
        object.__setattr__(self, "metadata", MappingProxyType(dict(metadata)))

    @property
    def state(self) -> str:
        """Return the serialized health state."""
        return self.status

    @property
    def lifecycle_status(self) -> str:
        """Return the lifecycle-oriented health state."""
        return self.status

    @property
    def phase(self) -> str:
        """Return the last lifecycle phase name."""
        return self.last_phase

    @property
    def closed(self) -> bool:
        """Return whether the client has completed shutdown."""
        return self.status == McpHealthStatus.CLOSED.value

    @property
    def error(self) -> Optional[str]:
        """Return the last safe error, if one exists."""
        return self.last_error or None

    @property
    def tools(self) -> Tuple[str, ...]:
        """Return the currently known tool names."""
        return self.tool_names

    @property
    def healthy(self) -> bool:
        """Return whether the client completed without a current failure."""
        return bool(
            self.ok
            and self.status
            in {
                McpHealthStatus.READY.value,
                McpHealthStatus.DEGRADED.value,
                McpHealthStatus.CLOSED.value,
            }
        )

    @property
    def duration_ms(self) -> float:
        """Return elapsed lifetime in milliseconds."""
        return round(self.elapsed_s * 1000.0, 3)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible health projection."""
        health_status = "healthy" if self.healthy else "unhealthy"
        if self.status == McpHealthStatus.IDLE.value:
            health_status = "unknown"
        elif self.status == McpHealthStatus.DEGRADED.value:
            health_status = "degraded"
        return {
            "ok": self.ok,
            "status": self.status,
            "state": self.status,
            "health_status": health_status,
            "healthy": self.healthy,
            "connected": self.connected,
            "initialized": self.initialized,
            "tool_count": self.tool_count,
            "tools": list(self.tool_names),
            "event_count": self.event_count,
            "last_phase": self.last_phase,
            "phase": self.last_phase,
            "error": self.error,
            "last_error": self.last_error or None,
            "elapsed_s": round(self.elapsed_s, 6),
            "duration_ms": self.duration_ms,
            "timeout_s": self.timeout_s,
            "phases": list(self.phases),
            "command": self.command,
            "server": self.command,
            "cwd": self.cwd,
            "transport": self.transport,
            "metadata": dict(self.metadata),
        }

    def as_dict(self) -> Dict[str, Any]:
        """Return the same projection as :meth:`to_dict`."""
        return self.to_dict()

    def __call__(self) -> Dict[str, Any]:
        """Return a plain dictionary for ``client.health()`` callers."""
        return self.to_dict()

    def __await__(self) -> Iterator[Any]:
        """Allow ``await client.health()`` without starting a probe."""

        async def _return() -> Dict[str, Any]:
            return self.to_dict()

        return _return().__await__()

    def __getitem__(self, key: str) -> Any:
        """Expose health fields through mapping syntax."""
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over stable health field names."""
        return iter(self.to_dict())

    def __len__(self) -> int:
        """Return the number of projected health fields."""
        return len(self.to_dict())


McpHealthMetadata = McpHealth
McpClientHealth = McpHealth
MCPHealth = McpHealth
MCPClientHealth = McpHealth
MCPHealthMetadata = McpHealth


def _server_errlog() -> Any:
    """Return a stderr sink with a real file descriptor when available.

    The MCP SDK binds its default ``errlog`` at import time.  Importing the
    SDK under pytest capture or another fileno-less stream can therefore
    poison every later spawn.  The client always passes this explicit sink,
    and the DEVNULL fallback keeps pythonw-style processes safe as well.
    """
    candidate = sys.__stderr__
    if candidate is not None:
        try:
            if int(candidate.fileno()) >= 0:
                return candidate
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            pass
    return subprocess.DEVNULL


def _run(coro: Awaitable[Any]) -> Any:
    """Run an awaitable on a fresh event loop for synchronous callers."""
    return asyncio.run(coro)


def _run_sync_bounded(coro: Awaitable[Any], timeout_s: float) -> Tuple[bool, Any]:
    """Run a synchronous facade with a deadline and cancel its worker task."""
    result_queue: queue.Queue[Tuple[str, Any]] = queue.Queue(maxsize=1)
    ready = threading.Event()
    state: Dict[str, Any] = {}

    def invoke() -> None:
        loop: Optional[asyncio.AbstractEventLoop] = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = loop.create_task(coro)
            state["loop"] = loop
            state["task"] = task
            ready.set()
            result_queue.put(("value", loop.run_until_complete(task)))
        except BaseException as exc:
            result_queue.put(("error", exc))
        finally:
            ready.set()
            if loop is not None:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except BaseException:
                    pass
                asyncio.set_event_loop(None)
                loop.close()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    deadline = time.monotonic() + max(0.01, float(timeout_s))
    ready.wait(max(0.0, deadline - time.monotonic()))
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        task = state.get("task")
        loop = state.get("loop")
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        worker.join(min(0.5, max(0.05, float(timeout_s))))
        if not state.get("task") and hasattr(coro, "close"):
            coro.close()
        return False, None
    if not state.get("task") and hasattr(coro, "close"):
        coro.close()
    try:
        kind, value = result_queue.get_nowait()
    except queue.Empty:
        return False, None
    if kind == "error":
        return False, value
    return True, value


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
    }
)
_SECRET_ENV_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PYPIC", "CREDENTIAL")
_SAFE_INHERITED_ENV = frozenset(
    {
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


def _child_environment(overrides: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Build a safe inherited environment or honor explicit overrides.

    ``None`` selects the narrow inherited allowlist.  Any explicit mapping,
    including an empty mapping or authorization variables, is copied as the
    caller supplied it; secrets are never silently removed from that mode.
    """
    if overrides is not None:
        return {str(key): str(value) for key, value in overrides.items()}
    result: Dict[str, str] = {}
    for key, value in os.environ.items():
        upper = str(key).upper()
        if upper in _SAFE_CHILD_ENV or upper in _SAFE_INHERITED_ENV:
            if any(marker in upper for marker in _SECRET_ENV_MARKERS):
                continue
            result[str(key)] = str(value)
    return result


def _description_first_line(description: Any) -> str:
    """Return the first non-empty line of a tool description."""
    text = str(description or "").strip()
    return text.splitlines()[0] if text else ""


def _content_text(content: Any) -> str:
    """Extract all text blocks from an MCP tool result."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    texts: List[str] = []
    for block in content:
        if isinstance(block, Mapping):
            value = block.get("text")
        else:
            value = getattr(block, "text", None)
        if value is None:
            continue
        texts.append(value if isinstance(value, str) else str(value))
    return "\n".join(texts)


def parse_server_command(server: str) -> List[str]:
    """Split a shell-style server command into an argv list.

    Non-POSIX ``shlex`` parsing keeps Windows backslashes intact.  One
    matching quote pair is removed from each token because the SDK receives
    an argv list rather than a shell command line.
    """
    if not server:
        return []
    out: List[str] = []
    for tok in shlex.split(server, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
            tok = tok[1:-1]
        out.append(tok)
    return out


def _coerce_argv(server: Optional[Command]) -> Tuple[List[str], Optional[str]]:
    """Normalize a string or argv-like server specification for a client."""
    if server is None:
        return [], "empty server command"
    if isinstance(server, str):
        try:
            argv = parse_server_command(server)
        except ValueError as exc:
            return [], f"invalid server command: {exc}"
        if not argv:
            return [], "empty server command"
        return argv, None
    try:
        argv = [str(part) for part in server]
    except (TypeError, ValueError) as exc:
        return [], f"invalid server command: {exc}"
    if not argv:
        return [], "empty server command"
    return argv, None


def _bounded_timeout(value: Any, default: float = 30.0) -> float:
    """Clamp a caller timeout to a finite, usable, bounded range."""
    try:
        result = float(default if value is None else value)
    except (TypeError, ValueError):
        result = float(default)
    if not math.isfinite(result):
        result = float(default)
    return max(0.01, min(result, 300.0))


def _exception_text(error: BaseException) -> str:
    """Format an exception without losing its stable type name."""
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


async def _await_bounded(awaitable: Awaitable[Any], timeout_s: float) -> Any:
    """Await in the current task with a loop-level cancellation deadline.

    The MCP SDK's AnyIO cancel scopes are task-affine.  ``asyncio.wait_for``
    would move the awaited coroutine into a child task and make the later
    session/transport exit fail, so this helper cancels the current task at
    the deadline instead.
    """
    task = asyncio.current_task()
    if task is None:
        return await awaitable
    timed_out = False

    def cancel() -> None:
        nonlocal timed_out
        if not task.done():
            timed_out = True
            task.cancel()

    handle = asyncio.get_running_loop().call_later(timeout_s, cancel)
    try:
        return await awaitable
    except asyncio.CancelledError:
        if timed_out:
            raise asyncio.TimeoutError from None
        raise
    finally:
        handle.cancel()


def _safe_health_text(value: Any) -> str:
    """Redact common credential shapes from health and command metadata."""
    text = str(value or "")
    text = re.sub(
        r"(?i)(api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|"
        r"authorization|password|passwd|secret|credential|token)"
        r"(\s*[=:]\s*|\s+)(\S+)",
        lambda match: f"{match.group(1)}{match.group(2)}***",
        text,
    )
    return re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,})", "sk-***", text)


def _safe_command(argv: Sequence[str]) -> str:
    """Return a display-safe command representation for health metadata."""
    return _safe_health_text(" ".join(str(part) for part in argv))


def _result_error(res: Any, attribute: str, default: Any = None) -> Any:
    """Read an MCP result field from SDK objects or mapping responses."""
    if isinstance(res, Mapping):
        if attribute == "is_error" and attribute not in res and "isError" in res:
            return res["isError"]
        value = res.get(attribute, default)
    else:
        value = getattr(res, attribute, default)
    if attribute == "is_error" and value is None:
        value = getattr(res, "isError", default)
    return value


def _tools_from_response(response: Any) -> List[Dict[str, str]]:
    """Normalize an SDK or mapping list-tools response into dictionaries."""
    raw_tools = _result_error(response, "tools", None)
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, (list, tuple)):
        raise ValueError("MCP list_tools response did not contain a tool list")
    tools: List[Dict[str, str]] = []
    for tool in raw_tools:
        if isinstance(tool, Mapping):
            name = tool.get("name")
            description = tool.get("description")
        else:
            name = getattr(tool, "name", None)
            description = getattr(tool, "description", None)
        if name is None or not str(name):
            continue
        tools.append(
            {
                "name": str(name),
                "description": _description_first_line(description),
            }
        )
    return tools


class _PhaseFailure(Exception):
    """Internal signal carrying a bounded or expected phase failure."""

    def __init__(self, error: str, status: str = McpHealthStatus.FAILED.value) -> None:
        super().__init__(error)
        self.error = error
        self.status = status


class McpClient:
    """Own one bounded stdio MCP connection and expose typed lifecycle data.

    ``server`` accepts the same command string as the compatibility wrappers
    or an argv-like sequence.  ``cwd`` is passed to the SDK unchanged apart
    from string conversion.  ``env=None`` uses the module's safe inherited
    allowlist; an explicit mapping is forwarded exactly as supplied after
    string conversion.  ``timeout_s`` bounds each connection operation and
    is clamped to 300 seconds.  ``on_event``/``event_callback`` may be
    synchronous or asynchronous and is never allowed to change client
    correctness.
    """

    def __init__(
        self,
        server: Optional[Command] = None,
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout_s: float = 30.0,
        on_event: Optional[EventCallback] = None,
        event_callback: Optional[EventCallback] = None,
        *,
        command: Optional[Command] = None,
        timeout: Optional[float] = None,
        event_handler: Optional[EventCallback] = None,
        event_sink: Optional[EventCallback] = None,
        argv: Optional[Command] = None,
    ) -> None:
        self.server = (
            command if command is not None else (argv if argv is not None else server)
        )
        self._argv, self._parse_error = _coerce_argv(self.server)
        self.cwd = str(cwd) if cwd is not None else None
        self._env = dict(env) if env is not None else None
        self._env_explicit = env is not None
        self._timeout_s = _bounded_timeout(
            timeout if timeout is not None else timeout_s
        )
        callbacks: List[EventCallback] = []
        if on_event is not None:
            callbacks.append(on_event)
        for callback in (event_callback, event_handler, event_sink):
            if callback is not None and callback not in callbacks:
                callbacks.append(callback)
        self._event_callbacks = tuple(callbacks)
        self._events: List[McpLifecycleEvent] = []
        self._status = McpHealthStatus.IDLE.value
        self._ok = False
        self._connected = False
        self._initialized = False
        self._tool_names: Tuple[str, ...] = ()
        self._last_phase = ""
        self._last_error = ""
        self._started_at: Optional[float] = None
        self._entered = False
        self._closing = False
        self._shutdown_done = False
        self._transport: Any = None
        self._streams: Any = None
        self._session: Any = None
        self._transport_entered = False
        self._session_entered = False
        self._initialize_result: Any = None

    @property
    def timeout_s(self) -> float:
        """Return the effective bounded per-operation timeout."""
        return self._timeout_s

    @property
    def events(self) -> Tuple[McpLifecycleEvent, ...]:
        """Return the immutable lifecycle event history."""
        return tuple(self._events)

    @property
    def lifecycle_events(self) -> Tuple[McpLifecycleEvent, ...]:
        """Return an alias for :attr:`events`."""
        return self.events

    @property
    def event_log(self) -> Tuple[McpLifecycleEvent, ...]:
        """Return an alias for :attr:`events`."""
        return self.events

    @property
    def last_event(self) -> Optional[McpLifecycleEvent]:
        """Return the most recently emitted event, if any."""
        return self._events[-1] if self._events else None

    @property
    def connected(self) -> bool:
        """Return whether an initialized session is currently active."""
        return self._connected

    @property
    def is_connected(self) -> bool:
        """Return an explicit boolean alias for :attr:`connected`."""
        return self.connected

    @property
    def closed(self) -> bool:
        """Return whether shutdown has completed."""
        return self._shutdown_done

    @property
    def initialized(self) -> bool:
        """Return whether MCP initialization completed successfully."""
        return self._initialized

    @property
    def is_initialized(self) -> bool:
        """Return an explicit boolean alias for :attr:`initialized`."""
        return self.initialized

    @property
    def status(self) -> str:
        """Return the current stable health state."""
        return self._status

    @property
    def phase(self) -> str:
        """Return the last stable lifecycle phase name."""
        return self._last_phase

    @property
    def last_error(self) -> str:
        """Return the last safe error text, or an empty string."""
        return self._last_error

    @property
    def tools(self) -> Tuple[str, ...]:
        """Return tool names observed by the latest successful list."""
        return self._tool_names

    @property
    def tool_count(self) -> int:
        """Return the number of tools observed by the latest list."""
        return len(self._tool_names)

    @property
    def session(self) -> Any:
        """Return the underlying SDK session for advanced integrations."""
        return self._session

    @property
    def health(self) -> McpHealth:
        """Return a frozen health snapshot; the snapshot is also callable."""
        elapsed = 0.0
        if self._started_at is not None:
            elapsed = max(0.0, time.monotonic() - self._started_at)
        return McpHealth(
            status=self._status,
            ok=self._ok,
            connected=self._connected,
            initialized=self._initialized,
            tool_count=len(self._tool_names),
            tool_names=self._tool_names,
            event_count=len(self._events),
            last_phase=self._last_phase,
            last_error=self._last_error,
            elapsed_s=elapsed,
            timeout_s=self._timeout_s,
            phases=tuple(event.phase.value for event in self._events),
            command=_safe_command(self._argv),
            cwd=self.cwd or str(Path.cwd()),
            transport="stdio",
            metadata={"explicit_env": self._env_explicit},
        )

    @property
    def health_metadata(self) -> McpHealth:
        """Return an alias for the frozen :attr:`health` snapshot."""
        return self.health

    @property
    def health_snapshot(self) -> McpHealth:
        """Return an alias for the frozen :attr:`health` snapshot."""
        return self.health

    @property
    def health_dict(self) -> Dict[str, Any]:
        """Return the current health projection as a plain dictionary."""
        return self.health.to_dict()

    def get_health(self) -> Dict[str, Any]:
        """Return the current health snapshot as a plain dictionary."""
        return self.health.to_dict()

    async def check_health(self) -> Dict[str, Any]:
        """Return current health without starting or probing a connection."""
        return self.get_health()

    async def health_check(self) -> Dict[str, Any]:
        """Alias for :meth:`check_health`."""
        return await self.check_health()

    def subscribe(self, callback: EventCallback) -> EventCallback:
        """Register an event callback and return it for caller ownership."""
        if not callable(callback):
            raise TypeError("MCP lifecycle callback must be callable")
        self._event_callbacks = (*self._event_callbacks, callback)
        return callback

    async def _emit(
        self,
        phase: Union[McpClientPhase, str],
        *,
        ok: bool = True,
        error: Optional[str] = None,
        tool: str = "",
        tool_count: int = 0,
        elapsed_s: float = 0.0,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> McpLifecycleEvent:
        """Append one frozen event and notify callbacks without raising."""
        event = McpLifecycleEvent(
            phase=phase,
            timestamp=time.time(),
            sequence=len(self._events) + 1,
            ok=ok,
            error=error,
            elapsed_s=elapsed_s,
            tool=tool,
            tool_count=tool_count,
            metadata=metadata or {},
        )
        self._events.append(event)
        self._last_phase = event.phase.value
        for callback in self._event_callbacks:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue
        return event

    async def _record_failure(
        self,
        phase: Union[McpClientPhase, str],
        error: Any,
        *,
        status: str = McpHealthStatus.FAILED.value,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> McpLifecycleEvent:
        """Record one expected failure and update safe health metadata."""
        text = (
            _exception_text(error) if isinstance(error, BaseException) else str(error)
        )
        self._last_error = _safe_health_text(text)
        self._status = McpHealthStatus.CLOSED.value if self._shutdown_done else status
        self._ok = False
        details = {
            "failed_phase": str(
                phase.value if isinstance(phase, McpClientPhase) else phase
            )
        }
        if metadata:
            details.update(dict(metadata))
        return await self._emit(
            McpClientPhase.FAILURE,
            ok=False,
            error=_safe_health_text(text),
            metadata=details,
        )

    async def _run_phase(
        self,
        phase: McpClientPhase,
        operation: Callable[[], Awaitable[Any]],
        **metadata: Any,
    ) -> Any:
        """Run one SDK operation under the configured bounded timeout."""
        try:
            return await _await_bounded(operation(), self._timeout_s)
        except asyncio.TimeoutError as exc:
            error = "TimeoutError: operation timed out"
            await self._record_failure(
                phase,
                error,
                status=McpHealthStatus.TIMEOUT.value,
                metadata=metadata or None,
            )
            raise _PhaseFailure(error, McpHealthStatus.TIMEOUT.value) from exc
        except asyncio.CancelledError:
            self._status = McpHealthStatus.CANCELLED.value
            self._last_error = "CancelledError: operation cancelled"
            self._ok = False
            try:
                await self._emit(
                    McpClientPhase.FAILURE,
                    ok=False,
                    error=self._last_error,
                    metadata={"failed_phase": phase.value},
                )
            except asyncio.CancelledError:
                pass
            raise
        except Exception as exc:
            error = _exception_text(exc)
            await self._record_failure(phase, exc, metadata=metadata or None)
            raise _PhaseFailure(error) from exc

    async def _cleanup_call(self, callback: Callable[[], Any]) -> Optional[str]:
        """Run one cleanup callback with a timeout and return safe text."""
        try:
            result = callback()
            if inspect.isawaitable(result):
                await _await_bounded(result, self._timeout_s)
        except asyncio.TimeoutError:
            return "TimeoutError: shutdown timed out"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _exception_text(exc)
        return None

    async def _cleanup_resources(self) -> List[str]:
        """Close the session before its stdio transport, collecting errors."""
        errors: List[str] = []
        if self._session_entered and self._session is not None:
            error = await self._cleanup_call(
                lambda: self._session.__aexit__(None, None, None)
            )
            if error:
                errors.append(error)
            self._session_entered = False
        if self._transport_entered and self._transport is not None:
            error = await self._cleanup_call(
                lambda: self._transport.__aexit__(None, None, None)
            )
            if error:
                errors.append(error)
            self._transport_entered = False
        self._session = None
        self._streams = None
        self._transport = None
        self._connected = False
        return errors

    async def _cancel_and_cleanup(self) -> None:
        """Best-effort cleanup used when connection setup is cancelled."""
        try:
            await self._cleanup_resources()
        except asyncio.CancelledError:
            pass

    async def connect(self) -> "McpClient":
        """Start the one owned stdio connection and initialize MCP."""
        if self._initialized and self._session is not None:
            return self
        if self._shutdown_done:
            self._status = McpHealthStatus.CLOSED.value
            return self
        if self._transport is not None and self._session is None:
            await self._cancel_and_cleanup()
        self._entered = True
        self._started_at = time.monotonic()
        self._status = McpHealthStatus.CONNECTING.value
        self._ok = False
        self._last_error = ""
        await self._emit(
            McpClientPhase.SPAWN,
            metadata={"command": _safe_command(self._argv), "transport": "stdio"},
        )
        phase = McpClientPhase.SPAWN
        try:
            if self._parse_error:
                raise ValueError(self._parse_error)
            if not self._argv:
                raise ValueError("empty server command")
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=self._argv[0],
                args=self._argv[1:],
                env=_child_environment(self._env),
                cwd=self.cwd or str(Path.cwd()),
            )
            self._transport = stdio_client(params, errlog=_server_errlog())
            self._transport_entered = True
            streams = await self._run_phase(
                phase,
                lambda: self._transport.__aenter__(),
                command=_safe_command(self._argv),
            )
            self._streams = streams
            phase = McpClientPhase.INITIALIZE
            await self._emit(
                McpClientPhase.INITIALIZE,
                metadata={"transport": "stdio"},
            )
            read_stream, write_stream = streams
            self._session = ClientSession(read_stream, write_stream)
            self._session_entered = True
            await self._run_phase(phase, lambda: self._session.__aenter__())
            self._initialize_result = await self._run_phase(
                phase, lambda: self._session.initialize()
            )
            self._initialized = True
            self._connected = True
            self._ok = True
            self._status = McpHealthStatus.READY.value
        except _PhaseFailure:
            await self._cancel_and_cleanup()
            return self
        except asyncio.CancelledError:
            await self._cancel_and_cleanup()
            raise
        except Exception as exc:
            await self._record_failure(phase, exc)
            await self._cancel_and_cleanup()
            return self
        return self

    async def initialize(self) -> bool:
        """Ensure the connection exists and report initialization success."""
        if not self._entered:
            await self.__aenter__()
        return self._initialized

    async def __aenter__(self) -> "McpClient":
        """Enter the reusable client context and return this instance."""
        return await self.connect()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the owned transport and preserve any body exception."""
        await self.close()
        return False

    async def close(self) -> None:
        """Close the session and transport exactly once without expected raises."""
        if self._shutdown_done or self._closing:
            return
        self._closing = True
        try:
            errors = await self._cleanup_resources()
            if errors:
                self._last_error = _safe_health_text("; ".join(errors))
                self._ok = False
                await self._emit(
                    McpClientPhase.FAILURE,
                    ok=False,
                    error=self._last_error,
                    metadata={"failed_phase": McpClientPhase.SHUTDOWN.value},
                )
            self._status = McpHealthStatus.CLOSED.value
            await self._emit(
                McpClientPhase.SHUTDOWN,
                ok=not errors,
                error=self._last_error if errors else None,
                tool_count=len(self._tool_names),
            )
        finally:
            self._closing = False
            self._shutdown_done = True

    async def shutdown(self) -> None:
        """Alias for :meth:`close` for lifecycle-oriented callers."""
        await self.close()

    async def aclose(self) -> None:
        """Alias for :meth:`close` for async resource-manager callers."""
        await self.close()

    async def list_tools(self) -> Dict[str, Any]:
        """List tools through the active session and return wrapper-shaped data."""
        if self._session is None or not self._initialized:
            message = self._last_error or (
                "MCP client is closed"
                if self._shutdown_done
                else "MCP client is not connected"
            )
            await self._emit(McpClientPhase.LIST)
            await self._record_failure(McpClientPhase.LIST, message)
            return {"ok": False, "tools": [], "error": message}
        await self._emit(McpClientPhase.LIST)
        try:
            response = await self._run_phase(
                McpClientPhase.LIST,
                lambda: self._session.list_tools(),
            )
            tools = _tools_from_response(response)
        except _PhaseFailure as failure:
            return {"ok": False, "tools": [], "error": failure.error}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _exception_text(exc)
            await self._record_failure(McpClientPhase.LIST, exc)
            return {"ok": False, "tools": [], "error": error}
        self._tool_names = tuple(tool["name"] for tool in tools)
        self._status = McpHealthStatus.READY.value
        self._ok = True
        self._last_error = ""
        return {"ok": True, "tools": tools}

    async def list(self) -> Dict[str, Any]:
        """Alias for :meth:`list_tools`."""
        return await self.list_tools()

    async def call_tool(
        self,
        tool: str,
        args: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Call one tool through the active session without raising tool errors."""
        tool_name = str(tool or "")
        if not tool_name:
            await self._emit(McpClientPhase.CALL)
            await self._record_failure(McpClientPhase.CALL, "empty tool name")
            return {"ok": False, "text": "", "error": "empty tool name"}
        if self._session is None or not self._initialized:
            message = self._last_error or (
                "MCP client is closed"
                if self._shutdown_done
                else "MCP client is not connected"
            )
            await self._emit(McpClientPhase.CALL, tool=tool_name)
            await self._record_failure(
                McpClientPhase.CALL, message, metadata={"tool": tool_name}
            )
            return {"ok": False, "text": "", "error": message}
        if args is None:
            call_args: Dict[str, Any] = {}
        elif isinstance(args, Mapping):
            call_args = dict(args)
        else:
            message = "MCP tool arguments must be a mapping"
            await self._emit(McpClientPhase.CALL, tool=tool_name)
            await self._record_failure(
                McpClientPhase.CALL,
                message,
                metadata={"tool": tool_name},
            )
            return {"ok": False, "text": "", "error": message}
        await self._emit(McpClientPhase.CALL, tool=tool_name)
        try:
            response = await self._run_phase(
                McpClientPhase.CALL,
                lambda: self._session.call_tool(tool_name, call_args),
                tool=tool_name,
            )
        except _PhaseFailure as failure:
            return {"ok": False, "text": "", "error": failure.error}
        except asyncio.CancelledError:
            raise
        text = _content_text(_result_error(response, "content", None))
        is_error = _result_error(response, "is_error", False)
        if is_error is None:
            is_error = _result_error(response, "isError", False)
        if bool(is_error):
            error_text = text or "MCP tool reported an error"
            await self._record_failure(
                McpClientPhase.CALL,
                error_text,
                status=McpHealthStatus.DEGRADED.value,
                metadata={"tool": tool_name, "tool_error": True},
            )
            return {"ok": False, "text": text, "error": error_text}
        self._status = McpHealthStatus.READY.value
        self._ok = True
        self._last_error = ""
        return {"ok": True, "text": text, "error": None}

    async def call(
        self, tool: str, args: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        """Alias for :meth:`call_tool`."""
        return await self.call_tool(tool, args)


MCPClient = McpClient


@asynccontextmanager
async def _session_context(
    server_argv: List[str],
    cwd: Optional[str],
    env: Optional[Mapping[str, str]],
):
    """Yield one initialized SDK session while preserving the private helper."""
    client = McpClient(server_argv, cwd=cwd, env=env)
    await client.__aenter__()
    try:
        if client._session is None:
            raise RuntimeError(client._last_error or "MCP client did not connect")
        yield client._session
    finally:
        await client.__aexit__(None, None, None)


async def _list_tools(
    server_argv: List[str],
    cwd: Optional[str],
    env: Optional[Mapping[str, str]],
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Run a one-shot list operation through the typed client."""
    client = McpClient(server_argv, cwd=cwd, env=env, timeout_s=timeout_s)
    try:
        async with client:
            return await client.list_tools()
    except Exception as exc:
        return {"ok": False, "tools": [], "error": _exception_text(exc)}


async def _call_tool(
    server_argv: List[str],
    tool: str,
    args: Dict[str, Any],
    cwd: Optional[str],
    env: Optional[Mapping[str, str]],
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Run a one-shot call operation through the typed client."""
    client = McpClient(server_argv, cwd=cwd, env=env, timeout_s=timeout_s)
    try:
        async with client:
            return await client.call_tool(tool, args)
    except Exception as exc:
        return {"ok": False, "text": "", "error": _exception_text(exc)}


def list_mcp_tools(
    server: str,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    *,
    timeout_s: float = 30.0,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Spawn an external MCP server and list its tools synchronously.

    The return shape remains ``{"ok", "tools", "error"}``.  Expected parse,
    spawn, initialization, timeout, and tool-listing failures return
    ``ok=False`` instead of raising.  ``timeout`` is an additive alias for
    ``timeout_s``.
    """
    try:
        argv = parse_server_command(server)
    except Exception as exc:
        return {"ok": False, "error": _exception_text(exc), "tools": []}
    if not argv:
        return {"ok": False, "error": "empty server command", "tools": []}
    effective_timeout = _bounded_timeout(timeout if timeout is not None else timeout_s)
    operation = _list_tools(argv, cwd, env, effective_timeout)
    completed, value = _run_sync_bounded(operation, effective_timeout)
    if completed:
        if isinstance(value, dict):
            return value
        return {"ok": False, "error": "bad client response", "tools": []}
    if value is not None:
        return {"ok": False, "error": _exception_text(value), "tools": []}
    return {
        "ok": False,
        "error": "TimeoutError: operation timed out",
        "tools": [],
    }


def call_mcp_tool(
    server: str,
    tool: str,
    args: Optional[Mapping[str, Any]] = None,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    *,
    timeout_s: float = 30.0,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Call one external MCP tool synchronously with compatibility results.

    The return shape remains ``{"ok", "text", "error"}``.  Expected command,
    connection, timeout, cancellation-at-the-sync-boundary, and tool errors
    are represented as data and do not escape as exceptions.
    """
    try:
        argv = parse_server_command(server)
    except Exception as exc:
        return {"ok": False, "text": "", "error": _exception_text(exc)}
    if not argv:
        return {"ok": False, "text": "", "error": "empty server command"}
    try:
        call_args = dict(args or {})
    except Exception as exc:
        return {"ok": False, "text": "", "error": _exception_text(exc)}
    effective_timeout = _bounded_timeout(timeout if timeout is not None else timeout_s)
    operation = _call_tool(argv, tool, call_args, cwd, env, effective_timeout)
    completed, value = _run_sync_bounded(operation, effective_timeout)
    if completed:
        if isinstance(value, dict):
            return value
        return {"ok": False, "text": "", "error": "bad client response"}
    if value is not None:
        return {"ok": False, "text": "", "error": _exception_text(value)}
    return {
        "ok": False,
        "text": "",
        "error": "TimeoutError: operation timed out",
    }
