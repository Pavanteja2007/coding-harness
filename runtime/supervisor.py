"""P5/W1 T3 — a background run that SURVIVES the process that started it.

**The gap this closes.** ``cli/AGENTS.md`` (VEX-CEILING-10 §7) records the
situation exactly: *"/detach leaves the run to its own worker thread; if the
whole process dies, nothing restarts the run automatically"*, and *"A detached
run is not supervised."* For a daily tool that is the difference between "it
keeps working" and "it silently stopped an hour ago".

So this module is **supervision**, not execution. It owns four things and
nothing else:

1. **A run outlives its parent.** :func:`detach_task` writes the run's task
   payload and launches a DETACHED supervisor process (see
   :func:`_detached_popen_kwargs`) with its own process group / no console
   attachment. ``kill -9`` on the caller leaves the supervisor — and therefore
   the run — alive, which is the entire point of "detach".
2. **Restart on unexpected death.** The supervision loop watches the worker
   process and its heartbeat. A worker that dies WITHOUT writing
   ``result.json`` is relaunched with **the same task payload**, so
   ``runtime.checkpoint.should_resume`` decides resume-vs-fresh exactly as it
   does for a scheduler crash-retry. There is no second resume policy here.
3. **Single-owner, fail-closed.** One supervisor per run, enforced by an
   ``O_EXCL`` lockfile carrying the owner's pid and host
   (:func:`acquire_supervision_lock`). A second supervisor is REFUSED, and so
   is an unreadable lock: an unparseable lock is a denial, not a free slot.
   Only a provably dead holder is taken over, and the takeover is journalled.
4. **Bounded and reapable.** Restarts are capped (:data:`DEFAULT_MAX_RESTARTS`);
   exhaustion is reported, never retried forever. Every detached run is
   reapable (:func:`reap_supervisions`), which removes stale locks and reports
   what it reaped.

**What this module deliberately does NOT do** (each one is a real decision,
not a gap):

* **It does not own the cost.** A supervised run's money is the worker
  governor's (``budget.json``) and the router ledger's. The supervision record
  deliberately carries **no cost field at all**, so a restart cannot add a
  second, contradictory number to a dashboard — ``runtime.budget_governor.spent_usd``
  stays "the maximum, never a sum" and nothing here re-sums it.
* **It does not add a hang-kill exemption.** The hang decision belongs to
  ``runtime.scheduler._check_timeouts``, and the exemption vocabulary there is
  closed at two reasons (``approval_gate``, ``provider_backoff``) over three skip
  conditions. This module REUSES ``state_stale_exempt`` when it decides a stale
  worker is parked rather than hung; it never WRITES a new exemption reason,
  and the pin suite asserts the vocabulary did not grow.
* **It does not re-implement resume, verification, or the hang classifier.**
  They have owners; this calls them.
* **It never claims quality.** A restart count, an uptime, or a status field is
  process bookkeeping, not evidence the run succeeded.

**Operator surface** (``python -m runtime.supervisor``)::

    supervise --task-json PATH [--run-dir DIR] [--max-restarts N] ...
    status --task-id ID [--logs-root DIR] [--json]
    list [--logs-root DIR] [--json]      # every record, live or not
    reap [--logs-root DIR] [--kill-stale-workers] [--json]

Exit codes: 0 finished, 2 denied (someone else owns the run), 3 restart
budget exhausted, 4 supervision error. ``130``/``124`` from a worker are
preserved as ``cancelled``/``timeout`` and are terminal — an interrupted run is
never relabelled a timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .checkpoint import TaskCheckpoint
from .config import apply_defaults
from .fsutil import (
    TASK_SECRETS_ENV,
    append_jsonl,
    atomic_write_json,
    extract_sensitive_config,
    is_pid_alive,
    now_epoch,
    now_iso,
    read_json_or_none,
    redact_sensitive_config,
)
from .paths import runtime_root, validate_path_segment

__all__ = [
    "DEFAULT_HEARTBEAT_STALE_S",
    "DEFAULT_MAX_RESTARTS",
    "DEFAULT_POLL_S",
    "SUPERVISION_STATES",
    "SupervisionDenied",
    "SupervisionOutcome",
    "acquire_supervision_lock",
    "classify_attempt",
    "detach_task",
    "live_supervisions",
    "main",
    "read_supervision",
    "reap_supervisions",
    "release_supervision_lock",
    "supervise",
    "supervision_paths",
]

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The closed supervision-state vocabulary. Closed because a reader that has to
#: ask "is ``reaped`` a state or an event?" cannot build a live-run filter.
#: ``supervising`` -> ``worker_running`` is the steady state; the rest are
#: terminal or one-shot transitions.
SUPERVISION_STATES = (
    "starting",  # lock held, no worker launched yet
    "worker_running",  # a worker process is live (the common case)
    "restarting",  # worker died without a result; relaunching under budget
    "finished",  # worker wrote result.json; status is its verdict
    "exhausted",  # restart budget consumed; no result; run NOT continued
    "failed",  # supervision itself failed (spawn error, unreadable lock)
    "denied",  # another supervisor owns the run; we did nothing
    "reaped",  # a supervisor record whose owner is gone; cleaned by reap
)

#: Terminal states — a run in one of these is no longer live, whatever the pid
#: probe says. ``reaped`` is included because a reaped record is finished with
#: by definition.
TERMINAL_STATES = ("finished", "exhausted", "failed", "denied", "reaped")

#: Extra launches a supervised run may take after its first attempt dies
#: without a result. Mirrors ``crash_retries``'s role for the scheduler (this is
#: the same idea for a run with no scheduler), but is read from its OWN task
#: config key so the two cannot be confused.
DEFAULT_MAX_RESTARTS = 3

#: Supervision loop period. Half a second: fast enough that a killed worker is
#: noticed while the user is still looking at the screen, slow enough that the
#: loop costs one ``poll()`` (a non-blocking OS wait, no sleep) plus one
#: heartbeat read per period.
DEFAULT_POLL_S = 0.5

#: How long a worker's heartbeat may be silent before the supervisor treats it
#: as dead-and-restart-worthy. This is a LIVENESS bound, deliberately not a
#: hang classifier: a worker parked in the approval gate or a provider backoff
#: legitimately stops writing state, and this module reads
#: ``state_stale_exempt`` for exactly that case rather than growing its own
#: exemption. It is also independent of the scheduler's wall-clock cap, which
#: never applies here (there is no scheduler).
DEFAULT_HEARTBEAT_STALE_S = 90.0

#: Worker exit codes that mean something specific and are NOT "crashed":
#: ``130`` = SIGINT/KeyboardInterrupt (cancelled), ``124`` = a timeout tool's
#: code (timed out). Both are terminal and are reported verbatim. A supervisor
#: that collapsed 130 into "crashed and restart me" would turn a user's Ctrl-C
#: into an infinite restart loop; one that collapsed 124 into 130 would
#: misreport a slow provider as a user interrupt.
EXIT_INTERRUPTED = 130
EXIT_TIMEOUT = 124

#: Exit codes of THIS process.
SUPERVISOR_EXIT_FINISHED = 0
SUPERVISOR_EXIT_DENIED = 2
SUPERVISOR_EXIT_EXHAUSTED = 3
SUPERVISOR_EXIT_ERROR = 4

_RECORD_NAME = "supervisor.json"
_LOCK_NAME = "supervisor.lock"
_EVENTS_NAME = "supervisor_events.jsonl"
_SCHEMA_VERSION = 1

#: ``runtime/fsutil.py::TASK_SECRETS_ENV`` — the transient credential transport
#: the worker already reads, imported (not re-declared) so this module cannot
#: drift from the worker's env key.


class SupervisionDenied(RuntimeError):
    """A supervision request that must NOT be honoured as asked.

    Deliberately NOT a ``PermissionError``: ``runtime.provider_gateway``
    absorbs ``PermissionError`` as "try the next candidate", and this refusal
    is not a provider problem at all — a refusal that could be absorbed into a
    failover would turn "someone already owns this run" into "spend money
    elsewhere". Carries the machine-readable ``reason`` and a list of
    ``claims_denied`` so a receipt can say exactly what was not taken.
    """

    def __init__(self, reason: str, detail: str, **claims: Any) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.claims_denied = dict(claims)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "claims_denied": dict(self.claims_denied),
        }


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisionPaths:
    """Every file a supervised run owns. Resolved once, never re-derived."""

    task_id: str
    runtime_dir: Path
    lock_path: Path
    record_path: Path
    events_path: Path
    attempts_dir: Path


def supervision_paths(
    task_id: str, config: Optional[Mapping[str, Any]] = None,
    logs_root: Path = Path("logs"),
) -> SupervisionPaths:
    """Resolve the supervision file set for ``task_id``.

    Assumes: ``task_id`` is a valid path segment (validated here, not assumed).
    The runtime dir honours ``resume_dir`` exactly like ``runtime.paths`` so a
    supervised run and a scheduler-spawned run of the SAME task share one
    checkpoint tree — which is what makes resume work across the two.
    """
    validate_path_segment(task_id, "task id")
    cfg = dict(config or {})
    runtime_dir = Path(runtime_root(task_id, cfg, logs_root))
    return SupervisionPaths(
        task_id=task_id,
        runtime_dir=runtime_dir,
        lock_path=runtime_dir / _LOCK_NAME,
        record_path=runtime_dir / _RECORD_NAME,
        events_path=runtime_dir / _EVENTS_NAME,
        attempts_dir=runtime_dir / "attempts",
    )


def _host() -> str:
    """Best-effort host identity for the record. Never raises."""
    try:
        return socket.gethostname() or "unknown"
    except OSError:  # pragma: no cover - exotic platform
        return "unknown"


def _journal(paths: SupervisionPaths, event: str, data: Optional[Mapping[str, Any]] = None) -> None:
    """Append one supervision event. Redaction is the writer's job elsewhere;
    supervision events carry ids, pids, and counts — never a payload."""
    append_jsonl(
        paths.events_path,
        {"ts": now_iso(), "epoch": now_epoch(), "event": event, "data": dict(data or {})},
    )


# ---------------------------------------------------------------------------
# The record (the observability surface)
# ---------------------------------------------------------------------------


@dataclass
class SupervisionOutcome:
    """What a supervision loop concluded. JSON-safe via :meth:`to_dict`.

    Carries NO cost field on purpose — see the module docstring. A supervised
    run's money is the worker governor's ``budget.json`` and the router ledger;
    a second number computed by summing per-restart results is exactly the
    double-count this module refuses to invent.
    """

    task_id: str
    state: str = "starting"
    supervisor_pid: int = field(default_factory=os.getpid)
    host: str = field(default_factory=_host)
    started_epoch: float = field(default_factory=now_epoch)
    started_at: str = field(default_factory=now_iso)
    attempts: int = 0
    restarts: int = 0
    max_restarts: int = DEFAULT_MAX_RESTARTS
    #: pid -> attempt number of each worker launched by THIS supervisor.
    worker_pids: Dict[str, int] = field(default_factory=dict)
    #: The current worker's pid, or ``None`` between attempts.
    worker_pid: Optional[int] = None
    #: The current worker's attempt-scoped heartbeat token (the same token the
    #: worker writes into ``heartbeat.json``). Lets a reader tell "this record
    #: is describing the worker I think" from "this record is stale".
    attempt_token: str = ""
    #: The worker's own verdict when it wrote ``result.json``; ``None`` if it
    #: never did (the crash / restart path). ``cancelled`` and ``timeout`` are
    #: distinct here and are never collapsed.
    worker_status: Optional[str] = None
    worker_status_reason: str = ""
    #: Last time any state changed (an event was journalled, a worker died, a
    #: result landed). A reader comparing this against the clock answers "is
    #: anything happening?" without reading the event journal.
    last_progress_epoch: float = field(default_factory=now_epoch)
    #: Takeovers of a DEAD previous owner's lock, recorded rather than hidden:
    #: a supervision record that appeared after a silent gap needs a reason.
    lock_takeovers: int = 0
    exhausted_reason: str = ""
    detail: str = ""
    events_path: str = ""
    #: This record's own directory. A reader that found the record by scanning
    #: (see :func:`read_supervision`) can find everything else from here, which
    #: is why the runtime layout does not have to be guessed twice.
    runtime_dir: str = ""
    schema_version: int = _SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "schema_version": int(self.schema_version),
            "task_id": self.task_id,
            "state": self.state,
            "terminal": self.state in TERMINAL_STATES,
            "supervisor_pid": int(self.supervisor_pid),
            "host": self.host,
            "started_epoch": round(float(self.started_epoch), 3),
            "started_at": self.started_at,
            "uptime_s": round(max(0.0, now_epoch() - float(self.started_epoch)), 3),
            "attempts": int(self.attempts),
            "restarts": int(self.restarts),
            "max_restarts": int(self.max_restarts),
            "restarts_remaining": max(0, int(self.max_restarts) - int(self.restarts)),
            "worker_pids": dict(self.worker_pids),
            "worker_pid": self.worker_pid,
            "attempt_token": self.attempt_token,
            "worker_status": self.worker_status,
            "worker_status_reason": self.worker_status_reason,
            "last_progress_epoch": round(float(self.last_progress_epoch), 3),
            "last_progress_age_s": round(
                max(0.0, now_epoch() - float(self.last_progress_epoch)), 3
            ),
            "lock_takeovers": int(self.lock_takeovers),
            "exhausted_reason": self.exhausted_reason,
            "detail": self.detail,
            "events_path": self.events_path,
            "runtime_dir": self.runtime_dir,
            "states": list(SUPERVISION_STATES),
        }
        return data


def _write_record(paths: SupervisionPaths, outcome: SupervisionOutcome) -> None:
    outcome.runtime_dir = str(paths.runtime_dir)
    atomic_write_json(paths.record_path, outcome.to_dict())


def _scan_supervision_files(logs_root: Path, filename: str, *, depth: int = 3) -> List[Path]:
    """Find supervision files under ``logs_root`` without guessing the layout.

    The runtime dir is wherever ``resume_dir`` (or the default sibling) points,
    and callers pin it in a dozen shapes (``logs/{id}.runtime``,
    ``logs/tasks/{id}``, a per-run tree). Rather than re-deriving those shapes
    here, a bounded scan looks for the files by their own names — supervision
    state is self-identifying, so a reader should not need the writer's path
    policy. Bounded to ``depth`` levels so a status read on a huge logs tree
    cannot become a directory walk (this repository has measured 124 774
    directory walks; the honest cheap thing is a bounded scan, not a full one).

    Records AND locks are scanned separately because either can be present
    without the other: a lock whose record was never written is exactly the
    debris a crash between the two writes leaves behind.
    """
    root = Path(logs_root)
    if not root.exists():
        return []
    found: List[Path] = []
    frontier = [(root, 0)]
    while frontier:
        directory, level = frontier.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:  # pragma: no cover - unreadable dir
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            candidate = entry / filename
            if candidate.exists():
                found.append(candidate)
            if level < depth:
                frontier.append((entry, level + 1))
    return sorted(found)


def _scan_records(logs_root: Path, *, depth: int = 3) -> List[Path]:
    return _scan_supervision_files(logs_root, _RECORD_NAME, depth=depth)


def read_supervision(
    task_id: str,
    config: Optional[Mapping[str, Any]] = None,
    logs_root: Path = Path("logs"),
) -> Optional[Dict[str, Any]]:
    """Read one supervision record, or ``None`` if this run was never supervised.

    The config-resolved path is tried first (it is O(1)); if that misses, a
    bounded scan looks for a record whose ``task_id`` matches. The scan matters
    because a status surface usually knows the task id and NOT the run's
    ``resume_dir`` — and a reader that silently found nothing would report "not
    supervised" for a run that very much is.

    This is the read side T4 renders. It is a pure file read — no pid probing,
    no liveness claim — so it is safe to call from a render loop or a status
    command that runs while the supervisor is mid-write.
    """
    paths = supervision_paths(task_id, config, logs_root)
    record = read_json_or_none(paths.record_path)
    if isinstance(record, dict):
        return record
    for candidate in _scan_records(logs_root):
        data = read_json_or_none(candidate)
        if isinstance(data, dict) and str(data.get("task_id") or "") == task_id:
            return data
    return None


def live_supervisions(
    logs_root: Path = Path("logs"),
    config: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Every supervision record on this host's logs tree, live rows first.

    A "live" record is one whose state is not in :data:`TERMINAL_STATES`. The
    list includes dead-but-not-reaped records with ``live`` still true — a
    reader that wants the truth should pair it with
    :func:`is_pid_alive(record["supervisor_pid"])`; the record itself does not
    claim liveness it has not checked.
    """
    out: List[Dict[str, Any]] = []
    for candidate in _scan_records(logs_root):
        record = read_json_or_none(candidate)
        if not isinstance(record, dict):
            continue
        record.setdefault("task_id", candidate.parent.name)
        state = str(record.get("state") or "")
        record["live"] = state not in TERMINAL_STATES
        record["record_path"] = str(candidate)
        out.append(record)
    return sorted(out, key=lambda r: (not bool(r["live"]), r.get("task_id") or ""))


# ---------------------------------------------------------------------------
# Single-owner lock
# ---------------------------------------------------------------------------


def _lock_payload(pid: int) -> Dict[str, Any]:
    return {"pid": int(pid), "host": _host(), "at": now_iso(), "epoch": now_epoch()}


def _lock_owner_alive(payload: Mapping[str, Any]) -> Optional[bool]:
    """Is the recorded owner alive? ``None`` when it cannot be decided.

    ``None`` is the fail-closed answer: a lock whose host is not this host, or
    whose pid cannot be read, is not evidence of a free slot. The supervisor
    refuses in that case rather than guessing, and ``reap`` is how an operator
    clears it.
    """
    if str(payload.get("host") or "") != _host():
        return None  # a shared filesystem: local pid knowledge says nothing
    pid = payload.get("pid")
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return None
    return is_pid_alive(pid_int)


def acquire_supervision_lock(
    paths: SupervisionPaths,
    *,
    pid: Optional[int] = None,
    now: Optional[Callable[[], float]] = None,
) -> int:
    """Take single-owner ownership of a run. Returns takeovers (0 or 1).

    Uses ``O_CREAT | O_EXCL`` so two racing supervisors cannot both win: the
    kernel decides, not a check-then-write. On an existing lock:

    * owner **alive** -> :class:`SupervisionDenied` (``run_already_supervised``).
    * owner **dead** -> take over, count it, and let the caller journal it.
    * owner **undecidable** (foreign host, unreadable pid) -> denied, fail-closed.

    A lock owned by THIS process is denied too, not treated as re-entrant:
    supervision of one run is not re-entrant, and a "re-entrant" path would be
    a second worker-launching path nobody tested.
    """
    owner_pid = int(pid if pid is not None else os.getpid())
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            str(paths.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError:
        payload = read_json_or_none(paths.lock_path)
        if not isinstance(payload, dict):
            raise SupervisionDenied(
                "lock_unreadable",
                f"{paths.lock_path} exists but cannot be parsed; refusing to"
                " assume the run is free (reap it if you are sure)",
                lock_path=str(paths.lock_path),
            ) from None
        alive = _lock_owner_alive(payload)
        if alive is None:
            raise SupervisionDenied(
                "lock_owner_undecidable",
                "lock is held by host"
                f" {payload.get('host')!r} pid {payload.get('pid')!r}, which this"
                " host cannot prove dead or alive; refusing (fail-closed)",
                lock_path=str(paths.lock_path),
                lock_host=payload.get("host"),
                lock_pid=payload.get("pid"),
            ) from None
        if alive:
            raise SupervisionDenied(
                "run_already_supervised",
                f"run is owned by pid {payload.get('pid')} on"
                f" {payload.get('host')} since {payload.get('at')!r}",
                lock_path=str(paths.lock_path),
                lock_pid=payload.get("pid"),
                lock_host=payload.get("host"),
                lock_since=payload.get("at"),
            ) from None
        # Dead owner: take over. Recorded by the caller so the gap is visible.
        with open(paths.lock_path, "w", encoding="utf-8") as handle:
            json.dump(_lock_payload(owner_pid), handle)
        return 1
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(_lock_payload(owner_pid), handle)
    return 0


def release_supervision_lock(
    paths: SupervisionPaths, *, pid: Optional[int] = None
) -> bool:
    """Release ownership IF this pid owns it. True if a lock was removed.

    Ownership-checked on purpose: a supervisor that has already lost the lock to
    a takeover must not delete the NEW owner's lock on its way out. Returns
    False (and removes nothing) when the lock is absent, unparseable, or held
    by someone else.
    """
    owner_pid = int(pid if pid is not None else os.getpid())
    payload = read_json_or_none(paths.lock_path)
    if not isinstance(payload, dict):
        return False
    try:
        held_by = int(payload.get("pid"))
    except (TypeError, ValueError):
        return False
    if held_by != owner_pid:
        return False
    try:
        paths.lock_path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def _detached_popen_kwargs() -> Dict[str, Any]:
    """The per-platform ``Popen`` kwargs that make a process DETACH.

    Windows: ``DETACHED_PROCESS`` (no console inheritance; the process is not
    killed when its parent's console closes) plus ``CREATE_NEW_PROCESS_GROUP``
    (its own Ctrl-C group, so a Ctrl-C aimed at the foreground CLI does not
    reach the supervisor — the supervisor must not die from the user's Ctrl-C;
    it must keep the run alive). POSIX: ``start_new_session`` (its own session,
    no controlling terminal).

    This is the piece that makes "survives ``kill -9`` of its parent" true
    rather than aspirational: on both platforms the child is reparented to init
    when the parent dies, and nothing walks down killing the tree.
    """
    if os.name == "nt":  # pragma: no cover - platform-specific
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            flags |= int(getattr(subprocess, name, 0))
        # A detached process with no console cannot use a console stream;
        # redirect to the null device so the Popen cannot fail on a missing
        # console handle.
        return {
            "creationflags": flags,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def _write_task_payload(
    paths: SupervisionPaths,
    task: Any,
    config: Mapping[str, Any],
) -> Dict[str, str]:
    """Write ``attempt_0/task.json`` for the first attempt. Returns the paths.

    Reuses ``runtime.fsutil``'s credential transport EXACTLY as
    ``runtime.scheduler._spawn`` does: the config is written redacted and the
    sensitive entries ride in ``TASK_SECRETS_ENV`` for the child. Two writers of
    that protocol would be two answers to "how does a worker get its key".
    """
    attempts = paths.attempts_dir
    attempts.mkdir(parents=True, exist_ok=True)
    attempt_dir = attempts / "attempt_0"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    task_json = attempt_dir / "task.json"
    cfg = dict(config)
    secret_entries = extract_sensitive_config(cfg)
    atomic_write_json(
        task_json,
        {
            "task_id": str(task.task_id),
            "repo_path": str(getattr(task, "repo_path", "") or ""),
            "issue_text": "",  # transient, via env — never persisted
            "config": redact_sensitive_config(cfg),
        },
    )
    return {
        "task_json": str(task_json),
        "attempt_dir": str(attempt_dir),
        "secrets_env": json.dumps(
            {
                "issue_text": str(getattr(task, "issue_text", "") or ""),
                "secrets": secret_entries,
            }
        ),
    }


def _repo_root() -> str:
    """The repository root — where ``runtime.worker`` must be importable."""
    return str(Path(__file__).resolve().parent.parent)


def _spawn_worker(
    paths: SupervisionPaths,
    payload: Mapping[str, str],
    attempt: int,
    *,
    secrets_env: str,
    cwd: Optional[str] = None,
    worker_argv: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Launch one worker for ``attempt`` as a supervised child.

    The default child is a ``runtime.worker`` subprocess launched EXACTLY as
    ``runtime.scheduler._spawn`` launches it (same argv shape, same redacted
    task.json, same secret env key), so every property resume, the approval
    gate, and the budget governor rely on is the same in supervised mode.

    ``worker_argv`` overrides the command. That is a real seam, not a test
    hook for its own sake: a caller may need a wrapper (an env-setting shim, a
    container launcher) between the supervisor and the worker. It is also the
    only way to drive the crash/exhaustion/cancelled/timeout shapes
    deterministically — the fake harness DISARMS its own fault injection on the
    first relaunch precisely so a crash-resume cannot loop forever, which means
    "a worker that always dies" cannot be constructed from a real task config.
    When supplied it must be a full argv whose ``{task_json}``, ``{run_dir}``
    and ``{attempt_token}`` placeholders are filled by the caller.

    Returns the Popen plus the attempt token, which the supervisor records so a
    reader can tell which heartbeat belongs to which launch.
    """
    attempt_dir = paths.attempts_dir / f"attempt_{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    attempt_token = uuid.uuid4().hex
    child_env = os.environ.copy()
    # Restore the caller's preload preference for the WORKER: the supervisor
    # runs with the provider-library preload off (it makes no model calls and
    # must start fast), but the worker's warm-first-call behaviour must match a
    # scheduler-spawned worker exactly.
    child_env.pop("NEO_PRELOAD_LITELLM", None)
    child_env[TASK_SECRETS_ENV] = secrets_env
    argv = list(worker_argv) if worker_argv else [
        sys.executable,
        "-m",
        "runtime.worker",
        "--task-json",
        str(payload["task_json"]),
        "--run-dir",
        str(attempt_dir),
        "--attempt-token",
        attempt_token,
    ]
    log_handle = (attempt_dir / "worker.log").open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd or _repo_root()),
            env=child_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **_detached_popen_kwargs(),
        )
    except OSError as exc:
        # A worker that cannot even be LAUNCHED is a supervision failure, not a
        # crash loop: reported with the OS error rather than as a generic exit.
        raise SupervisionDenied(
            "worker_spawn_failed",
            f"could not launch the supervised worker: {exc}",
            attempt=attempt,
        ) from exc
    finally:
        # Popen dups the handle; the parent's copy is closed here so a long
        # supervision run does not accumulate one open file per attempt.
        log_handle.close()
    return {
        "proc": proc,
        "pid": proc.pid,
        "attempt_token": attempt_token,
        "attempt_dir": str(attempt_dir),
    }


# ---------------------------------------------------------------------------
# Attempt classification — the honesty seam
# ---------------------------------------------------------------------------


def classify_attempt(
    result: Optional[Mapping[str, Any]], exit_code: Optional[int]
) -> Dict[str, Any]:
    """Decide what one worker attempt's exit MEANS. Never restarts on a
    verdict, only on an absence.

    The three shapes, and why each is what it is:

    * **A result was written** (``result.json`` present): the worker reached its
      own terminal verdict. It is returned verbatim — including ``cancelled``
      (130) and ``timeout`` (124), which stay distinct. A worker that decided
      something must not be overruled by "it didn't crash".
    * **No result, exit 130 / 124** (interrupted / timed out): terminal, with
      the exit code recorded. Restarting an interrupted run would fight the
      user; restarting a timed-out run would re-enter the same slow condition
      forever.
    * **No result, any other exit** (crash, OOM, ``kill -9``): the only
      restartable shape. ``retryable=True`` here and nowhere else.

    Returns ``{"terminal": str|None, "reason": str, "retryable": bool,
    "exit_code": int|None}``. ``terminal`` is ``None`` exactly when retryable.
    """
    exit_code = int(exit_code) if exit_code is not None else None
    if isinstance(result, Mapping):
        status = str(result.get("status") or "")
        return {
            "terminal": status or "unknown",
            "reason": "worker_result",
            "retryable": False,
            "exit_code": exit_code,
        }
    if exit_code == EXIT_INTERRUPTED:
        return {
            "terminal": "cancelled",
            "reason": f"worker_exit_{EXIT_INTERRUPTED}",
            "retryable": False,
            "exit_code": exit_code,
        }
    if exit_code == EXIT_TIMEOUT:
        return {
            "terminal": "timeout",
            "reason": f"worker_exit_{EXIT_TIMEOUT}",
            "retryable": False,
            "exit_code": exit_code,
        }
    return {
        "terminal": None,
        "reason": "worker_died_without_result",
        "retryable": True,
        "exit_code": exit_code,
    }


# ---------------------------------------------------------------------------
# The supervision loop
# ---------------------------------------------------------------------------


def supervise(
    task_json_path: str,
    run_dir: str = "",
    *,
    task_id: str = "",
    logs_root: Path = Path("logs"),
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    poll_s: float = DEFAULT_POLL_S,
    heartbeat_stale_s: Optional[float] = DEFAULT_HEARTBEAT_STALE_S,
    secrets_env: Optional[str] = None,
    worker_argv: Optional[Sequence[str]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> SupervisionOutcome:
    """Supervise a run to a terminal state. This is the loop a detached
    supervisor process runs.

    Loop, one poll at a time:

    1. ``proc.poll()`` — is the worker still alive?
    2. If dead: read the attempt's ``result.json``, classify (see
       :func:`classify_attempt`). A verdict is terminal and reported verbatim.
       A death WITHOUT a result is the only retryable shape, and the restart
       budget decides whether it is retried or reported as ``exhausted``.
    3. If alive: check the ATTEMPT-SCOPED heartbeat age. Older than
       ``heartbeat_stale_s`` and not ``state_stale_exempt`` -> the worker is
       presumed wedged: terminate it and let the crash branch handle the
       restart. This is the supervisor's only liveness response and it reuses
       the exemption reader rather than inventing a fourth exemption reason.
    4. Rewrite the record on every state change and every ~2 s otherwise, so a
       reader polling the file sees progress without reading the journal.

    Assumes: ``task_json_path`` was written by :func:`detach_task` (or by the
    scheduler — the worker reads either); the transient credential payload
    arrives either in this process's ``TASK_SECRETS_ENV`` (the detached path —
    credentials are never written to disk) or via the ``secrets_env`` argument
    (the inline path). Returns the final :class:`SupervisionOutcome`.
    """
    clock = clock or now_epoch
    task_payload = read_json_or_none(task_json_path)
    if not isinstance(task_payload, Mapping):
        raise ValueError(f"task payload is not readable: {task_json_path}")
    raw_cfg = task_payload.get("config")
    cfg: Dict[str, Any] = dict(raw_cfg) if isinstance(raw_cfg, Mapping) else {}
    task_id = task_id or str(task_payload.get("task_id") or Path(task_json_path).stem)
    paths = supervision_paths(task_id, cfg, logs_root)
    outcome = SupervisionOutcome(task_id=task_id, max_restarts=int(max_restarts))
    outcome.events_path = str(paths.events_path)

    try:
        takeovers = acquire_supervision_lock(paths)
    except SupervisionDenied as exc:
        outcome.state = "denied"
        outcome.detail = json.dumps(exc.as_dict())
        _write_record(paths, outcome)
        _journal(paths, "supervision_denied", exc.as_dict())
        return outcome
    outcome.lock_takeovers = takeovers
    if takeovers:
        _journal(paths, "lock_taken_over", {"pid": outcome.supervisor_pid})
    _journal(
        paths,
        "supervisor_started",
        {"pid": outcome.supervisor_pid, "host": outcome.host},
    )
    _write_record(paths, outcome)

    transient = secrets_env if secrets_env is not None else os.environ.get(TASK_SECRETS_ENV)
    if transient is None:
        transient = json.dumps({"issue_text": "", "secrets": []})
        outcome.detail = (
            "no transient task payload was available to this supervisor"
            " (expected NEO_RUNTIME_TASK_SECRETS in the environment); the worker"
            " started with an EMPTY issue text, which is reported rather than"
            " invented"
        )
        _journal(paths, "transient_payload_absent", {})

    payload = {"task_json": str(task_json_path)}

    def _terminate(proc: "subprocess.Popen") -> Optional[int]:
        """Stop a wedged worker. Returns its exit code when one is available."""
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    return proc.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover - rare
                    proc.kill()
                    return proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return None
        return proc.poll()

    def _attempt_result(attempt: int) -> Optional[Mapping[str, Any]]:
        data = read_json_or_none(paths.attempts_dir / f"attempt_{attempt}" / "result.json")
        return data if isinstance(data, Mapping) else None

    attempt = 0
    try:
        spawn = _spawn_worker(
            paths, payload, attempt, secrets_env=transient, worker_argv=worker_argv
        )
    except SupervisionDenied as exc:
        outcome.state = "failed"
        outcome.detail = json.dumps(exc.as_dict())
        outcome.last_progress_epoch = clock()
        _journal(paths, "worker_spawn_failed", exc.as_dict())
        _write_record(paths, outcome)
        return outcome
    proc = spawn["proc"]
    outcome.attempts = 1
    outcome.worker_pid = spawn["pid"]
    outcome.worker_pids[str(spawn["pid"])] = attempt
    outcome.attempt_token = spawn["attempt_token"]
    outcome.state = "worker_running"
    outcome.last_progress_epoch = clock()
    _write_record(paths, outcome)
    _journal(paths, "worker_launched", {"attempt": attempt, "pid": spawn["pid"]})

    last_record_epoch = float(clock())
    try:
        while True:
            exit_code = proc.poll()

            if exit_code is not None:
                verdict = classify_attempt(_attempt_result(attempt), exit_code)
                if not verdict["retryable"]:
                    outcome.state = "finished"
                    outcome.worker_pid = None
                    outcome.worker_status = verdict["terminal"]
                    outcome.worker_status_reason = verdict["reason"]
                    outcome.last_progress_epoch = clock()
                    _journal(
                        paths,
                        "worker_finished",
                        {
                            "attempt": attempt,
                            "status": verdict["terminal"],
                            "reason": verdict["reason"],
                            "exit_code": verdict["exit_code"],
                        },
                    )
                    _write_record(paths, outcome)
                    return outcome
                if outcome.restarts >= outcome.max_restarts:
                    outcome.state = "exhausted"
                    outcome.worker_pid = None
                    outcome.exhausted_reason = (
                        "worker died without a result"
                        f" {outcome.max_restarts + 1} times"
                        f" (restart budget {outcome.max_restarts} exhausted;"
                        " the run was NOT continued)"
                    )
                    outcome.last_progress_epoch = clock()
                    _journal(
                        paths,
                        "restart_budget_exhausted",
                        {
                            "restarts": outcome.restarts,
                            "max_restarts": outcome.max_restarts,
                            "last_exit_code": verdict["exit_code"],
                        },
                    )
                    _write_record(paths, outcome)
                    return outcome
                outcome.restarts += 1
                outcome.state = "restarting"
                outcome.last_progress_epoch = clock()
                _journal(
                    paths,
                    "worker_restarting",
                    {
                        "attempt": attempt,
                        "exit_code": verdict["exit_code"],
                        "restart": outcome.restarts,
                        "max_restarts": outcome.max_restarts,
                    },
                )
                attempt += 1
                outcome.attempts += 1
                try:
                    spawn = _spawn_worker(
                        paths,
                        payload,
                        attempt,
                        secrets_env=transient,
                        worker_argv=worker_argv,
                    )
                except SupervisionDenied as exc:
                    outcome.state = "failed"
                    outcome.worker_pid = None
                    outcome.detail = json.dumps(exc.as_dict())
                    outcome.last_progress_epoch = clock()
                    _journal(paths, "worker_spawn_failed", exc.as_dict())
                    _write_record(paths, outcome)
                    return outcome
                proc = spawn["proc"]
                outcome.worker_pid = spawn["pid"]
                outcome.worker_pids[str(spawn["pid"])] = attempt
                outcome.attempt_token = spawn["attempt_token"]
                outcome.state = "worker_running"
                _write_record(paths, outcome)
                _journal(
                    paths, "worker_launched", {"attempt": attempt, "pid": spawn["pid"]}
                )
                last_record_epoch = float(clock())
                continue

            # -- worker alive: attempt-scoped heartbeat liveness bound. This is
            # NOT a hang classifier and NOT an exemption: it reads the shared
            # exemption authority so an approval park or a provider backoff is
            # never mistaken for a wedged worker.
            if heartbeat_stale_s is not None:
                cp = TaskCheckpoint(str(paths.runtime_dir))
                age = cp.heartbeat_age_s(spawn["attempt_token"])
                if age is not None and age > float(heartbeat_stale_s):
                    exempt = False
                    exempt_reason = ""
                    try:
                        from .budget_governor import state_stale_exempt

                        exempt, exempt_reason = state_stale_exempt(cp.load() or {})
                    except Exception:  # pragma: no cover - never break supervision
                        exempt = False
                        exempt_reason = "exemption_reader_failed"
                    if not exempt:
                        outcome.last_progress_epoch = clock()
                        _journal(
                            paths,
                            "worker_heartbeat_stale",
                            {
                                "attempt": attempt,
                                "heartbeat_age_s": round(age, 3),
                                "threshold_s": float(heartbeat_stale_s),
                                "exemption_seen": exempt_reason or "none",
                            },
                        )
                        _write_record(paths, outcome)
                        _terminate(proc)
                        continue

            now = float(clock())
            if now - last_record_epoch >= 2.0:
                last_record_epoch = now
                _write_record(paths, outcome)
            if float(poll_s) > 0:
                time.sleep(float(poll_s))
    finally:
        release_supervision_lock(paths)


# ---------------------------------------------------------------------------
# Detach entry
# ---------------------------------------------------------------------------


def detach_task(
    task: Any,
    *,
    logs_root: Path = Path("logs"),
    config: Optional[Mapping[str, Any]] = None,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    detach: bool = True,
    cwd: Optional[str] = None,
    worker_argv: Optional[Sequence[str]] = None,
) -> SupervisionOutcome:
    """Start a run under a supervisor that outlives this process.

    Writes the task payload, takes nothing (the lock belongs to the supervisor
    process), and launches ``python -m runtime.supervisor supervise`` as a
    DETACHED child. Returns immediately with the outcome's ``supervisor_pid``.

    ``detach=False`` runs the supervision loop inline (same process) — used by
    the tests to drive a deterministic end-to-end without a second process. The
    production path is ``detach=True``.

    Assumes: ``task`` is a ``shared.types.Task``; its ``config`` carries the
    runtime knobs (``supervised_max_restarts`` etc. are read from
    :data:`DEFAULT_MAX_RESTARTS` here, then journalled). Returns the supervisor
    record even when the supervisor is still starting, so a caller can show
    "supervised, pid N".
    """
    cfg = apply_defaults(dict(getattr(task, "config", {}) or {}))
    if config:
        cfg.update(dict(config))
    task_id = str(task.task_id)
    paths = supervision_paths(task_id, cfg, logs_root)
    payload = _write_task_payload(paths, task, cfg)

    if not detach:
        return supervise(
            payload["task_json"],
            task_id=task_id,
            logs_root=logs_root,
            max_restarts=int(cfg.get("supervised_max_restarts", max_restarts)),
            secrets_env=str(payload["secrets_env"]),
            worker_argv=worker_argv,
        )

    child_env = os.environ.copy()
    # The supervisor itself never dials a provider; keep the provider-library
    # preload out of its startup.
    child_env["NEO_PRELOAD_LITELLM"] = "off"
    # Credentials ride the ENVIRONMENT to the supervisor and from there to each
    # worker, never the disk: task.json is written redacted, exactly as the
    # scheduler writes it.
    child_env[TASK_SECRETS_ENV] = str(payload["secrets_env"])
    argv = [
        sys.executable,
        "-m",
        "runtime.supervisor",
        "supervise",
        "--task-json",
        payload["task_json"],
        "--task-id",
        task_id,
        "--logs-root",
        str(logs_root),
        "--max-restarts",
        str(int(cfg.get("supervised_max_restarts", max_restarts))),
    ]
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd or _repo_root()),
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_detached_popen_kwargs(),
    )
    # Give the supervisor a beat to write its record so a caller that shows
    # "supervised, pid N" is not reading a record that does not exist yet.
    time.sleep(0.2)
    return SupervisionOutcome(
        task_id=task_id,
        state="starting",
        supervisor_pid=proc.pid,
        events_path=str(paths.events_path),
        detail="supervisor launched detached",
    )


# ---------------------------------------------------------------------------
# Reap
# ---------------------------------------------------------------------------


def _kill_worker(pid: int) -> bool:
    """Hard-kill one orphaned worker. True when the kill was issued.

    Windows: ``taskkill /T /F`` — ``/T`` also takes the worker's own children
    (a container CLI, a pytest process), which a bare ``TerminateProcess``
    would leave running. POSIX: ``SIGKILL`` to the process group the worker was
    given, falling back to the pid. Never raises: a reap that dies on one dead
    pid would leave the rest unreaped, which is the leak this exists to close.
    """
    try:
        if os.name == "nt":  # pragma: no cover - platform-specific
            proc = subprocess.Popen(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.wait(timeout=15)
            return True
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, AttributeError):
            os.kill(pid, signal.SIGKILL)
        return True
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False


def reap_supervisions(
    logs_root: Path = Path("logs"),
    *,
    kill_stale_workers: bool = False,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Clean up supervision state whose owner is gone. Returns what it reaped.

    Two kinds of debris, both reported rather than silently removed:

    * **Stale locks** whose recorded owner is provably dead on this host — the
      lock is unlinked so the run can be supervised again. A lock whose owner
      is undecidable (foreign host) is left alone and reported; clearing it is
      an operator decision, not a guess.
    * **Orphaned records** whose supervisor pid is dead and whose state is
      non-terminal: the record is rewritten to ``reaped`` so a live-run list
      does not show a run nobody is supervising. With
      ``kill_stale_workers=True`` a *recorded worker pid* that is still alive
      for such an orphaned run is terminated too — the supervisor that would
      have reaped it is gone, and an unsupervised worker that has stopped
      beating its heartbeat is the leak this exists to stop. The number killed
      is reported; nothing is killed silently.

    Fail-closed: never deletes a lock whose owner cannot be proven dead.
    """
    root = Path(logs_root)
    reaped_locks: List[str] = []
    reaped_records: List[str] = []
    skipped_locks: List[Dict[str, Any]] = []
    killed_workers: List[int] = []
    if not root.exists():
        return {
            "reaped_locks": [],
            "reaped_records": [],
            "skipped_locks": [],
            "killed_workers": [],
            "detail": "logs root absent",
        }

    directories = {p.parent for p in _scan_supervision_files(root, _RECORD_NAME)}
    directories |= {p.parent for p in _scan_supervision_files(root, _LOCK_NAME)}
    for runtime_dir in sorted(directories):
        paths = SupervisionPaths(
            task_id=runtime_dir.name,
            runtime_dir=runtime_dir,
            lock_path=runtime_dir / _LOCK_NAME,
            record_path=runtime_dir / _RECORD_NAME,
            events_path=runtime_dir / _EVENTS_NAME,
            attempts_dir=runtime_dir / "attempts",
        )
        # -- stale lock
        if paths.lock_path.exists():
            payload = read_json_or_none(paths.lock_path)
            alive = _lock_owner_alive(payload) if isinstance(payload, Mapping) else None
            if alive is False:
                try:
                    paths.lock_path.unlink()
                    reaped_locks.append(paths.task_id)
                except OSError:  # pragma: no cover
                    pass
            elif alive is None:
                skipped_locks.append(
                    {
                        "task_id": paths.task_id,
                        "host": (payload or {}).get("host"),
                        "pid": (payload or {}).get("pid"),
                    }
                )
        # -- orphaned record
        record = read_json_or_none(paths.record_path)
        if not isinstance(record, Mapping):
            continue
        state = str(record.get("state") or "")
        if state in TERMINAL_STATES:
            continue
        sup_pid = record.get("supervisor_pid")
        if isinstance(sup_pid, int) and sup_pid > 0 and is_pid_alive(sup_pid):
            continue  # owner alive; still being supervised
        if kill_stale_workers:
            for pid_str in (record.get("worker_pids") or {}):
                try:
                    wpid = int(pid_str)
                except (TypeError, ValueError):
                    continue
                if is_pid_alive(wpid):
                    _kill_worker(wpid)
                    killed_workers.append(wpid)
        new_record = dict(record)
        new_record["state"] = "reaped"
        new_record["terminal"] = True
        new_record["reaped_at"] = now_iso()
        new_record["reaped_reason"] = "supervisor process is gone; record reclaimed"
        atomic_write_json(paths.record_path, new_record)
        _journal(paths, "supervision_reaped", {"killed_workers": killed_workers})
        reaped_records.append(paths.task_id)

    return {
        "reaped_locks": reaped_locks,
        "reaped_records": reaped_records,
        "skipped_locks": skipped_locks,
        "killed_workers": killed_workers,
        "detail": "",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Operator surface. Exit codes are the vocabulary in the module docstring."""
    parser = argparse.ArgumentParser(prog="runtime.supervisor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sup = sub.add_parser("supervise", help="supervise one run to a terminal state")
    sup.add_argument("--task-json", required=True)
    sup.add_argument("--task-id", default="")
    sup.add_argument("--logs-root", default="logs")
    sup.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS)
    sup.add_argument("--poll-s", type=float, default=DEFAULT_POLL_S)
    sup.add_argument(
        "--heartbeat-stale-s", type=float, default=DEFAULT_HEARTBEAT_STALE_S
    )

    st = sub.add_parser("status", help="read one supervision record")
    st.add_argument("--task-id", required=True)
    st.add_argument("--logs-root", default="logs")
    st.add_argument("--json", action="store_true")

    li = sub.add_parser("list", help="every supervision record on this tree")
    li.add_argument("--logs-root", default="logs")
    li.add_argument("--json", action="store_true")

    rp = sub.add_parser("reap", help="clean up supervision state whose owner is gone")
    rp.add_argument("--logs-root", default="logs")
    rp.add_argument("--kill-stale-workers", action="store_true")
    rp.add_argument("--json", action="store_true")

    args = parser.parse_args(list(argv) if argv is not None else None)
    logs_root = Path(getattr(args, "logs_root", "logs"))

    if args.cmd == "supervise":
        outcome = supervise(
            args.task_json,
            task_id=args.task_id,
            logs_root=logs_root,
            max_restarts=int(args.max_restarts),
            poll_s=float(args.poll_s),
            heartbeat_stale_s=float(args.heartbeat_stale_s),
        )
        print(json.dumps(outcome.to_dict(), indent=2))
        return {
            "finished": SUPERVISOR_EXIT_FINISHED,
            "exhausted": SUPERVISOR_EXIT_EXHAUSTED,
            "denied": SUPERVISOR_EXIT_DENIED,
        }.get(outcome.state, SUPERVISOR_EXIT_ERROR)

    if args.cmd == "status":
        record = read_supervision(args.task_id, logs_root=logs_root)
        if args.json:
            print(json.dumps(record, indent=2))
        elif record is None:
            print(f"{args.task_id}: not supervised (no record)")
        else:
            print(
                f"{record.get('task_id')}  {record.get('state')}  "
                f"pid {record.get('supervisor_pid')} on {record.get('host')}  "
                f"attempts {record.get('attempts')}  restarts {record.get('restarts')}"
                f"/{record.get('max_restarts')}  "
                f"last progress {record.get('last_progress_age_s')}s ago"
            )
        return 0

    if args.cmd == "list":
        records = live_supervisions(logs_root)
        if args.json:
            print(json.dumps(records, indent=2))
        else:
            for r in records:
                print(
                    f"{'LIVE' if r.get('live') else 'done'}  {r.get('task_id')}  "
                    f"{r.get('state')}  pid {r.get('supervisor_pid')} on {r.get('host')}"
                )
        return 0

    if args.cmd == "reap":
        report = reap_supervisions(logs_root, kill_stale_workers=args.kill_stale_workers)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"reaped locks: {report['reaped_locks']}  "
                f"reaped records: {report['reaped_records']}  "
                f"killed workers: {report['killed_workers']}  "
                f"skipped (undecidable owner): {report['skipped_locks']}"
            )
        return 0

    parser.error(f"unknown command {args.cmd}")  # pragma: no cover
    return SUPERVISOR_EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(main())
