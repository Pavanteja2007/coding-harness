"""Injectable newline-delimited JSON-RPC transports for ACP version 1."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from shared.security import redact_text, scrub_environment

from ._compat import ImmediateAwaitable, call_maybe_async
from .models import (
    ACPProtocolError,
    ACPRequest,
    ACPResponse,
    ACPTimeoutError,
    ACPTransportClosed,
    ACPTransportError,
)

_EOF = object()
_DEFAULT_MAX_FRAME_BYTES = 1024 * 1024
MAX_ACP_FRAME_BYTES = _DEFAULT_MAX_FRAME_BYTES
_MAX_CONFIGURED_FRAME_BYTES = 64 * 1024 * 1024


class _ReceivedMessage(dict[str, Any]):
    """A received mapping that supports both sync and await-style callers."""

    def __await__(self):
        if False:
            yield
        return self


class ACPTransport(Protocol):
    """Structural transport contract used by ACPClient and ACPServer."""

    def start(self) -> Any:
        """Start the transport if it owns a process or connection."""

    def send(self, message: Mapping[str, Any]) -> Any:
        """Send one JSON object to the peer."""

    def receive(self, timeout: float | None = None) -> Mapping[str, Any]:
        """Receive one JSON object from the peer."""

    def close(self) -> Any:
        """Close the transport and release its resources."""


def validate_acp_argv(argv: Sequence[str] | str | None) -> list[str]:
    """Return a shell-free argument vector for a child ACP process.

    A string is split with shell-like quoting rules but is never passed to a
    shell.  The resulting vector is checked for NUL/control characters and
    an empty executable before it can reach ``subprocess``.
    """
    if isinstance(argv, str):
        try:
            values = shlex.split(argv, posix=False)
        except ValueError as exc:
            raise ValueError("ACP argv could not be parsed") from exc
        cleaned: list[str] = []
        for value in values:
            text = str(value)
            if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
                text = text[1:-1]
            cleaned.append(text)
        values = cleaned
    elif isinstance(argv, Sequence) and not isinstance(argv, (bytes, bytearray)):
        values = [str(item) for item in argv]
    else:
        raise TypeError("ACP argv must be a sequence of strings")
    if not values or not values[0].strip():
        raise ValueError("ACP argv must not be empty")
    for value in values:
        if "\x00" in value or any(
            ord(char) < 32 and char not in "\t" for char in value
        ):
            raise ValueError("ACP argv contains a control character")
    return values


parse_acp_argv = validate_acp_argv
safe_argv = validate_acp_argv


class InMemoryACPTransport:
    """A thread-safe bidirectional in-memory ACP transport.

    Instances are endpoints, not brokers.  Use :meth:`pair` or
    :meth:`create_pair` to create linked client/server endpoints.
    """

    def __init__(
        self, peer: "InMemoryACPTransport | None" = None, *, max_queue: int = 0
    ) -> None:
        """Create an unconnected endpoint or connect it to ``peer``."""
        self.peer: InMemoryACPTransport | None = None
        self.max_queue = int(max_queue or 0)
        self._incoming: queue.Queue[Any] = queue.Queue()
        self._request_deferred: deque[Mapping[str, Any]] = deque()
        self._next_request_id = 1
        self._completed_request_ids: set[int | str] = set()
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._closed = False
        self._started = False
        if peer is not None:
            self.connect(peer)

    @classmethod
    def pair(cls) -> tuple["InMemoryACPTransport", "InMemoryACPTransport"]:
        """Create and connect two endpoints for an in-process round trip."""
        left = cls()
        right = cls()
        left.connect(right)
        return left, right

    @classmethod
    def create_pair(cls) -> tuple["InMemoryACPTransport", "InMemoryACPTransport"]:
        """Alias for :meth:`pair` used by transport-oriented callers."""
        return cls.pair()

    def connect(
        self, peer: "InMemoryACPTransport | None" = None
    ) -> "InMemoryACPTransport":
        """Connect this endpoint to ``peer`` and return the peer endpoint."""
        if peer is None:
            peer = type(self)()
        if not isinstance(peer, InMemoryACPTransport):
            raise TypeError(
                "InMemoryACPTransport peers must use the same transport type"
            )
        with self._state_lock:
            if self.peer is not None and self.peer is not peer:
                raise RuntimeError("ACP transport is already connected")
            if peer.peer is not None and peer.peer is not self:
                raise RuntimeError("peer ACP transport is already connected")
            self.peer = peer
            peer.peer = self
        return peer

    attach = connect

    def start(self) -> "InMemoryACPTransport":
        """Mark the endpoint ready; no external resource is required."""
        with self._state_lock:
            if self._closed:
                raise ACPTransportClosed(-32000, "ACP transport is closed")
            self._started = True
        return self

    def _ensure_open(self) -> None:
        """Raise a typed error when the endpoint cannot send or receive."""
        if self._closed:
            raise ACPTransportClosed(-32000, "ACP transport is closed")
        if self.peer is None:
            raise ACPTransportError(-32000, "ACP transport has no connected peer")

    @staticmethod
    def _wire_message(message: Any) -> dict[str, Any]:
        """Convert a typed model or mapping to a JSON wire object."""
        if isinstance(message, (ACPRequest, ACPResponse)):
            value = message.to_dict()
        elif isinstance(message, Mapping):
            value = dict(message)
        elif isinstance(message, (str, bytes, bytearray)):
            try:
                decoded = json.loads(message)
            except (TypeError, ValueError) as exc:
                raise ACPTransportError(
                    -32602, "ACP transport message is not valid JSON"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise ACPTransportError(
                    -32602, "ACP transport message must be a JSON object"
                )
            value = dict(decoded)
        else:
            raise ACPTransportError(-32602, "ACP transport message must be a mapping")
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ACPTransportError(
                -32602, "ACP transport message is not JSON serializable"
            ) from exc
        return dict(value)

    def send(self, message: Mapping[str, Any]) -> ImmediateAwaitable[None]:
        """Queue one message for the connected peer without using a shell."""
        self._ensure_open()
        value = self._wire_message(message)
        peer = self.peer
        if peer is None or peer._closed:
            raise ACPTransportClosed(-32000, "ACP transport peer is closed")
        if self.max_queue > 0 and peer._incoming.qsize() >= self.max_queue:
            raise ACPTransportError(-32000, "ACP transport queue is full")
        with self._write_lock:
            peer._incoming.put(value)
        return ImmediateAwaitable(None)

    def receive(self, timeout: float | None = None) -> dict[str, Any]:
        """Receive one message, raising a typed timeout or close error."""
        self._ensure_open()
        if self._request_deferred:
            return _ReceivedMessage(self._request_deferred.popleft())
        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        )
        while True:
            if deadline is None:
                wait = 0.05
            else:
                wait = min(0.05, max(0.0, deadline - time.monotonic()))
            try:
                value = self._incoming.get(timeout=wait)
            except queue.Empty as exc:
                if deadline is not None and time.monotonic() >= deadline:
                    raise ACPTimeoutError(
                        -32000, "ACP transport receive timed out"
                    ) from exc
                continue
            if value is _EOF:
                self._closed = True
                raise ACPTransportClosed(-32000, "ACP transport peer closed")
            return _ReceivedMessage(value)

    def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> ImmediateAwaitable[None]:
        """Send a JSON-RPC notification through this endpoint."""
        return self.send(ACPRequest(method=method, params=params or {}, id=None))

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout_s: float | None = 30.0,
    ) -> Any:
        """Send a request and wait only for its matching response ID."""
        request_id = self._next_request_id
        self._next_request_id += 1
        request = ACPRequest(method=method, params=params or {}, id=request_id)
        deferred = self._take_deferred_response(request_id)
        if deferred is None:
            self.send(request)
        deadline = (
            None if timeout_s is None else time.monotonic() + max(0.0, float(timeout_s))
        )
        while True:
            if deferred is not None:
                response = deferred
                deferred = None
            else:
                wait = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                if deadline is not None and wait <= 0:
                    raise ACPTimeoutError(-32000, "ACP transport request timed out")
                try:
                    response = self._incoming.get(timeout=wait)
                except queue.Empty as exc:
                    raise ACPTimeoutError(
                        -32000, "ACP transport request timed out"
                    ) from exc
                if response is _EOF:
                    self._closed = True
                    raise ACPTransportClosed(-32000, "ACP transport peer closed")
            response_id = response.get("id")
            if response_id == request_id and not isinstance(response_id, bool):
                self._completed_request_ids.add(request_id)
                parsed = ACPResponse.from_dict(response)
                if parsed.error is not None:
                    raise parsed.error
                return parsed.result
            if response_id not in self._completed_request_ids:
                self._request_deferred.append(dict(response))

    def _take_deferred_response(
        self, request_id: int | str
    ) -> Mapping[str, Any] | None:
        """Remove a response retained for a later transport request."""
        for index, message in enumerate(self._request_deferred):
            if message.get("id") == request_id and not isinstance(
                message.get("id"), bool
            ):
                del self._request_deferred[index]
                return message
        return None

    async def asend(self, message: Mapping[str, Any]) -> None:
        """Asynchronously send one message without occupying a worker thread."""
        self.send(message)

    async def areceive(self, timeout: float | None = None) -> dict[str, Any]:
        """Asynchronously receive one message without blocking an event-loop thread."""
        self._ensure_open()
        if self._request_deferred:
            return dict(self._request_deferred.popleft())
        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        )
        while True:
            try:
                value = self._incoming.get_nowait()
            except queue.Empty:
                if deadline is not None and time.monotonic() >= deadline:
                    raise ACPTimeoutError(
                        -32000, "ACP transport receive timed out"
                    ) from None
                await asyncio.sleep(0.001)
                continue
            if value is _EOF:
                self._closed = True
                raise ACPTransportClosed(-32000, "ACP transport peer closed")
            return dict(value)

    def close(self) -> ImmediateAwaitable[None]:
        """Close this endpoint and wake the peer with a typed EOF."""
        with self._state_lock:
            if self._closed:
                return ImmediateAwaitable(None)
            self._closed = True
            peer = self.peer
        if peer is not None:
            peer._incoming.put(_EOF)
        return ImmediateAwaitable(None)

    send_message = send
    receive_message = receive
    write = send
    read = receive
    write_message = send
    read_message = receive
    open = start
    shutdown = close

    @property
    def closed(self) -> bool:
        """Return whether this endpoint has been closed."""
        return self._closed

    @property
    def started(self) -> bool:
        """Return whether :meth:`start` has been called."""
        return self._started

    def __enter__(self) -> "InMemoryACPTransport":
        """Start the endpoint for a synchronous context manager."""
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the endpoint on context exit."""
        self.close()
        return False

    async def __aenter__(self) -> "InMemoryACPTransport":
        """Start the endpoint for an asynchronous context manager."""
        self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the endpoint on asynchronous context exit."""
        await call_maybe_async(self, "close")
        return False


class _DecodeFailure:
    """Internal queue item retaining a malformed stdio line for the server."""

    def __init__(self, raw: bytes, reason: str = "malformed JSON") -> None:
        self.raw = raw
        self.reason = reason


def _frame_limit(value: Any) -> int:
    """Validate and bound a configurable stdio frame size."""
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ACP frame size must be an integer") from exc
    if result < 1 or result > _MAX_CONFIGURED_FRAME_BYTES:
        raise ValueError("ACP frame size is outside the safe range")
    return result


def _reject_json_constant(value: str) -> None:
    """Reject non-standard JSON numeric constants."""
    raise ValueError(f"unsupported JSON constant: {value}")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate member names."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _decode_json_frame(
    raw: bytes, max_bytes: int = _DEFAULT_MAX_FRAME_BYTES
) -> Mapping[str, Any]:
    """Decode one bounded UTF-8 JSONL frame with strict control framing."""
    if not raw or len(raw) > max_bytes:
        raise ValueError("ACP frame is empty or too large")
    if any(byte < 0x20 and byte not in {9, 10, 13} for byte in raw):
        raise ValueError("ACP frame contains a control character")
    if b"\x7f" in raw:
        raise ValueError("ACP frame contains a control character")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("ACP frame is not valid UTF-8") from exc
    if any(ord(char) < 0x20 and char not in "\t\r\n" for char in text):
        raise ValueError("ACP frame contains a control character")
    try:
        value = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_json_object,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("ACP frame is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("ACP frame must contain a JSON object")
    return dict(value)


def _drain_oversized_frame(stream: Any, initial: bytes) -> None:
    """Discard the remainder of an oversized newline frame without buffering it."""
    if initial.endswith(b"\n"):
        return
    while True:
        chunk = stream.readline(4096)
        if not chunk or chunk.endswith(b"\n"):
            return


class StdioACPTransport:
    """A safe subprocess ACP transport using stdout for protocol only."""

    def __init__(
        self,
        argv: Sequence[str] | str | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        *,
        timeout_s: float = 30.0,
        timeout: float | None = None,
        max_stderr_bytes: int = 65536,
        max_stderr: int | None = None,
        max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
        max_frame_size: int | None = None,
        max_line_bytes: int | None = None,
        max_stdout_bytes: int | None = None,
        allow_env: Sequence[str] = (),
        command: Sequence[str] | str | None = None,
    ) -> None:
        """Configure a shell-free child process and a bounded stderr drain."""
        if argv is None:
            argv = command
        if timeout is not None:
            timeout_s = timeout
        if max_stderr is not None:
            max_stderr_bytes = max_stderr
        self.argv = validate_acp_argv(argv)
        self.cwd = str(cwd) if cwd is not None else None
        self.timeout_s = max(0.01, float(timeout_s))
        self.max_stderr_bytes = max(0, int(max_stderr_bytes))
        selected_frame_size = max_line_bytes
        if selected_frame_size is None:
            selected_frame_size = max_frame_size
        if selected_frame_size is None:
            selected_frame_size = max_stdout_bytes
        if selected_frame_size is None:
            selected_frame_size = max_frame_bytes
        self.max_frame_bytes = _frame_limit(selected_frame_size)
        self.allow_env = tuple(str(item) for item in allow_env)
        self._requested_env = dict(env or {})
        self.env = self._scrub_environment()
        self.process: subprocess.Popen[bytes] | None = None
        self._incoming: queue.Queue[Any] = queue.Queue()
        self._request_deferred: deque[Mapping[str, Any]] = deque()
        self._next_request_id = 1
        self._completed_request_ids: set[int | str] = set()
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr = bytearray()
        self._closed = False
        self._started = False
        self._eof = False

    def _scrub_environment(self) -> dict[str, str]:
        """Return an isolated child environment with credentials removed."""
        source = dict(os.environ)
        source.update(self._requested_env)
        return scrub_environment(source, allow=self.allow_env)

    def start(self) -> "StdioACPTransport":
        """Spawn the child and begin bounded stdout/stderr reader threads."""
        with self._state_lock:
            if self._closed:
                raise ACPTransportClosed(-32000, "ACP stdio transport is closed")
            if self._started:
                return self
            if self.cwd is not None and not Path(self.cwd).is_dir():
                raise ACPTransportError(
                    -32000, "ACP stdio working directory does not exist"
                )
            try:
                self.process = subprocess.Popen(
                    self.argv,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    bufsize=0,
                )
            except (OSError, ValueError) as exc:
                raise ACPTransportError(
                    -32000, "ACP stdio process could not be started"
                ) from exc
            self._started = True
            self._reader_thread = threading.Thread(
                target=self._read_stdout,
                name="neo-acp-stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                name="neo-acp-stderr",
                daemon=True,
            )
            self._reader_thread.start()
            self._stderr_thread.start()
        return self

    def _read_stdout(self) -> None:
        """Read bounded UTF-8 JSONL frames from stdout into the input queue."""
        process = self.process
        if process is None or process.stdout is None:
            self._incoming.put(_EOF)
            return
        try:
            while not self._closed:
                line = process.stdout.readline(self.max_frame_bytes + 2)
                if not line:
                    break
                payload = line[:-1] if line.endswith(b"\n") else line
                oversized = len(payload) > self.max_frame_bytes
                if oversized:
                    _drain_oversized_frame(process.stdout, line)
                    self._incoming.put(
                        _DecodeFailure(line[: self.max_frame_bytes], "frame too large")
                    )
                    continue
                if line.endswith(b"\n"):
                    raw = line[:-1]
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                else:
                    raw = line
                    self._incoming.put(
                        _DecodeFailure(
                            raw[: self.max_frame_bytes], "unterminated frame"
                        )
                    )
                    continue
                if not raw.strip():
                    continue
                try:
                    value = _decode_json_frame(raw, self.max_frame_bytes)
                except ValueError as exc:
                    self._incoming.put(
                        _DecodeFailure(raw[: self.max_frame_bytes], str(exc))
                    )
                    continue
                self._incoming.put(dict(value))
        except (OSError, ValueError):
            pass
        finally:
            self._eof = True
            self._incoming.put(_EOF)

    def _read_stderr(self) -> None:
        """Drain stderr into a bounded in-memory tail without logging it."""
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while not self._closed:
                chunk = process.stderr.read(4096)
                if not chunk:
                    break
                if self.max_stderr_bytes:
                    self._stderr.extend(chunk)
                    if len(self._stderr) > self.max_stderr_bytes:
                        del self._stderr[: len(self._stderr) - self.max_stderr_bytes]
        except (OSError, ValueError):
            pass

    def _ensure_open(self) -> subprocess.Popen[bytes]:
        """Return the live process or raise a typed transport error."""
        with self._state_lock:
            process = self.process
            if (
                self._closed
                or process is None
                or (process.poll() is not None and self._eof)
            ):
                raise ACPTransportClosed(-32000, "ACP stdio transport is closed")
            if process is None:
                self.start()
                process = self.process
        if process is None or process.stdin is None:
            raise ACPTransportError(-32000, "ACP stdio process has no input pipe")
        return process

    def send(self, message: Mapping[str, Any]) -> ImmediateAwaitable[None]:
        """Write exactly one compact JSON line to child stdout."""
        if isinstance(message, (ACPRequest, ACPResponse)):
            value = message.to_dict()
        elif isinstance(message, Mapping):
            value = dict(message)
        elif isinstance(message, (str, bytes, bytearray)):
            try:
                decoded = json.loads(message)
            except (TypeError, ValueError) as exc:
                raise ACPTransportError(
                    -32602, "ACP stdio message is not valid JSON"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise ACPTransportError(
                    -32602, "ACP stdio message must be a JSON object"
                )
            value = dict(decoded)
        else:
            raise ACPTransportError(-32602, "ACP stdio message must be a mapping")
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ACPTransportError(
                -32602, "ACP stdio message is not JSON serializable"
            ) from exc
        encoded = payload.encode("utf-8")
        if len(encoded) + 1 > self.max_frame_bytes:
            raise ACPTransportError(-32602, "ACP stdio frame is too large")
        process = self._ensure_open()
        with self._write_lock:
            try:
                process.stdin.write(encoded + b"\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ACPTransportClosed(
                    -32000, "ACP stdio process is not accepting input"
                ) from exc
        return ImmediateAwaitable(None)

    def receive(self, timeout: float | None = None) -> dict[str, Any]:
        """Receive one decoded protocol line from child stdout."""
        if self.process is None and not self._closed:
            self.start()
        if self._request_deferred:
            return _ReceivedMessage(self._request_deferred.popleft())
        wait = self.timeout_s if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ACPTimeoutError(-32000, "ACP stdio receive timed out")
            try:
                value = self._incoming.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if value is _EOF:
                self._eof = True
                raise ACPTransportClosed(-32000, "ACP stdio peer closed")
            if isinstance(value, _DecodeFailure):
                raise ACPProtocolError(
                    -32700, f"ACP stdio frame rejected: {value.reason}"
                )
            return _ReceivedMessage(value)

    def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> ImmediateAwaitable[None]:
        """Send a JSON-RPC notification over stdout."""
        return self.send(ACPRequest(method=method, params=params or {}, id=None))

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout_s: float | None = 30.0,
    ) -> Any:
        """Send a request and wait only for its matching response ID."""
        request_id = self._next_request_id
        self._next_request_id += 1
        request = ACPRequest(method=method, params=params or {}, id=request_id)
        deferred = self._take_deferred_response(request_id)
        if deferred is None:
            self.send(request)
        deadline = (
            None if timeout_s is None else time.monotonic() + max(0.0, float(timeout_s))
        )
        while True:
            if deferred is not None:
                response = deferred
                deferred = None
            else:
                wait = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                if deadline is not None and wait <= 0:
                    raise ACPTimeoutError(-32000, "ACP stdio request timed out")
                try:
                    response = self._incoming.get(timeout=wait)
                except queue.Empty as exc:
                    raise ACPTimeoutError(
                        -32000, "ACP transport request timed out"
                    ) from exc
                if response is _EOF:
                    self._closed = True
                    raise ACPTransportClosed(-32000, "ACP transport peer closed")
                if isinstance(response, _DecodeFailure):
                    raise ACPProtocolError(
                        -32700, f"ACP stdio frame rejected: {response.reason}"
                    )
            response_id = response.get("id")
            if response_id == request_id and not isinstance(response_id, bool):
                self._completed_request_ids.add(request_id)
                parsed = ACPResponse.from_dict(response)
                if parsed.error is not None:
                    raise parsed.error
                return parsed.result
            if response_id not in self._completed_request_ids:
                self._request_deferred.append(dict(response))

    def _take_deferred_response(
        self, request_id: int | str
    ) -> Mapping[str, Any] | None:
        """Remove a response retained for a later stdio request."""
        for index, message in enumerate(self._request_deferred):
            if message.get("id") == request_id and not isinstance(
                message.get("id"), bool
            ):
                del self._request_deferred[index]
                return message
        return None

    async def asend(self, message: Mapping[str, Any]) -> None:
        """Asynchronously send one protocol message."""
        await call_maybe_async(self, "send", message)

    async def areceive(self, timeout: float | None = None) -> dict[str, Any]:
        """Asynchronously receive one protocol message without blocking the loop."""
        if self.process is None and not self._closed:
            self.start()
        if self._request_deferred:
            return dict(self._request_deferred.popleft())
        wait = self.timeout_s if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ACPTimeoutError(-32000, "ACP stdio receive timed out")
            try:
                value = self._incoming.get_nowait()
            except queue.Empty:
                await asyncio.sleep(min(0.001, remaining))
                continue
            if value is _EOF:
                self._eof = True
                raise ACPTransportClosed(-32000, "ACP stdio peer closed")
            if isinstance(value, _DecodeFailure):
                raise ACPProtocolError(
                    -32700, f"ACP stdio frame rejected: {value.reason}"
                )
            return dict(value)

    def close(self) -> ImmediateAwaitable[None]:
        """Close pipes, terminate the child, and bound all reader threads."""
        with self._state_lock:
            if self._closed:
                return ImmediateAwaitable(None)
            self._closed = True
            process = self.process
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=1.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._incoming.put(_EOF)
        return ImmediateAwaitable(None)

    send_message = send
    receive_message = receive
    write = send
    read = receive
    write_message = send
    read_message = receive
    open = start
    shutdown = close
    cancel = close

    @property
    def closed(self) -> bool:
        """Return whether the transport has been closed."""
        return self._closed

    @property
    def max_frame_size(self) -> int:
        """Return the configured frame bound under its size alias."""
        return self.max_frame_bytes

    @property
    def max_line_bytes(self) -> int:
        """Return the configured outbound/inbound frame bound."""
        return self.max_frame_bytes

    @property
    def max_stdout_bytes(self) -> int:
        """Return the stdout frame bound under its compatibility name."""
        return self.max_frame_bytes

    @property
    def started(self) -> bool:
        """Return whether the child process has been spawned."""
        return self._started

    @property
    def returncode(self) -> int | None:
        """Return the child exit code when it has exited."""
        return self.process.poll() if self.process is not None else None

    @property
    def stderr_bytes(self) -> bytes:
        """Return bounded, redacted stderr bytes retained for diagnostics."""
        return redact_text(self.stderr_tail).encode("utf-8", errors="replace")[
            : self.max_stderr_bytes
        ]

    @property
    def stderr_tail(self) -> str:
        """Return a bounded, redacted stderr tail without writing it anywhere."""
        return redact_text(bytes(self._stderr).decode("utf-8", errors="replace"))

    def __enter__(self) -> "StdioACPTransport":
        """Start the child process for a synchronous context manager."""
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the child process on context exit."""
        self.close()
        return False

    async def __aenter__(self) -> "StdioACPTransport":
        """Start the child process for an asynchronous context manager."""
        await call_maybe_async(self, "start")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Close the child process on asynchronous context exit."""
        await call_maybe_async(self, "close")
        return False


InMemoryTransport = InMemoryACPTransport
StdioTransport = StdioACPTransport

__all__ = [
    "MAX_ACP_FRAME_BYTES",
    "ACPTransport",
    "InMemoryACPTransport",
    "InMemoryTransport",
    "StdioACPTransport",
    "StdioTransport",
    "parse_acp_argv",
    "safe_argv",
    "validate_acp_argv",
]
