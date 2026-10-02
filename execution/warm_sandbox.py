"""Task-scoped warm sandbox: one long-lived container per (repository, task).

Why
---
``execution.sandbox.execute_sandboxed`` starts a FRESH container per call. That
is the correct default and it stays the default: it is the isolation boundary
the verifier mints success behind, and a task whose commands are hostile must
never share a filesystem namespace with its own next command. But it is also
the single largest per-command latency cost in the loop — a container start is
hundreds of milliseconds, and an agent turn that runs four reads pays it four
times for four commands that were never going to interfere with each other.

This module adds the other side of the trade without weakening the first:

* **Identity is the reuse key, and it is checked.** A warm container is bound
  to the canonical repository path AND the task id. Starting a second warm
  container with a different identity while one is live is REFUSED
  (``IdentityMismatch``), not silently satisfied by the existing one. Files
  written in task A's container therefore cannot be seen by task B's.
* **The verification boundary is explicit and refused here.** The final
  verifier keeps its own fresh container per command through
  ``execute_sandboxed``; constructing a warm sandbox with
  ``purpose="verification"`` raises rather than quietly weakening the gate
  that mints verified success.
* **Per-command isolation remains available for hostile tasks.** ``hostile=True``
  (or ``reuse=False``) makes every command run through ``execute_sandboxed``,
  so a task can opt out of sharing without changing its call sites.
* **Teardown is the caller's job and is loud.** ``release()`` stops and removes
  the container; leaving one behind is visible to the existing
  ``reap_orphaned_containers`` sweep (the warm name embeds this process's PID
  and environment token, so a hard-killed owner's container is reaped by peers
  exactly like a per-command one).

Container naming reuses ``execution.sandbox``'s ``hexec-e<env>-p<pid>-`` prefix
and adds a ``w`` marker, so every existing residue/reaping assertion keeps
working unchanged.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from shared.types import ExecutionResult

from . import sandbox as _sandbox
from .ingress import REDACTION_UNAVAILABLE
from .ingress import seal_streams as _seal_streams
from .sandbox import (
    DEFAULT_CPU_LIMIT,
    DEFAULT_MEM_LIMIT,
    DEFAULT_PIDS_LIMIT,
    DEFAULT_TIMEOUT_S,
    MOUNT_POINT,
    TIMEOUT_EXIT_CODE,
    SandboxUnavailableError,
    _container_name_prefix,
    _docker_environment,
    _docker_run_args,
    _scrub_container_env,
    _truncate,
    assert_sandbox_argv_isolated,
    declared_egress_allowlist,
    docker_available,
    ensure_image,
    pin_image,
)

__all__ = [
    "IdentityMismatch",
    "SandboxIdentity",
    "WarmSandboxError",
    "WarmTaskSandbox",
    "live_warm_identity",
    "warm_sandbox_available",
]


def _sealed(result: ExecutionResult) -> ExecutionResult:
    """Pass one warm-container result through the subprocess-output ingress.

    THE SUBPROCESS-OUTPUT INGRESS for the warm-container path. It delegates to
    `execution.ingress` rather than re-calling the redactor, so the warm path
    and the cold path share ONE cap and ONE redaction policy — a second copy
    here would be exactly the divergence gap G34 named.

    This module has no production call site today, which is precisely why it
    is the one that quietly grows a second, un-fenced egress: nothing
    exercises it, so a reviewer reading only the warm path would not know a
    boundary existed. `execution/AGENTS.md` records that as the reason the
    wiring is still a handoff rather than a claim.
    """
    try:
        sealed, _reports = _seal_streams(result)
    except Exception:  # pragma: no cover - seal_streams is total by construction
        return ExecutionResult(
            result.exit_code,
            REDACTION_UNAVAILABLE,
            REDACTION_UNAVAILABLE,
            result.timed_out,
        )
    return sealed


#: The keep-alive command. A shell loop rather than `sleep infinity` because
#: GNU coreutils' `infinity` argument is not present on every slim base image.
_IDLE_COMMAND = "while true; do sleep 3600; done"

#: The only purpose a warm sandbox may serve. The final verification boundary
#: is deliberately absent from this set.
AGENT_STEP_PURPOSE = "agent_step"
ALLOWED_PURPOSES = frozenset({AGENT_STEP_PURPOSE})


class WarmSandboxError(RuntimeError):
    """Raised when a warm task sandbox cannot be created or used."""


class IdentityMismatch(WarmSandboxError):
    """Raised when reuse would cross a repository or task identity boundary."""


@dataclass(frozen=True)
class SandboxIdentity:
    """The identity a warm container is bound to. Reuse requires equality."""

    repo_path: str
    task_id: str
    image: str
    purpose: str = AGENT_STEP_PURPOSE

    @property
    def key(self) -> str:
        """Return the comparable identity key."""
        return "|".join([self.repo_path, self.task_id, self.image, self.purpose])

    def describe(self) -> str:
        """Return a short, secret-free description for refusal messages."""
        return f"task={self.task_id or '(none)'} repo={self.repo_path}"


#: The one live warm container per process, if any. A module-level singleton is
#: deliberate: it is what makes a cross-identity reuse ATTEMPT visible and
#: refusable instead of quietly succeeding.
_LIVE: Dict[str, Any] = {"identity": None, "sandbox": None}
_LIVE_LOCK = threading.RLock()


def live_warm_identity() -> Optional[SandboxIdentity]:
    """Return the identity of this process's live warm sandbox, if any."""
    with _LIVE_LOCK:
        identity = _LIVE.get("identity")
        return identity if isinstance(identity, SandboxIdentity) else None


def warm_sandbox_available() -> bool:
    """Return whether warm reuse can be used at all (docker is reachable)."""
    try:
        return bool(docker_available())
    except Exception:
        return False


class WarmTaskSandbox:
    """One long-lived sandboxed container for a single (repository, task).

    Assumes ``repo_path`` is a directory Docker can bind-mount and that the
    caller is an agent STEP loop, never the final verifier. Use it as a context
    manager so the container cannot outlive the step loop by accident::

        with WarmTaskSandbox(repo, task_id) as box:
            result = box.run("cat src/a.py")

    ``run`` returns the same ``ExecutionResult`` shape as
    ``execution.sandbox.execute_sandboxed`` (exit 124 + ``timed_out`` on
    timeout, exit 130 on cancellation), so callers do not branch on the mode.
    """

    def __init__(
        self,
        repo_path: str,
        task_id: str,
        *,
        image: Optional[str] = None,
        purpose: str = AGENT_STEP_PURPOSE,
        reuse: bool = True,
        hostile: bool = False,
        allow_network: bool = False,
        env: Optional[Dict[str, str]] = None,
        mem_limit: str = DEFAULT_MEM_LIMIT,
        cpu_limit: float = DEFAULT_CPU_LIMIT,
        pids_limit: int = DEFAULT_PIDS_LIMIT,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        trace_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        if purpose not in ALLOWED_PURPOSES:
            # The final verification boundary mints verified success. It must
            # never run inside a shared container, so this refuses loudly
            # instead of degrading quietly.
            raise WarmSandboxError(
                f"warm sandbox cannot serve purpose {purpose!r}; the final "
                "verification boundary must keep a fresh container per command "
                "via execution.sandbox.execute_sandboxed"
            )
        if not Path(repo_path).expanduser().is_dir():
            raise FileNotFoundError(f"repo_path is not a directory: {repo_path}")
        self.repo_path = str(Path(repo_path).expanduser().resolve(strict=True))
        self.task_id = str(task_id or "")
        self.purpose = purpose
        self.reuse = bool(reuse)
        self.hostile = bool(hostile)
        self.allow_network = bool(allow_network)
        self.env = dict(env or {})
        self.mem_limit = mem_limit
        self.cpu_limit = cpu_limit
        self.pids_limit = pids_limit
        self.timeout_s = int(timeout_s)
        self.trace_hook = trace_hook
        self._image = str(image or "")
        self._container: Optional[str] = None
        self._lock = threading.RLock()
        self._execs = 0
        self._per_command = 0
        self._created_at: Optional[float] = None
        self._released = False
        self._identity: Optional[SandboxIdentity] = None

    # -- identity ---------------------------------------------------------

    @property
    def identity(self) -> SandboxIdentity:
        """Return this sandbox's identity (resolving the image on first use)."""
        if self._identity is None:
            self._identity = SandboxIdentity(
                repo_path=self.repo_path,
                task_id=self.task_id,
                image=self._image or f"pending:{self.repo_path}",
                purpose=self.purpose,
            )
        return self._identity

    def _freeze_identity(self, image: str) -> None:
        self._image = image
        self._identity = SandboxIdentity(
            repo_path=self.repo_path,
            task_id=self.task_id,
            image=image,
            purpose=self.purpose,
        )

    def _claim_identity(self) -> None:
        """Register this identity, refusing to share across identities."""
        with _LIVE_LOCK:
            live = _LIVE.get("identity")
            if isinstance(live, SandboxIdentity) and live != self.identity:
                raise IdentityMismatch(
                    "refusing to reuse a warm sandbox across identity "
                    f"boundaries: live={live.describe()} "
                    f"requested={self.identity.describe()}"
                )
            _LIVE["identity"] = self.identity
            _LIVE["sandbox"] = self

    def _release_identity(self) -> None:
        with _LIVE_LOCK:
            if _LIVE.get("sandbox") is self:
                _LIVE["identity"] = None
                _LIVE["sandbox"] = None

    # -- lifecycle --------------------------------------------------------

    def ensure(self) -> str:
        """Start the warm container if needed and return its name.

        Refuses (rather than sharing) when another identity already holds this
        process's warm slot, and refuses to run at all when the daemon is
        unreachable — the fail-loud policy of ``execution.sandbox`` is not
        relaxed for the warm path.
        """
        with self._lock:
            if self._released:
                raise WarmSandboxError("warm sandbox already released")
            if self._container is not None:
                return self._container
            if not docker_available():
                raise SandboxUnavailableError(
                    "docker daemon not reachable; refusing to run unsandboxed"
                )
            image = self._image or ensure_image(self.repo_path)
            self._freeze_identity(image)
            if self.reuse:
                self._claim_identity()
            name = f"{_container_name_prefix()}-w{uuid.uuid4().hex[:12]}"
            js_volume = None
            try:
                if _sandbox._detect_repo_language(self.repo_path) == "js":
                    js_volume = _sandbox._js_deps_volume(self.repo_path, image)
            except Exception:
                js_volume = None
            run_args = _docker_run_args(
                image,
                self.repo_path,
                self.timeout_s,
                self.allow_network,
                self.env or None,
                self.mem_limit,
                self.cpu_limit,
                self.pids_limit,
                js_deps=js_volume,
            )
            # `_docker_run_args` mints its own per-call name; the warm path
            # needs a name it can remember for `docker exec`, so the value is
            # replaced in place (the flag and its value are both preserved).
            run_args = _with_container_name(run_args, name)
            # `-d` goes directly after `run`, so the argv is byte-comparable
            # with the per-command path apart from the detach flag.
            argv = ["docker", "run", "-d", *run_args[1:], "bash", "-c", _IDLE_COMMAND]
            # Same fail-closed static isolation gate the per-command path uses.
            assert_sandbox_argv_isolated(argv[1:], self.repo_path)
            image_ref = pin_image(image)
            spawn = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                env=_docker_environment(),
                timeout=300,
                check=False,
            )
            if spawn.returncode != 0:
                self._release_identity()
                detail = (spawn.stderr or spawn.stdout or "").strip()[:500]
                raise WarmSandboxError(
                    f"warm sandbox could not start: {detail or 'docker run -d failed'}"
                )
            self._container = name
            self._created_at = time.time()
            self._emit(
                "sandbox_warm_start",
                {
                    "container": name,
                    "image": image_ref,
                    "identity": self.identity.key.split("|")[1] or "(none)",
                    "reuse": self.reuse,
                    "hostile": self.hostile,
                },
            )
            return name

    def run(
        self,
        command: str,
        timeout_s: Optional[int] = None,
        *,
        cancel_event: Optional[object] = None,
    ) -> ExecutionResult:
        """Run one command, in the warm container or per-command as configured.

        A hostile sandbox (or ``reuse=False``) runs every command through
        ``execute_sandboxed``, so isolation is per command and the warm
        container is never created.
        """
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if self._released:
            raise WarmSandboxError("warm sandbox already released")
        if not self.reuse or self.hostile:
            self._per_command += 1
            return _sandbox.execute_sandboxed(
                self.repo_path,
                command,
                int(timeout_s or self.timeout_s),
                allow_network=self.allow_network,
                env=self.env or None,
                mem_limit=self.mem_limit,
                cpu_limit=self.cpu_limit,
                pids_limit=self.pids_limit,
                cancel_event=cancel_event,
            )
        name = self.ensure()
        with self._lock:
            self._execs += 1
        argv = [
            "docker",
            "exec",
            name,
            "bash",
            "-c",
            command,
        ]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                env=_docker_environment(),
                timeout=float(timeout_s or self.timeout_s),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._kill_container()
            partial_out = exc.stdout if isinstance(exc.stdout, str) else ""
            partial_err = exc.stderr if isinstance(exc.stderr, str) else ""
            self._emit(
                "sandbox_warm_exec",
                {
                    "container": name,
                    "command": command[:400],
                    "exit_code": TIMEOUT_EXIT_CODE,
                    "timed_out": True,
                    "elapsed_s": round(time.monotonic() - started, 3),
                },
            )
            # THE SUBPROCESS-OUTPUT INGRESS, warm-container path. Same
            # argument as the cold path: `docker exec` writes these two streams
            # and they become a caller-visible ExecutionResult, so they are
            # sealed before they leave. This module currently has no
            # production call site, which is exactly why it is easy to forget
            # — a module nothing imports is the one that quietly grows a
            # second, un-fenced egress.
            return _sealed(
                ExecutionResult(
                    exit_code=TIMEOUT_EXIT_CODE,
                    stdout=_truncate(partial_out),
                    stderr=_truncate(
                        partial_err
                        + f"\n[sandbox] timed out after {timeout_s or self.timeout_s}s"
                    ),
                    timed_out=True,
                )
            )
        exit_code = proc.returncode if proc.returncode is not None else 1
        self._emit(
            "sandbox_warm_exec",
            {
                "container": name,
                "command": command[:400],
                "exit_code": exit_code,
                "timed_out": False,
                "elapsed_s": round(time.monotonic() - started, 3),
            },
        )
        return _sealed(
            ExecutionResult(
                exit_code=exit_code,
                stdout=_truncate(proc.stdout or ""),
                stderr=_truncate(proc.stderr or ""),
                timed_out=False,
            )
        )

    def release(self) -> bool:
        """Stop and remove the warm container. Returns whether one was removed.

        Safe to call more than once. A failure to stop is reported (False) and
        still clears the process-local identity claim, so a wedged container is
        reaped by the existing orphan sweep rather than blocking the next task.
        """
        with self._lock:
            name = self._container
            self._container = None
            self._released = True
        self._release_identity()
        if not name:
            return False
        removed = False
        for argv in (
            ["docker", "stop", "-t", "5", name],
            ["docker", "rm", "-f", name],
        ):
            try:
                subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    env=_docker_environment(),
                    timeout=120,
                    check=False,
                )
                removed = True
            except (OSError, subprocess.SubprocessError):
                continue
        self._emit("sandbox_warm_release", {"container": name, "removed": removed})
        return removed

    # -- introspection ----------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return the auditable receipt for this sandbox."""
        return {
            "container": self._container,
            "identity": {
                "repo_path": self.repo_path,
                "task_id": self.task_id,
                "image": self._image,
                "purpose": self.purpose,
            },
            "reuse": self.reuse,
            "hostile": self.hostile,
            "warm_exec_count": self._execs,
            "per_command_count": self._per_command,
            "created_at": self._created_at,
            "released": self._released,
            "egress_allowlist": (
                list(declared_egress_allowlist()) if self.allow_network else []
            ),
        }

    # -- internals --------------------------------------------------------

    def _kill_container(self) -> None:
        name = self._container
        if not name:
            return
        try:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                text=True,
                env=_docker_environment(),
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
        with self._lock:
            self._container = None
        self._release_identity()

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.trace_hook is None:
            return
        try:
            self.trace_hook(event, dict(payload))
        except Exception:
            # Observability must never change a task outcome.
            pass

    def __enter__(self) -> "WarmTaskSandbox":
        if self.reuse and not self.hostile:
            self.ensure()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def _with_container_name(run_args: List[str], name: str) -> List[str]:
    """Return ``run_args`` with the container ``--name`` value replaced.

    ``_docker_run_args`` generates a unique name per call, which is correct
    for the per-command path and useless here: a warm container is addressed
    by name on every ``docker exec``. The flag list is copied, never mutated,
    and a missing ``--name`` is a hard error rather than a silent no-op (it
    would mean the container could not be addressed at all).
    """
    out = list(run_args)
    for index, token in enumerate(out):
        if token == "--name" and index + 1 < len(out):
            out[index + 1] = name
            return out
    raise WarmSandboxError("docker run argv carried no --name flag")


def _env_snapshot() -> Dict[str, str]:
    """Return the container environment a warm sandbox would forward.

    Exposed for tests and for the run receipt: the warm path must forward
    exactly the scrubbed variables the per-command path forwards, never the
    caller's raw environment.
    """
    return dict(_scrub_container_env(os.environ))


def warm_run_argv_preview(
    image: str, repo_path: str, allow_network: bool = False
) -> List[str]:
    """Return the argv a warm container would be started with (no daemon).

    Pure and unit-testable: the same inputs always produce the same flags, so
    a test can assert the warm path's isolation without a running container.
    """
    return [
        "docker",
        *_docker_run_args(
            image,
            repo_path,
            DEFAULT_TIMEOUT_S,
            allow_network,
            None,
            DEFAULT_MEM_LIMIT,
            DEFAULT_CPU_LIMIT,
            DEFAULT_PIDS_LIMIT,
        ),
        "bash",
        "-c",
        _IDLE_COMMAND,
    ]


def warm_mount_point() -> str:
    """Return the in-container repository mount point (the same /workspace)."""
    return MOUNT_POINT


def warm_sequence() -> Sequence[str]:
    """Return the keep-alive argv tail used by the warm container."""
    return ("bash", "-c", _IDLE_COMMAND)
