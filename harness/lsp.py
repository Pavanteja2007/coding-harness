"""Optional Language Server Protocol lifecycle and diagnostics.

The harness treats LSP as an optional read-only enhancement. A missing server,
a failed spawn, an unsupported request, or a timeout returns an explicit empty
or unavailable result and never changes a task outcome. The implementation uses
the stdlib JSON-RPC framing so no language-server package is required.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import unquote, urlparse

__all__ = ["Diagnostic", "LspManager", "diagnostics", "get_diagnostics"]


def _safe_int(value: Any, default: int = 0) -> int:
    """Convert malformed LSP numeric fields to a safe integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Parse common configuration boolean spellings without raising."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


@dataclass
class Diagnostic:
    """One normalized language-server diagnostic."""

    file: str
    line: int = 1
    column: int = 1
    end_line: int = 1
    end_column: int = 1
    severity: str = "error"
    message: str = ""
    source: str = ""
    code: Any = None
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> str:
        """Return the diagnostic file path using the same name as file."""
        return self.file

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable diagnostic receipt."""
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "code": self.code,
            "data": dict(self.data),
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], file_hint: str = ""
    ) -> "Diagnostic":
        """Normalize one LSP diagnostic payload without trusting its shape."""
        data = dict(payload) if isinstance(payload, Mapping) else {}
        raw_range = data.get("range") if isinstance(data.get("range"), Mapping) else {}
        start = (
            raw_range.get("start")
            if isinstance(raw_range.get("start"), Mapping)
            else {}
        )
        end = raw_range.get("end") if isinstance(raw_range.get("end"), Mapping) else {}
        severity_code = data.get("severity", 1)
        try:
            severity = {
                1: "error",
                2: "warning",
                3: "information",
                4: "hint",
            }.get(severity_code, str(severity_code).lower())
        except TypeError:
            severity = str(severity_code).lower()
        raw_data = data.get("data")
        normalized_data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
        return cls(
            file=_uri_to_path(str(data.get("uri") or file_hint)),
            line=max(0, _safe_int(start.get("line", 0))),
            column=max(0, _safe_int(start.get("character", 0))),
            end_line=max(0, _safe_int(end.get("line", start.get("line", 0)))),
            end_column=max(
                0, _safe_int(end.get("character", start.get("character", 0)))
            ),
            severity=severity,
            message=str(data.get("message") or ""),
            source=str(data.get("source") or ""),
            code=data.get("code"),
            data=normalized_data,
        )


def _uri_to_path(value: str) -> str:
    """Convert a file URI or ordinary path into a normalized local path."""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if parsed.netloc:
            path = f"//{parsed.netloc}{path}"
        if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path.replace("\\", "/")
    return text.replace("\\", "/")


def _path_to_uri(value: str | Path) -> str:
    """Convert a local path to an LSP file URI."""
    try:
        return Path(value).resolve().as_uri()
    except (OSError, RuntimeError, ValueError):
        return str(value)


def _workspace_path(root: Optional[Path], value: str | Path) -> str:
    """Normalize a path and reject escapes and symlink components under root."""
    text = _uri_to_path(str(value)).strip()
    if not text or "\x00" in text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        if root is None:
            return ""
        candidate = root / candidate
    if root is None:
        return ""
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return ""
    except (OSError, RuntimeError, ValueError):
        return ""
    return resolved.as_posix()


def _safe_environment(extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Build a minimal child environment without forwarding credentials."""
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "HOME",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _command_args(command: str | Sequence[str]) -> List[str]:
    """Parse a command without invoking a shell."""
    if isinstance(command, str):
        if os.name == "nt":
            parts = shlex.split(command, posix=False)
            cleaned = []
            for part in parts:
                text = str(part)
                if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
                    text = text[1:-1]
                cleaned.append(text)
            return cleaned
        return shlex.split(command)
    return [str(part) for part in command]


class _ProcessTransport:
    """Small framed JSON-RPC transport for one child language server."""

    def __init__(
        self,
        command: str | Sequence[str],
        cwd: str | Path | None,
        timeout_s: float,
        env: Optional[Mapping[str, str]],
    ) -> None:
        self.command = _command_args(command)
        self.cwd = str(cwd) if cwd else None
        self.timeout_s = max(0.1, float(timeout_s))
        self.env = _safe_environment(env)
        self.process: Optional[subprocess.Popen[bytes]] = None
        self._responses: Dict[int, "queue.Queue[Dict[str, Any]]"] = {}
        self._notifications: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._response_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._write_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._closed = False

    def start(self) -> None:
        """Spawn the configured process or raise OSError."""
        if not self.command:
            raise FileNotFoundError("LSP command is empty")
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._reader = threading.Thread(
            target=self._read_loop,
            name="neo-lsp-reader",
            daemon=True,
        )
        self._reader.start()

    @staticmethod
    def _read_exact(stream: Any, length: int) -> bytes:
        """Read exactly one framed payload or fail on premature EOF."""
        chunks: List[bytes] = []
        remaining = max(0, int(length))
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise OSError("LSP transport closed while reading a frame")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        try:
            while not self._closed:
                headers: Dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    key, separator, value = line.partition(b":")
                    if separator:
                        headers[key.decode("ascii", "replace").strip().lower()] = (
                            value.decode("ascii", "replace").strip()
                        )
                try:
                    length = int(headers.get("content-length", "0"))
                except ValueError:
                    continue
                if length <= 0:
                    continue
                payload = self._read_exact(stream, length)
                try:
                    value = json.loads(payload.decode("utf-8", "replace"))
                except (TypeError, ValueError):
                    continue
                if not isinstance(value, dict):
                    continue
                if "id" in value and ("result" in value or "error" in value):
                    try:
                        response_id = int(value["id"])
                    except (TypeError, ValueError):
                        continue
                    with self._response_lock:
                        response = self._responses.get(response_id)
                    if response is not None:
                        response.put(value)
                elif "method" in value:
                    self._notifications.put(value)
                    if "id" in value:
                        self._send_raw(
                            {"jsonrpc": "2.0", "id": value["id"], "result": None}
                        )
        except (OSError, ValueError):
            return

    def _send_raw(self, value: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or self._closed:
            raise OSError("LSP transport is closed")
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        encoded = payload.encode("utf-8")
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        with self._write_lock:
            process.stdin.write(header + encoded)
            process.stdin.flush()

    def request(
        self, method: str, params: Optional[Mapping[str, Any]], timeout_s: float
    ) -> Any:
        """Send a request and wait for its matching response."""
        if self.process is None:
            raise OSError("LSP process is not started")
        request_id = next(self._ids)
        response_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
        with self._response_lock:
            self._responses[request_id] = response_queue
        try:
            self._send_raw(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": str(method),
                    "params": dict(params or {}),
                }
            )
            deadline = time.monotonic() + max(0.1, float(timeout_s))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"LSP request timed out: {method}")
                try:
                    response = response_queue.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError(f"LSP request timed out: {method}") from exc
                if response.get("error") is not None:
                    raise RuntimeError(str(response["error"]))
                return response.get("result")
        finally:
            with self._response_lock:
                self._responses.pop(request_id, None)

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        """Send a notification without waiting for a response."""
        self._send_raw(
            {
                "jsonrpc": "2.0",
                "method": str(method),
                "params": dict(params or {}),
            }
        )

    def pop_notifications(self) -> List[Dict[str, Any]]:
        """Drain notifications received since the previous call."""
        values: List[Dict[str, Any]] = []
        while True:
            try:
                values.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return values

    def close(self) -> None:
        """Close streams and terminate the child process."""
        self._closed = True
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class LspManager:
    """Lifecycle facade for an optional LSP server.

    ``command`` is never shell-interpreted. ``transport`` may be injected by
    tests or embedders. A server is considered available only after a valid
    initialize response; every other failure is represented by ``available``
    and ``last_error`` rather than an exception.
    """

    def __init__(
        self,
        command: Optional[str | Sequence[str]] = None,
        cwd: str | Path | None = None,
        timeout_s: float = 5.0,
        enabled: bool = True,
        transport: Any = None,
        root_uri: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        root_value = root_uri if root_uri is not None else cwd
        root_path: Optional[Path] = None
        if root_value is not None:
            try:
                candidate = Path(_uri_to_path(str(root_value))).expanduser()
                if candidate.is_dir():
                    root_path = candidate.resolve()
            except (OSError, RuntimeError, ValueError):
                root_path = None
        self._workspace_root = root_path
        try:
            self.timeout_s = max(0.1, float(timeout_s))
        except (TypeError, ValueError):
            self.timeout_s = 5.0
        self.enabled = _safe_bool(enabled, True)
        self.root_uri = root_uri
        self.env = env
        self.transport = transport
        self._owns_transport = transport is None
        self.available = False
        self.started = False
        self.last_error = ""
        self.capabilities: Dict[str, Any] = {}
        self._diagnostics: Dict[str, List[Diagnostic]] = {}
        self._open_documents: Dict[str, str] = {}
        self._document_versions: Dict[str, int] = {}
        self._notification_offset = 0
        self._lock = threading.RLock()

    @property
    def status(self) -> Dict[str, Any]:
        """Return a serializable lifecycle status receipt."""
        return {
            "enabled": self.enabled,
            "available": self.available,
            "started": self.started,
            "error": self.last_error,
            "open_documents": sorted(self._open_documents),
        }

    def start(self) -> bool:
        """Start and initialize the server, returning whether it is usable."""
        with self._lock:
            if not self.enabled:
                self.last_error = "disabled"
                return False
            if self.available:
                return True
            if self.started and self.last_error:
                return False
            self.started = True
            try:
                if self.transport is None:
                    if not self.command:
                        self.last_error = "no LSP command configured"
                        self.started = False
                        return False
                    self.transport = _ProcessTransport(
                        self.command,
                        self.cwd,
                        self.timeout_s,
                        self.env,
                    )
                    self._owns_transport = True
                starter = getattr(self.transport, "start", None)
                if callable(starter):
                    starter()
                root = self.root_uri
                if root is None and self.cwd:
                    root = _path_to_uri(self.cwd)
                result = self.transport.request(
                    "initialize",
                    {
                        "processId": os.getpid(),
                        "rootUri": root,
                        "capabilities": {
                            "textDocument": {
                                "hover": {"contentFormat": ["plaintext", "markdown"]},
                                "documentSymbol": {},
                                "references": {},
                                "publishDiagnostics": {},
                            }
                        },
                    },
                    self.timeout_s,
                )
                if not isinstance(result, Mapping):
                    raise ValueError("LSP initialize returned a non-object result")
                self.capabilities = dict(result.get("capabilities") or {})
                self.transport.notify("initialized", {})
                self.available = True
                self.last_error = ""
                return True
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.available = False
                self.close()
                if self._owns_transport:
                    self.transport = None
                return False

    def _request(
        self,
        method: str,
        params: Optional[Mapping[str, Any]],
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Issue a request after ensuring the lifecycle is started."""
        if not self.start():
            return None
        try:
            return self.transport.request(
                method,
                dict(params or {}),
                self.timeout_s if timeout_s is None else max(0.1, float(timeout_s)),
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def _notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> bool:
        """Send a notification after ensuring the lifecycle is started."""
        if not self.start():
            return False
        notifier = getattr(self.transport, "notify", None)
        if not callable(notifier):
            self.last_error = "LSP transport cannot send notifications"
            return False
        try:
            notifier(method, dict(params or {}))
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def _normalize_path(self, value: str | Path) -> str:
        """Normalize a document path for matching and citations."""
        return _workspace_path(self._workspace_root, value)

    def open_document(
        self,
        path: str | Path,
        text: Optional[str] = None,
        language_id: Optional[str] = None,
        version: int = 1,
    ) -> bool:
        """Open a contained document with didOpen, returning success."""
        if not self.start():
            return False
        normalized = self._normalize_path(path)
        if not normalized:
            self.last_error = "document path outside workspace"
            return False
        try:
            content = (
                str(text)
                if text is not None
                else Path(normalized).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            )
        except (OSError, TypeError, ValueError):
            self.last_error = "document unreadable"
            return False
        language = language_id or self._language_for(normalized)
        uri = _path_to_uri(normalized)
        try:
            document_version = int(version)
        except (TypeError, ValueError):
            document_version = 1
        sent = self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language,
                    "version": document_version,
                    "text": content,
                }
            },
        )
        if not sent:
            return False
        self._open_documents[normalized] = uri
        self._document_versions[normalized] = document_version
        return True

    def update_document(
        self,
        path: str | Path,
        text: Optional[str] = None,
        version: Optional[int] = None,
    ) -> bool:
        """Send a full-document change for an already-open workspace file."""
        if not self.start():
            return False
        normalized = self._normalize_path(path)
        if not normalized or normalized not in self._open_documents:
            self.last_error = "document is not open in this workspace"
            return False
        try:
            content = (
                str(text)
                if text is not None
                else Path(normalized).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            )
        except (OSError, TypeError, ValueError):
            self.last_error = "document unreadable"
            return False
        current_version = self._document_versions.get(normalized, 1)
        try:
            next_version = int(version) if version is not None else current_version + 1
        except (TypeError, ValueError):
            next_version = current_version + 1
        if next_version <= current_version:
            next_version = current_version + 1
        sent = self._notify(
            "textDocument/didChange",
            {
                "textDocument": {
                    "uri": self._open_documents[normalized],
                    "version": next_version,
                },
                "contentChanges": [{"text": content}],
            },
        )
        if sent:
            self._document_versions[normalized] = next_version
        return sent

    def sync_document(
        self,
        path: str | Path,
        text: Optional[str] = None,
        version: Optional[int] = None,
    ) -> bool:
        """Open a contained document, or update it when it is already open.

        A caller that pushes a file after every edit should not have to know
        which notification the document currently needs. The first call for a
        path sends ``didOpen``; every later call sends ``didChange``. Returns
        whether the document is open in the server afterwards, so a caller can
        treat ``False`` as "the language-server boundary is unavailable" rather
        than as a per-file error.
        """
        normalized = self._normalize_path(path)
        if not normalized:
            self.last_error = "document path outside workspace"
            return False
        if normalized in self._open_documents:
            return self.update_document(normalized, text=text, version=version)
        return self.open_document(normalized, text=text, version=version or 1)

    def close_document(self, path: str | Path) -> bool:
        """Close a previously opened document."""
        normalized = self._normalize_path(path)
        if not normalized:
            self.last_error = "document path outside workspace"
            return False
        uri = self._open_documents.pop(normalized, _path_to_uri(normalized))
        self._document_versions.pop(normalized, None)
        return self._notify(
            "textDocument/didClose",
            {"textDocument": {"uri": uri}},
        )

    @staticmethod
    def _language_for(path: str) -> str:
        """Choose a basic LSP language id from a source suffix."""
        suffix = Path(path).suffix.lower()
        return {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
        }.get(suffix, "plaintext")

    def _drain_notifications(self) -> None:
        """Record publishDiagnostics notifications from the transport."""
        popper = getattr(self.transport, "pop_notifications", None)
        if callable(popper):
            try:
                values = popper()
            except Exception:
                values = []
        else:
            raw_values = list(getattr(self.transport, "notifications", []) or [])
            values = raw_values[self._notification_offset :]
            self._notification_offset = len(raw_values)
        for value in values:
            if (
                not isinstance(value, Mapping)
                or value.get("method") != "textDocument/publishDiagnostics"
            ):
                continue
            params = value.get("params")
            if not isinstance(params, Mapping):
                continue
            file_path = self._normalize_path(str(params.get("uri") or ""))
            if not file_path:
                continue
            raw_items = params.get("diagnostics")
            if not isinstance(raw_items, list):
                raw_items = []
            self._diagnostics[file_path] = [
                Diagnostic.from_payload(item, file_path)
                for item in raw_items
                if isinstance(item, Mapping)
            ]

    def get_diagnostics(
        self,
        path: str | Path | None = None,
        timeout_s: Optional[float] = None,
    ) -> List[Diagnostic]:
        """Return diagnostics, using pull diagnostics when supported."""
        if not self.start():
            return []
        self._drain_notifications()
        requested = self._normalize_path(path) if path else ""
        if path and not requested:
            self.last_error = "document path outside workspace"
            return []
        result: Any = None
        if requested:
            result = self._request(
                "textDocument/diagnostic",
                {"textDocument": {"uri": _path_to_uri(requested)}},
                timeout_s=timeout_s,
            )
            self._drain_notifications()
        if result is None and requested:
            return sorted(
                self._diagnostics.get(requested, []),
                key=lambda item: (item.file, item.line, item.column),
            )
        explicit_empty = False
        if isinstance(result, Mapping):
            raw_items = result.get("items")
            if not isinstance(raw_items, list):
                raw_items = (
                    result.get("diagnostics")
                    if isinstance(result.get("diagnostics"), list)
                    else []
                )
            explicit_empty = isinstance(raw_items, list) and not raw_items
        else:
            raw_items = result if isinstance(result, list) else []
            explicit_empty = isinstance(result, list) and not raw_items
        values = [
            Diagnostic.from_payload(item, requested)
            for item in raw_items
            if isinstance(item, Mapping)
        ]
        if requested and (raw_items or explicit_empty):
            self._diagnostics[requested] = values
        if requested and (raw_items or explicit_empty):
            return sorted(values, key=lambda item: (item.file, item.line, item.column))
        if requested:
            return sorted(
                self._diagnostics.get(requested, []),
                key=lambda item: (item.file, item.line, item.column),
            )
        self._drain_notifications()
        values = [item for group in self._diagnostics.values() for item in group]
        return sorted(values, key=lambda item: (item.file, item.line, item.column))

    def hover(
        self,
        path: str | Path,
        line: int,
        column: int,
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Return hover content, or None when unavailable."""
        normalized = self._normalize_path(path)
        if not normalized:
            self.last_error = "document path outside workspace"
            return None
        return self._request(
            "textDocument/hover",
            {
                "textDocument": {"uri": _path_to_uri(normalized)},
                "position": {
                    "line": max(0, _safe_int(line)),
                    "character": max(0, _safe_int(column)),
                },
            },
            timeout_s=timeout_s,
        )

    def symbols(
        self,
        path: Optional[str | Path] = None,
        query: str = "",
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Return document or workspace symbols, or an empty list."""
        if path:
            normalized = self._normalize_path(path)
            if not normalized:
                self.last_error = "document path outside workspace"
                return []
            result = self._request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": _path_to_uri(normalized)}},
                timeout_s=timeout_s,
            )
        else:
            result = self._request(
                "workspace/symbol",
                {"query": str(query or "")},
                timeout_s=timeout_s,
            )
        if result is None:
            return []
        return result

    def references(
        self,
        path: str | Path,
        line: int,
        column: int,
        include_declaration: bool = True,
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Return reference locations, or an empty list when unavailable."""
        normalized = self._normalize_path(path)
        if not normalized:
            self.last_error = "document path outside workspace"
            return []
        result = self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": _path_to_uri(normalized)},
                "position": {
                    "line": max(0, _safe_int(line)),
                    "character": max(0, _safe_int(column)),
                },
                "context": {"includeDeclaration": bool(include_declaration)},
            },
            timeout_s=timeout_s,
        )
        return [] if result is None else result

    def get_hover(
        self,
        path: str | Path,
        line: int,
        column: int,
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Alias for :meth:`hover`."""
        return self.hover(path, line, column, timeout_s=timeout_s)

    def document_symbols(
        self,
        path: str | Path,
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Alias for document-scoped :meth:`symbols`."""
        return self.symbols(path, timeout_s=timeout_s)

    def find_references(
        self,
        path: str | Path,
        line: int,
        column: int,
        include_declaration: bool = True,
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Alias for :meth:`references`."""
        return self.references(
            path,
            line,
            column,
            include_declaration=include_declaration,
            timeout_s=timeout_s,
        )

    def shutdown(self) -> bool:
        """Send shutdown/exit and close the transport without raising."""
        if not self.started:
            return False
        try:
            if self.available and self.transport is not None:
                try:
                    self.transport.request("shutdown", {}, self.timeout_s)
                    self.transport.notify("exit", {})
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self.close()
        return not self.available

    def close(self) -> None:
        """Close the transport and mark the manager unavailable."""
        with self._lock:
            if self.transport is not None:
                closer = getattr(self.transport, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
            self.available = False
            self.started = False
            self._diagnostics.clear()
            self._notification_offset = 0
            self._open_documents.clear()
            self._document_versions.clear()
            if self._owns_transport:
                self.transport = None

    def __enter__(self) -> "LspManager":
        """Start the manager for a context-managed request."""
        self.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        """Shut down the manager at context exit."""
        self.shutdown()

    @classmethod
    def from_config(
        cls,
        config: Optional[Mapping[str, Any]],
        repo_path: str | Path | None = None,
    ) -> "LspManager":
        """Construct a manager from optional task configuration."""
        values = dict(config or {})
        command = values.get("lsp_command") or values.get("command")
        try:
            timeout = float(values.get("lsp_timeout_s", values.get("timeout_s", 5.0)))
        except (TypeError, ValueError):
            timeout = 5.0
        return cls(
            command=command,
            cwd=repo_path,
            timeout_s=timeout,
            enabled=_safe_bool(values.get("lsp_enabled", bool(command)), bool(command)),
            root_uri=_path_to_uri(repo_path) if repo_path else None,
        )


def get_diagnostics(
    source: Any = None,
    path: str | Path | None = None,
    timeout_s: Optional[float] = None,
) -> List[Diagnostic]:
    """Return diagnostics from a manager or an honest empty list."""
    if source is None:
        return []
    method = getattr(source, "get_diagnostics", None)
    if not callable(method):
        return []
    try:
        values = method(path, timeout_s=timeout_s)
    except TypeError:
        try:
            values = method(path)
        except Exception:
            return []
    except Exception:
        return []
    normalized: List[Diagnostic] = []
    for item in values or []:
        if isinstance(item, Diagnostic):
            normalized.append(item)
        elif isinstance(item, Mapping):
            normalized.append(Diagnostic.from_payload(item))
    return normalized


def diagnostics(source: Any = None, path: str | Path | None = None) -> List[Diagnostic]:
    """Short alias for the optional diagnostic query."""
    return get_diagnostics(source, path)
