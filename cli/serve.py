"""``neo serve`` and ``neo acp``: the two integration entry points.

Both commands are thin, honest adapters over modules that already exist
and are already tested elsewhere:

- ``neo serve`` drives :class:`agent_sdk.server.AgentServer` - the
  loopback-default HTTP/SSE/WebSocket agent protocol.
- ``neo acp`` drives :class:`acp.server.ACPServer` over stdio, bound to a
  public :class:`agent_sdk.Agent`. Editors speak ACP v1; this is the
  process they launch.

Three things this module owns, because they are product truth rather than
protocol truth:

**Loopback by default, and a real warning when you leave it.** Both servers
refuse a non-loopback bind unless explicitly allowed. ``bind_warning``
repeats that refusal in the operator's own words at the moment it
happens, and says what is exposed - this is the difference between a
default that is safe and a default that is merely lucky.

**Exact editor configuration, once.** ``editor_configuration`` returns the
literal configuration an editor needs, with the real interpreter path
resolved, and ``first_run_notice`` records that it was shown so the second
``neo acp`` is silent. An editor integration that prints the wrong
snippet is worse than one that prints nothing.

**Model resolution is the CLI's, not the SDK's.** ``resolve_model`` returns
the effective model plus where it came from, and refuses to start a server
that would answer every request with an auth error while looking healthy.
The refusal names the fix (``neo login``); it never invents a model.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ProcessStdioTransport",
    "ServePlan",
    "acp_editor_config",
    "acp_stdio_serve",
    "bind_warning",
    "build_serve_plan",
    "editor_configuration",
    "first_run_notice",
    "is_loopback",
    "resolve_model",
    "serve_once",
]

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "localhost.localdomain"})

#: Marker file under the config root recording that the ACP editor snippet
#: has been shown once. Presence of the file is the only state.
ACP_NOTICE_FILENAME = "acp-first-run.json"


def _config_root() -> Path:
    """The Neo-owned config root (never the repository).

    ``NEO_HOME``/``HARNESS_HOME`` win outright, so the first-run marker is
    isolatable and never lands in a user's real config directory during a
    test or a sandboxed run. Only when neither is set does this fall back to
    the platform config path.
    """
    for name in ("NEO_HOME", "HARNESS_HOME"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return Path(value).expanduser()
    try:
        from cli.neoconfig import global_settings_path

        return Path(global_settings_path()).parent
    except Exception:
        home = Path(os.environ.get("NEO_HOME") or Path.home() / ".neo")
        return home


def is_loopback(host: str) -> bool:
    """Whether a bind host is a loopback address.

    Used for the warning text only; the authoritative refusal lives in
    ``agent_sdk`` / ``acp``, which are the components that actually bind.
    """
    value = str(host or "").strip().lower()
    if value in _LOOPBACK_HOSTS:
        return True
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    return value.startswith("127.") or value == "::1"


def bind_warning(host: str, protocol: str) -> str:
    """The operator-facing warning for a non-loopback bind.

    Names the exposure explicitly. Returns "" for a loopback bind, so the
    common case stays quiet.
    """
    if is_loopback(host):
        return ""
    return (
        f"WARNING: binding {protocol} to {host} exposes the agent to every "
        "network interface this machine can reach. Other hosts can send it "
        "prompts, read its repository, and spend its provider budget. Use "
        "127.0.0.1 unless you understand the exposure."
    )


def resolve_model(
    repo: Optional[Path] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Resolve the effective model for a server and report its provenance.

    Returns ``(model, info)``. ``model`` is a string when a model is
    configured and ``None`` when it is not, so the caller can refuse rather
    than start a server that will fail every request.

    ``info`` always carries ``model``, ``source`` (which tier supplied it),
    and ``reason`` when nothing is configured. Precedence is the CLI's
    existing one (env > project-local > project > global), resolved through
    ``cli.neoconfig`` rather than reimplemented.
    """
    info: Dict[str, Any] = {"model": "", "source": "", "reason": ""}
    model = str(os.environ.get("NEO_MODEL") or "").strip()
    if model:
        info["source"] = "env:NEO_MODEL"
    if not model:
        try:
            from cli.neoconfig import merged_settings

            effective = merged_settings(start=str(repo) if repo else None)
            if isinstance(effective, Mapping):
                model = str(effective.get("model") or "").strip()
                if model:
                    info["source"] = "settings"
        except Exception:
            model = ""
    if not model:
        info["reason"] = "no model configured; run `neo login` first"
        return None, info
    info["model"] = model
    return model, info


def _server_config(repo: Optional[Path]) -> Dict[str, Any]:
    """Non-secret server configuration merged from the settings chain."""
    config: Dict[str, Any] = {}
    try:
        from cli.neoconfig import merged_settings

        effective = merged_settings(start=str(repo) if repo else None)
    except Exception:
        return config
    if not isinstance(effective, Mapping):
        return config
    for key in (
        "agent_approval",
        "steering_enabled",
        "max_retries",
        "budget_cap_usd",
        "stream_enabled",
    ):
        if key in effective and effective[key] is not None:
            value = effective[key]
            # Never carry a credential into a long-lived server process.
            if key in ("api_key", "api_base", "base_url") or "key" in key:
                continue
            config[key] = value
    return config


def acp_editor_config(
    *,
    repo: Optional[Path] = None,
    editor: str = "zed",
    command: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Return the EXACT editor configuration for the ACP entry point.

    The command is the real resolved argv a user can paste, using the
    interpreter that is actually running Neo (a venv's python, not
    whatever ``python`` resolves to later). Returns a mapping so the
    renderer and the ``--print-config`` output cannot disagree.

    ``zed`` uses Zed's documented ``context_servers`` ACP shape. Other
    editors get the same argv under a ``command``/``args`` pair plus a
    ``note`` saying where the editor expects it, rather than a guessed file
    path that would be wrong.
    """
    argv = list(command) if command else [sys.executable, "-m", "cli", "acp"]
    if repo is not None:
        argv = [*argv, "--repo", str(repo)]
    name = str(editor or "zed").strip().lower()
    if name == "zed":
        return {
            "editor": "zed",
            "path": ".zed/settings.json",
            "config": {
                "context_servers": {
                    "neo": {
                        "command": {"path": argv[0], "args": argv[1:]},
                    }
                }
            },
        }
    return {
        "editor": name,
        "path": "",
        "config": {"command": {"path": argv[0], "args": argv[1:]}},
        "note": (
            f"{name} has no Neo-specific ACP snippet of record; run "
            "`neo acp --print-config` and wire the argv above into your "
            "editor's agent/ACP command setting."
        ),
    }


def _notice_path() -> Path:
    return _config_root() / ACP_NOTICE_FILENAME


def editor_configuration(
    *,
    repo: Optional[Path] = None,
    editor: str = "zed",
) -> str:
    """Render the editor configuration as copy-pasteable text.

    Separate from :func:`first_run_notice` because the notice is a
    first-run EVENT and this is a value: ``--print-config`` prints this on
    every invocation, and both read the same :func:`acp_editor_config`, so
    the two renderings cannot disagree.
    """
    payload = acp_editor_config(repo=repo, editor=editor)
    lines = [json.dumps(payload["config"], indent=2)]
    if payload.get("path"):
        lines.append("")
        lines.append(f"# paste into {payload['path']}")
    if payload.get("note"):
        lines.append(f"# {payload['note']}")
    return "\n".join(lines)


def first_run_notice(
    *,
    repo: Optional[Path] = None,
    editor: str = "zed",
    mark_shown: bool = True,
) -> Optional[str]:
    """Return the exact editor configuration ONCE, then None.

    "First run" is a real fact on disk, not a heuristic: a marker file
    under the Neo config root. ``mark_shown=False`` reads the state without
    writing it, which is what the test and ``--dry-run`` use.
    """
    marker = _notice_path()
    if marker.is_file():
        return None
    payload = acp_editor_config(repo=repo, editor=editor)
    lines = [
        "First ACP run: here is the exact editor configuration.",
        "",
        json.dumps(payload["config"], indent=2),
        "",
    ]
    if payload.get("path"):
        lines.append(f"Paste it into: {payload['path']}")
    if payload.get("note"):
        lines.append(str(payload["note"]))
    lines.append("This is printed once. `neo acp --print-config` prints it again.")
    text = "\n".join(lines)
    if mark_shown:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "shown_at": time.time(),
                        "editor": payload["editor"],
                        "path": payload["path"],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
    return text


class ProcessStdioTransport:
    """Server-side ACP stdio: this process's own stdin/stdout.

    ``acp.transport.StdioACPTransport`` is the CLIENT side - it spawns a
    child and talks to its pipes. An editor launches ``neo acp`` and speaks
    to the process's inherited streams, so the server needs the mirror
    image, which did not exist. This is that transport, and it is
    deliberately small and explicit:

    - a daemon reader thread does the blocking ``readline`` so the asyncio
      loop is never parked on stdin (an editor that sends nothing must not
      freeze the protocol);
    - frames are strict UTF-8 and bounded, matching the client transport's
      limits, so a malformed or oversized line is refused rather than
      desynchronizing the stream;
    - ``asend`` writes ONE compact JSON line and flushes, and holds a lock so
      a notification and a response cannot interleave mid-line;
    - EOF closes the queue and the server, which is how an editor
      disconnecting terminates ``neo acp`` cleanly.

    The hand-off between the reader thread and the loop is a plain
    ``queue.Queue``, not ``call_soon_threadsafe``. That is deliberate:
    ``acp._compat.call_maybe_async`` runs a SYNCHRONOUS transport ``start()``
    in ``asyncio.to_thread``, so ``start()`` is not guaranteed to run on the
    loop's thread and cannot capture a running loop. The first version tried,
    and every handshake hung with no frames and no error. A polled queue has
    no such requirement and matches the client transport's own design.
    """

    def __init__(
        self,
        *,
        stdin: Any = None,
        stdout: Any = None,
        max_frame_bytes: int = 1 << 20,
    ) -> None:
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.max_frame_bytes = int(max_frame_bytes)
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self.sent: List[str] = []
        self.frames: List[str] = []
        self.read_error = ""
        self.eof = False

    @property
    def closed(self) -> bool:
        """Whether the peer has gone away (EOF) or we have been closed.

        ``acp.server._reader_loop`` uses exactly this to decide that a
        ``None`` receive is an END OF STREAM rather than an idle poll. A
        transport that never reports it leaves the server running forever
        after its editor disconnects, which is the failure an editor
        restart then looks like.
        """
        return self._closed or self.eof

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Start the reader thread once. Safe to call from any thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._read_loop, name="neo-acp-stdin", daemon=True
        )
        self._thread.start()

    def _read_loop(self) -> None:
        stream = self.stdin
        buffer = getattr(stream, "buffer", None)
        try:
            while not self._closed:
                if buffer is not None:
                    raw = buffer.readline(self.max_frame_bytes + 2)
                else:
                    raw = stream.readline()
                if not raw:
                    self.eof = True
                    break
                if isinstance(raw, str):
                    text = raw
                else:
                    if len(raw) > self.max_frame_bytes + 1:
                        self._queue.put(ValueError("ACP frame too large"))
                        continue
                    try:
                        text = bytes(raw).decode("utf-8")
                    except UnicodeDecodeError:
                        self._queue.put(ValueError("ACP frame is not valid UTF-8"))
                        continue
                text = text.strip()
                if not text:
                    continue
                self.frames.append(text)
                self._queue.put(text)
        except Exception as exc:
            # A dead reader must not leave the editor waiting forever: the
            # reason is recorded and EOF is offered, which closes the server.
            self.read_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._queue.put(None)

    # -- protocol ----------------------------------------------------------
    async def areceive(self, timeout: Optional[float] = None) -> Any:
        """Return the next raw frame, or None on idle timeout / EOF.

        A bounded wait is the contract the ACP server already relies on:
        its reader treats a None return as an idle poll, not as EOF, and it
        is the ONLY thing that keeps a silent editor from freezing the
        process.
        """
        try:
            return self._queue.get(timeout=max(0.001, float(timeout or 0.1)))
        except queue.Empty:
            return None

    async def asend(self, message: Mapping[str, Any]) -> None:
        """Write one compact JSON line to stdout.

        Text, not bytes: the process's stdout is whatever the editor gave
        us, and a text stream rejects bytes.
        """
        payload = json.dumps(dict(message), ensure_ascii=False, allow_nan=False)
        with self._write_lock:
            self.stdout.write(payload + "\n")
            self.stdout.flush()
            self.sent.append(payload)

    async def aclose(self) -> None:
        """Close the transport and stop the reader thread."""
        self._closed = True
        self._queue.put(None)

    def close(self) -> None:
        """Synchronous close for embedding callers."""
        self._closed = True
        self._queue.put(None)


@dataclass
class ServePlan:
    """The resolved plan for ``neo serve``, before anything binds."""

    host: str
    port: int
    token: str
    auth_required: bool
    warning: str
    model: str
    model_source: str
    config: Dict[str, Any]
    repo: str
    log_root: str

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable view (never carries the token)."""
        return {
            "host": self.host,
            "port": self.port,
            "auth_required": self.auth_required,
            "warning": self.warning,
            "model": self.model,
            "model_source": self.model_source,
            "repo": self.repo,
            "log_root": self.log_root,
            "config": dict(self.config),
        }


def build_serve_plan(
    *,
    repo: Optional[Path] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
    allow_non_loopback: bool = False,
    log_root: Optional[Path] = None,
) -> ServePlan:
    """Resolve every ``neo serve`` input before the server binds.

    Refuses a non-loopback host without ``--allow-non-loopback`` (exit-code
    decision belongs to the caller), resolves the model, and decides whether
    auth is required. A server with no token on a non-loopback bind is
    refused rather than started: "loopback plus no auth" is the only
    unauthenticated configuration that is defensible.
    """
    model, info = resolve_model(repo)
    if not model:
        model = ""
    resolved_token = str(token or os.environ.get("NEO_SERVER_TOKEN") or "")
    loopback = is_loopback(host)
    if not loopback and not allow_non_loopback:
        raise PermissionError(
            f"refusing to bind {host}: AgentServer is loopback by default. "
            "Pass --allow-non-loopback only if you understand the exposure."
        )
    if not loopback and not resolved_token:
        raise PermissionError(
            f"refusing an unauthenticated bind to {host}: pass --auth-token "
            "(or set NEO_SERVER_TOKEN)."
        )
    effective_log_root = str(log_root) if log_root else ""
    if not effective_log_root:
        try:
            from cli.headless import _default_log_root

            effective_log_root = str(_default_log_root())
        except Exception:
            effective_log_root = ""
    return ServePlan(
        host=str(host),
        port=int(port),
        token=resolved_token,
        auth_required=bool(resolved_token),
        warning=bind_warning(host, "the agent server"),
        model=str(info.get("model") or ""),
        model_source=str(info.get("source") or ""),
        config=_server_config(repo),
        repo=str(repo or Path.cwd()),
        log_root=effective_log_root,
    )


def serve_once(
    plan: ServePlan,
    *,
    call_fn: Any = None,
    on_ready: Any = None,
) -> Dict[str, Any]:
    """Start the agent server and return its live endpoints.

    ``call_fn`` is the injectable model boundary (tests pass a deterministic
    callable; a real run leaves it None so the provider gateway resolves the
    configured model). ``on_ready`` receives the plan's dict as soon as the
    socket is bound, which is how a caller prints the real port when the
    request was ``--port 0``.

    Blocks until the server stops.
    """
    from agent_sdk.server import AgentServer

    options: Dict[str, Any] = {
        "log_root": plan.log_root,
        "config": plan.config,
        "token": plan.token,
        "allow_non_loopback": not is_loopback(plan.host),
    }
    if call_fn is not None:
        options["call_fn"] = call_fn
    elif plan.model:
        options["model"] = plan.model
    server = AgentServer(plan.repo, host=plan.host, port=plan.port, **options)
    server.start()
    host, port = server.address
    try:
        advertised_version = server.version()
    except Exception:
        advertised_version = ""
    endpoints = {
        "url": server.url,
        "host": host,
        "port": port,
        "capabilities": server.capabilities,
        "auth_required": plan.auth_required,
        "version": advertised_version,
    }
    if on_ready is not None:
        try:
            on_ready(endpoints)
        except Exception:
            pass
    try:
        server.serve_forever()
    finally:
        try:
            server.stop()
        except Exception:
            pass
    return endpoints


def acp_stdio_serve(
    *,
    repo: Optional[Path] = None,
    call_fn: Any = None,
    log_root: Optional[Path] = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    on_ready: Any = None,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Serve ACP v1 on stdio for an editor, and block until it disconnects.

    The public agent is ``agent_sdk.Agent`` over a ``LocalTransport``, which
    is exactly the connection the ACP handoff asked for. Stdout carries
    protocol frames ONLY: the first-run notice and every warning go to
    stderr, because one stray byte on stdout desynchronizes a JSON-RPC
    stream and the editor reports a protocol error with no cause.

    Returns a process exit code from the shared contract.
    """

    from acp.server import ACPServer
    from agent_sdk import Agent

    repo_dir = Path(repo) if repo else Path.cwd()
    model, info = resolve_model(repo_dir)
    if model is None and call_fn is None:
        print(
            f"error: {info.get('reason') or 'no model configured'}",
            file=sys.stderr,
        )
        return 4
    options: Dict[str, Any] = {"config": _server_config(repo_dir)}
    if log_root:
        options["log_root"] = str(log_root)
    if call_fn is not None:
        options["call_fn"] = call_fn
    else:
        options["model"] = model
    # `Agent` owns its LocalTransport; constructing one here and handing it
    # over would fight the facade's own positional/keyword contract.
    agent = Agent(str(repo_dir), **options)
    server = ACPServer(agent, capabilities=capabilities)

    async def _run() -> None:
        stdio = ProcessStdioTransport(
            stdin=stdin if stdin is not None else sys.stdin,
            stdout=stdout if stdout is not None else sys.stdout,
        )
        server.transport = stdio
        if on_ready is not None:
            try:
                on_ready(
                    {"protocol": "acp", "transport": "stdio", "repo": str(repo_dir)}
                )
            except Exception:
                pass
        try:
            await server.serve_forever()
        finally:
            try:
                await stdio.aclose()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            agent.close()
        except Exception:
            pass
    return 0
