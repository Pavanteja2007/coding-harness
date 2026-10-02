"""One writer per work tree: the multi-instance guard for the daily path.

Two ``neo`` processes writing one repository is silent data loss. The failure
is not a crash -- it is two sessions each believing they own
``logs/<task>/work``, a checkpoint taken over a file the other session is
mid-write, and a diff that mixes both. This module makes that state
**detectable and refuses it**, so the second instance stops before it has
written anything.

Design rules (Prompt 08, "Multi-instance, offline, and long sessions"):

* **The lock lives OUTSIDE the repository.** A guard that wrote
  ``.neo/instance.lock`` would dirty a user's working tree to protect it and
  would show up in ``git status`` on the very tree it is meant to leave
  alone. It lives under the Neo home, keyed by a digest of the repository's
  canonical path.
* **KEY-PER-WORK-TREE, not per-git-repository.** Two worktrees of one
  repository are two different directories writing two different sets of
  files, so they are not a conflict; the same worktree reached through two
  spellings (``C:\\repo``, ``c:\\repo\\.``, a symlink, an 8.3 short name) is.
  The canonical path plus ``os.path.normcase`` collapses the spellings, and on
  a case-sensitive filesystem ``normcase`` is the identity -- which is
  correct there, because ``/Repo`` and ``/repo`` really are two directories.
* **A live PID is the authority, not a clock.** On the same host the holder's
  process liveness is exact and needs no heartbeat: a crashed ``neo`` frees
  the repository immediately. Only a *different* host -- a shared network
  checkout, where liveness is unknowable -- falls back to a TTL, and that TTL
  is reported, never hidden.
* **An unreadable lock is HELD, not free.** Refusing is the safe direction;
  stealing a lock we cannot read is the exact data loss this module exists to
  prevent. It is still self-healing -- an unreadable lock older than the TTL
  is taken over, and the refusal names which case it was.
* **A lock that could not be taken is never bypassed.** The caller gets a
  typed :class:`ConcurrentInstanceError` naming the other process. There is no
  "just write anyway" path, because that path is the bug.

The module is dependency-free and sits at the bottom of the project (like
:mod:`shared.security`): ``cli``, ``execution``, ``harness`` and ``runtime``
may all import it, and it imports none of them.

Note on the home directory: this module reads ``NEO_HOME``, then
``HARNESS_HOME``, then falls back to ``~/.neo``, and deliberately does NOT
import :func:`memory.paths.neo_home` -- ``shared`` is the bottom layer and
imports no other project module. The lock directory is also intentionally
independent of the artifact/log root: a ``--log-root`` override or
``HARNESS_LOGS_DIR`` must never be able to relocate a guard, because moving
the guard is how two instances stop seeing each other.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_STALE_S",
    "DEFAULT_WAIT_S",
    "INSTANCE_LOCK_SCHEMA",
    "INSTANCE_STATES",
    "LOCK_DIR_NAME",
    "MAX_WAIT_S",
    "ConcurrentInstanceError",
    "InstanceInfo",
    "InstanceLease",
    "acquire_repository_lock",
    "canonical_repository_key",
    "describe_instance",
    "instance_guard_report",
    "instance_home",
    "instance_lock_path",
    "pid_alive",
    "probe_repository_lock",
    "repository_instance_lock",
]

#: Bumped when the on-disk record's shape changes incompatibly. A record
#: carrying anything else is UNREADABLE, which is the fail-closed direction.
INSTANCE_LOCK_SCHEMA = 1

#: Directory name under the Neo home. Separate from logs, settings and
#: sessions so no artifact-root override can relocate a guard.
LOCK_DIR_NAME = "locks"

#: Cross-host TTL. Same-host liveness is authoritative and does not use this
#: number; a holder on a *different* host whose heartbeat is older than this
#: is taken over. 15 minutes survives a real long session and does not wedge
#: a shared checkout for a day. The refusal names it when it is the reason.
DEFAULT_STALE_S = 900.0

#: Refuse IMMEDIATELY by default. A caller that wants to wait says so; the
#: safe direction is not the one that blocks a terminal on a lock.
DEFAULT_WAIT_S = 0.0

#: A wait longer than this is a bug in the caller, not a request.
MAX_WAIT_S = 30.0

#: The closed vocabulary for "why is this lock in the state it is in". A
#: reason nobody defined is a receipt nobody can count.
INSTANCE_STATES = (
    "free",
    "held",
    "held_dead_owner",
    "held_expired",
    "held_unreadable",
    "held_reentrant",
    "unusable_home",
)

_GUARD_DEPTH = threading.local()


def _env_path(name: str) -> Optional[Path]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return None
    try:
        return Path(raw).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None


def instance_home(home: Any = None) -> Path:
    """Return the directory that holds instance guards.

    Precedence: explicit ``home`` argument > ``NEO_HOME`` >
    ``HARNESS_HOME`` > ``~/.neo``. Never raises; an unusable location
    surfaces as a refusal at acquisition time, not as an import error.
    """
    if home:
        try:
            return Path(home).expanduser()
        except (OSError, RuntimeError, ValueError):
            return home
    for name in ("NEO_HOME", "HARNESS_HOME"):
        override = _env_path(name)
        if override is not None:
            return override
    return Path.home() / ".neo"


def canonical_repository_key(repo: Any) -> str:
    """Return the one spelling of a repository path that identifies a tree.

    Resolves symlinks, junctions, relative components and 8.3 short names,
    then applies ``os.path.normcase``. An empty or unusable path yields
    ``""`` rather than raising: a guard cannot identify a repository it was
    handed no name for, and the caller gets a refusal instead of a traceback.
    """
    text = str(repo or "").strip()
    if not text:
        return ""
    candidate = Path(text)
    for step in (lambda p: p.expanduser(), lambda p: p.resolve()):
        try:
            candidate = step(candidate)
        except (OSError, RuntimeError, ValueError):
            pass
    try:
        return os.path.normcase(str(candidate))
    except (TypeError, ValueError):
        return ""


def instance_lock_path(repo: Any, *, home: Any = None) -> Path:
    """Return the guard file path for a repository.

    The filename is a digest of the canonical key, not the key itself: a
    repository path contains separators, drive colons and characters that are
    illegal in a Windows filename. The canonical key is stored INSIDE the
    record, where it is evidence rather than a name to parse.
    """
    key = canonical_repository_key(repo)
    digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:20]
    return instance_home(home) / LOCK_DIR_NAME / f"repo-{digest}.json"


def pid_alive(pid: Any) -> Optional[bool]:
    """Return whether ``pid`` names a live process on this host.

    ``None`` means the question could not be answered, so a caller can tell
    "the holder is dead" from "we were never told who the holder is".
    ``None`` is NOT ``False``: an unanswerable liveness question must never
    become permission to steal a lock.

    POSIX uses ``kill(pid, 0)``; ``PermissionError`` means the process exists
    but belongs to another user, which is alive. Windows waits on a
    ``SYNCHRONIZE`` handle, which distinguishes a live process from one that
    exited with code 259 -- the ``STILL_ACTIVE`` exit-code idiom cannot, and
    getting it backwards makes the guard steal a live holder's repository.
    """
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if os.name == "nt":
        return _pid_alive_windows(value)
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _pid_alive_windows(value: int) -> Optional[bool]:
    """Return process liveness on Windows.

    Uses ``WaitForSingleObject(handle, 0)`` on a ``SYNCHRONIZE`` handle.
    A process object becomes signalled when it terminates, so
    ``WAIT_TIMEOUT`` means alive and ``WAIT_OBJECT_0`` means gone, and
    ``OpenProcess`` continuing to succeed on a killed process -- which is
    normal, and is why "the open succeeded" is not a liveness answer -- does
    not affect it.

    The ``GetExitCodeProcess``/``STILL_ACTIVE`` idiom that this replaced
    cannot distinguish a live process from one that exited with code 259, and
    the first version of this function had its comparison inverted, so it
    called every live process dead and the guard happily stole a held
    repository. A gate that cannot fail is worse than no gate.

    ``None`` means the question could not be answered. The Windows constants
    are inlined rather than read from ``ctypes.wintypes`` because that module
    does not define them, and a missing name inside a ``try/except`` here
    silently becomes "unknown" -- which is how a live process once came back
    as unanswerable instead of alive.
    """
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.restype = wintypes.HANDLE
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        wait = kernel32.WaitForSingleObject
        wait.restype = wintypes.DWORD
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = open_process(synchronize, False, int(value))
        if not handle:
            # 5 == ERROR_ACCESS_DENIED: the process exists but is not ours.
            return ctypes.get_last_error() == 5
        try:
            result = int(wait(handle, 0))
        finally:
            kernel32.CloseHandle(handle)
        if result == wait_timeout:
            return True
        if result == wait_object_0:
            return False
        if result == wait_failed:
            return None
        return None
    except Exception:
        return None


def _host_name() -> str:
    try:
        return str(socket.gethostname() or "")
    except Exception:
        return ""


def _as_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return number


def _age_of(path: Path, now: float) -> float:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return 0.0


def _read_record(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return ``(record, problem)`` for one guard file.

    ``record`` is ``None`` when the file cannot be trusted and ``problem``
    names why. Never raises: an unreadable guard is a refusal, not a
    traceback in the middle of somebody's session.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, ValueError) as exc:
        return None, f"unreadable ({type(exc).__name__})"
    text = raw.strip()
    if not text:
        return None, "empty"
    try:
        payload = json.loads(text)
    except ValueError:
        return None, "not valid JSON"
    if not isinstance(payload, dict):
        return None, "not a JSON object"
    try:
        version = int(payload.get("schema_version", 0) or 0)
    except (TypeError, ValueError):
        version = -1
    if version != INSTANCE_LOCK_SCHEMA:
        return None, f"schema version {version} (expected {INSTANCE_LOCK_SCHEMA})"
    return payload, ""


@dataclass(frozen=True)
class InstanceInfo:
    """What is known about the process that holds (or held) a repository.

    ``state`` is the load-bearing field. ``held`` means a live peer owns the
    tree. ``held_dead_owner`` / ``held_expired`` mean a record exists but the
    owner is gone, and the guard is takeable. ``held_unreadable`` means we
    cannot tell, and the guard refuses. Every ``held*`` value means "not
    free"; only ``free`` does.
    """

    state: str
    lock_path: str = ""
    repository: str = ""
    pid: int = 0
    host: str = ""
    command: str = ""
    session_id: str = ""
    started_at: float = 0.0
    heartbeat_at: float = 0.0
    age_s: float = 0.0
    owner_alive: Optional[bool] = None
    detail: str = ""

    @property
    def is_free(self) -> bool:
        """True only when nothing holds the repository."""
        return self.state == "free"

    @property
    def takeable(self) -> bool:
        """True when the recorded owner is provably gone."""
        return self.state in ("held_dead_owner", "held_expired")

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible record (no token, no secrets)."""
        return {
            "state": self.state,
            "lock_path": self.lock_path,
            "repository": self.repository,
            "pid": self.pid,
            "host": self.host,
            "command": self.command,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "age_s": round(float(self.age_s), 3),
            "owner_alive": self.owner_alive,
            "detail": self.detail,
        }

    def describe(self) -> List[str]:
        """Return PLAIN lines naming the other process and the way out.

        Never markup: a repository path may contain ``[`` and a line that
        crosses into a markup parser carrying one deletes the message rather
        than printing it. This is a refusal, not a panel, so the
        anti-clutter rule (>= 3 entries per section) does not apply -- a
        refusal that rendered nothing would be a hang with better manners.
        """
        return describe_instance(self)


def _classify(
    record: Optional[Dict[str, Any]],
    problem: str,
    path: Path,
    *,
    now: float,
    stale_s: float,
    our_host: str,
    our_pid: int,
) -> InstanceInfo:
    if problem == "missing":
        return InstanceInfo(state="free", lock_path=str(path), detail="no lock file")

    if record is None:
        age = _age_of(path, now)
        detail = f"the lock file is {problem}; treated as HELD, never stolen silently"
        state = "held_unreadable"
        if age > max(1.0, stale_s):
            state = "held_expired"
            detail = (
                f"the lock file is {problem} and {int(age)}s old, past the "
                f"{int(stale_s)}s takeover budget; treated as abandoned"
            )
        return InstanceInfo(state=state, lock_path=str(path), age_s=age, detail=detail)

    try:
        holder_pid = int(record.get("pid", 0) or 0)
    except (TypeError, ValueError):
        holder_pid = 0
    holder_host = str(record.get("host", "") or "")
    started = _as_float(record.get("started_at"))
    heartbeat = _as_float(record.get("heartbeat_at")) or started
    base = {
        "lock_path": str(path),
        "repository": str(record.get("repository", "") or ""),
        "pid": holder_pid,
        "host": holder_host,
        "command": str(record.get("command", "") or ""),
        "session_id": str(record.get("session_id", "") or ""),
        "started_at": started,
        "heartbeat_at": heartbeat,
        "age_s": max(0.0, now - (heartbeat or started or now)),
    }

    if holder_pid == our_pid and holder_host == our_host:
        return InstanceInfo(
            state="held_reentrant",
            owner_alive=True,
            detail="this process already holds it",
            **base,
        )

    if holder_host and holder_host == our_host:
        alive = pid_alive(holder_pid)
        if alive is True:
            return InstanceInfo(state="held", owner_alive=True, **base)
        if alive is False:
            return InstanceInfo(
                state="held_dead_owner",
                owner_alive=False,
                detail=f"the recorded pid {holder_pid} is not running on this host",
                **base,
            )
        return InstanceInfo(
            state="held_unreadable",
            owner_alive=None,
            detail=(
                f"the recorded pid {holder_pid} could not be inspected on this host; "
                "treated as HELD"
            ),
            **base,
        )

    ttl = max(1.0, stale_s)
    if base["age_s"] > ttl:
        return InstanceInfo(
            state="held_expired",
            owner_alive=None,
            detail=(
                f"held by {holder_host or 'another host'} with no heartbeat for "
                f"{int(base['age_s'])}s, past the {int(ttl)}s takeover budget"
            ),
            **base,
        )
    return InstanceInfo(
        state="held",
        owner_alive=None,
        detail=f"held by pid {holder_pid} on {holder_host or 'another host'}",
        **base,
    )


def _read_info(
    path: Path,
    *,
    now: Optional[float],
    stale_s: float,
    our_host: Optional[str] = None,
    our_pid: Optional[int] = None,
) -> InstanceInfo:
    record, problem = _read_record(path)
    return _classify(
        record,
        problem,
        path,
        now=time.time() if now is None else float(now),
        stale_s=stale_s,
        our_host=_host_name() if our_host is None else our_host,
        our_pid=os.getpid() if our_pid is None else int(our_pid),
    )


def probe_repository_lock(
    repo: Any,
    *,
    home: Any = None,
    stale_s: float = DEFAULT_STALE_S,
    now: Optional[float] = None,
) -> InstanceInfo:
    """Report who holds a repository WITHOUT taking, creating, or deleting.

    This is the read-only half of the guard: a surface can render "another
    session is running here" without creating a lock file as a side effect of
    asking. The returned ``state`` is ``free`` when nothing holds the tree.
    """
    return _read_info(instance_lock_path(repo, home=home), now=now, stale_s=stale_s)


class ConcurrentInstanceError(RuntimeError):
    """Raised when a repository is already held by another live instance.

    Carries the :class:`InstanceInfo` so a surface can render the other
    process's pid, command and age instead of re-parsing a message. The string
    form is a single plain line: it is written to terminals, to stderr under a
    pipe, and into ``--json`` documents.
    """

    def __init__(self, message: str, info: Optional[InstanceInfo] = None) -> None:
        super().__init__(message)
        self.info = info

    def lines(self) -> List[str]:
        """Return PLAIN lines explaining the refusal and the way out."""
        if self.info is None:
            return [str(self)]
        return describe_instance(self.info)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible refusal record."""
        payload = {"error": "concurrent_instance", "message": str(self)}
        if self.info is not None:
            payload["owner"] = self.info.as_dict()
        return payload


def _describe_duration(seconds: float) -> str:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "unknown"
    if value < 90:
        return f"{int(value)}s"
    if value < 5400:
        return f"{int(value / 60)}m"
    return f"{value / 3600.0:.1f}h"


def describe_instance(info: InstanceInfo) -> List[str]:
    """Render one :class:`InstanceInfo` as PLAIN, markup-free lines.

    Names WHO holds the tree, HOW LONG they have, WHAT they were running, and
    the door out. It never says "busy": a refusal a user cannot act on is a
    hang with better manners.
    """
    if info is None or info.is_free:
        return ["free: no other instance holds this repository"]
    if info.state == "unusable_home":
        return [
            info.detail or "the guard cannot be used here; refusing to run unguarded"
        ]
    lines = [
        f"another neo instance holds this repository: pid {info.pid or '?'} "
        f"on {info.host or 'an unknown host'}"
    ]
    if info.command:
        lines.append(f"  running: {info.command}")
    if info.session_id:
        lines.append(f"  session: {info.session_id}")
    if info.started_at and info.age_s:
        lines.append(f"  held for: {_describe_duration(info.age_s)}")
    if info.state == "held_reentrant":
        lines.append("  this process already holds it; re-entering is allowed")
    elif info.state == "held_dead_owner":
        lines.append("  its process is gone, so the lock can be taken over")
    elif info.state == "held_expired":
        lines.append(f"  {info.detail}, so the lock can be taken over")
    elif info.state == "held_unreadable":
        lines.append(f"  {info.detail}")
        lines.append("  refusing to guess: a lock we cannot read is never stolen")
    else:
        lines.append("  wait for it, or stop it, then run again")
    return lines


def _depth_map() -> Dict[str, int]:
    current = getattr(_GUARD_DEPTH, "paths", None)
    if current is None:
        current = {}
        _GUARD_DEPTH.paths = current
    return current


def _write_record(path: Path, record: Dict[str, Any]) -> None:
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _delete_if_ours(path: Path, token: str) -> bool:
    """Remove the guard record only when it still carries our token."""
    record, problem = _read_record(path)
    if record is None or problem:
        with contextlib.suppress(OSError):
            path.unlink()
        return False
    if str(record.get("token", "")) != token:
        return False
    with contextlib.suppress(OSError):
        path.unlink()
    return True


def _steal_expired(path: Path) -> bool:
    """Remove a guard whose owner is provably gone, then report success.

    The caller has already established that the record is takeable (a dead
    same-host pid, or a heartbeat past the TTL). Re-reading the token and
    deleting against it means two processes racing to take over a crashed
    lock both attempt the same compare-and-delete; at most one unlink
    succeeds, and the winner is the one whose ``O_EXCL`` create then lands.
    An unreadable record is unlinked outright: it is past its takeover
    budget, so nobody is there to protect, and refusing to reclaim garbage
    would wedge a checkout forever.
    """
    record, problem = _read_record(path)
    if record is None or problem:
        removed = False
        with contextlib.suppress(OSError):
            path.unlink()
            removed = True
        return removed and not path.exists()
    with contextlib.suppress(OSError):
        path.unlink()
    return not path.exists()


@dataclass
class InstanceLease:
    """A held repository guard.

    Use it as a context manager. :meth:`release` is idempotent and never
    raises, because a failed cleanup must not turn a finished run into a
    crash. :meth:`refresh` is only needed for a CROSS-HOST lease, where the
    holder's process liveness is unknowable and the TTL is the whole contract.
    """

    path: Path
    token: str
    info: InstanceInfo
    depth: int = 1
    released: bool = False

    @property
    def lock_path(self) -> Path:
        """The guard file this lease owns."""
        return self.path

    @property
    def reentrant(self) -> bool:
        """True when an outer lease in this thread already held the guard."""
        return self.depth > 1

    def refresh(self, *, now: Optional[float] = None) -> bool:
        """Rewrite the heartbeat; return False when we no longer own the lock.

        A long cross-host session must call this. ``False`` means the record is
        gone or is somebody else's and the caller is no longer protected; the
        safe direction is to stop, not to re-create.
        """
        if self.released:
            return False
        record, problem = _read_record(self.path)
        if record is None or problem or str(record.get("token", "")) != self.token:
            return False
        record["heartbeat_at"] = time.time() if now is None else float(now)
        try:
            _write_record(self.path, record)
        except OSError:
            return False
        return True

    def release(self) -> bool:
        """Release the guard. Idempotent, never raises.

        Returns True when this call removed the record. The compare-and-delete
        on the token means a lease can only ever remove ITS OWN record, so a
        stale holder releasing late cannot delete the lock a newer instance now
        owns.
        """
        if self.released:
            return False
        self.released = True
        depths = _depth_map()
        key = str(self.path)
        if depths.get(key, 1) > 1:
            depths[key] = depths.pop(key) - 1
            return False
        depths.pop(key, None)
        return _delete_if_ours(self.path, self.token)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible record for a receipt."""
        payload = self.info.as_dict()
        payload.update(
            {
                "lock_path": str(self.path),
                "released": self.released,
                "reentrant": self.reentrant,
                "owner_pid": os.getpid(),
            }
        )
        return payload

    def __enter__(self) -> "InstanceLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def _refusal(info: InstanceInfo) -> ConcurrentInstanceError:
    headline = describe_instance(info)[0]
    if info.state == "held_reentrant":
        headline = "this process already holds the repository guard"
    return ConcurrentInstanceError(headline, info=info)


def acquire_repository_lock(
    repo: Any,
    *,
    owner: str = "",
    command: str = "",
    session_id: str = "",
    wait_s: float = DEFAULT_WAIT_S,
    stale_s: float = DEFAULT_STALE_S,
    home: Any = None,
    now: Optional[float] = None,
) -> InstanceLease:
    """Take the single-writer guard for one repository, or raise.

    This is the DETECT-AND-REFUSE half of the multi-instance contract. It
    never blocks by default: a second ``neo`` stops immediately and says who
    holds the tree, because a second instance that quietly waits is a second
    instance the user believes is working.

    ``owner`` is a free-form label for the process that took the guard (a
    module name is enough). ``command`` and ``session_id`` are what the
    refusal will show the other user, so pass them when you have them -- "pid
    4312" is much harder to act on than "pid 4312 running 'neo fix'".

    Raises :class:`ConcurrentInstanceError` when a live peer holds the
    repository or when the guard location is unusable. A dead owner's lock IS
    taken over; an unreadable one is not.
    """
    key = canonical_repository_key(repo)
    if not key:
        info = InstanceInfo(
            state="unusable_home",
            detail="no repository path was supplied, so there is no work tree to guard",
        )
        raise _refusal(info)

    path = instance_lock_path(repo, home=home)
    wait = min(max(0.0, float(wait_s)), MAX_WAIT_S)
    deadline = time.monotonic() + wait
    token = uuid.uuid4().hex

    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = _read_info(path, now=now, stale_s=stale_s)
            if info.state == "held_reentrant":
                depths = _depth_map()
                depths[str(path)] = depths.get(str(path), 1) + 1
                record, _ = _read_record(path)
                return InstanceLease(
                    path=path,
                    token=str((record or {}).get("token", "")),
                    info=info,
                    depth=depths[str(path)],
                )
            if info.takeable and _steal_expired(path):
                continue
            if time.monotonic() >= deadline:
                raise _refusal(info) from None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            continue
        except OSError as exc:
            info = InstanceInfo(
                state="unusable_home",
                lock_path=str(path),
                detail=f"the guard location is unusable ({type(exc).__name__}); refusing to run unguarded",
            )
            raise _refusal(info) from None

        started = time.time() if now is None else float(now)
        record = {
            "schema_version": INSTANCE_LOCK_SCHEMA,
            "repository": key,
            "pid": os.getpid(),
            "host": _host_name(),
            "owner": str(owner or "")[:200],
            "command": str(command or "")[:200],
            "session_id": str(session_id or "")[:120],
            "started_at": started,
            "heartbeat_at": started,
            "token": token,
        }
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            with contextlib.suppress(OSError):
                path.unlink()
            info = InstanceInfo(
                state="unusable_home",
                lock_path=str(path),
                detail=f"the guard record could not be written ({type(exc).__name__})",
            )
            raise _refusal(info) from None

        _depth_map()[str(path)] = 1
        return InstanceLease(
            path=path,
            token=token,
            info=_classify(
                record,
                "",
                path,
                now=started,
                stale_s=stale_s,
                our_host=record["host"],
                our_pid=record["pid"],
            ),
        )


def repository_instance_lock(repo: Any, **kwargs: Any):
    """Return a context manager holding the guard for the duration of a block.

    ``acquire_repository_lock`` is the function; this is the shape a caller
    almost always wants, so a guard is never acquired and forgotten. The
    keyword arguments are exactly ``acquire_repository_lock``'s.
    """
    return _RepositoryLockContext(repo, kwargs)


class _RepositoryLockContext:
    """The context manager behind :func:`repository_instance_lock`."""

    def __init__(self, repo: Any, options: Dict[str, Any]) -> None:
        self._repo = repo
        self._options = options
        self._lease: Optional[InstanceLease] = None

    def __enter__(self) -> InstanceLease:
        self._lease = acquire_repository_lock(self._repo, **self._options)
        return self._lease

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._lease is not None:
            self._lease.release()
        self._lease = None


def instance_guard_report(
    repo: Any,
    *,
    home: Any = None,
    stale_s: float = DEFAULT_STALE_S,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Return the read-only guard receipt for a repository.

    A surface asks this to render "another session is running here" without
    taking a lock, and a test asserts on it. ``free`` means nothing holds the
    tree; every ``held*`` state means a second writer would be refused or
    could take over, and the report says which.
    """
    key = canonical_repository_key(repo)
    info = probe_repository_lock(repo, home=home, stale_s=stale_s, now=now)
    return {
        "schema_version": INSTANCE_LOCK_SCHEMA,
        "repository": key,
        "lock_path": info.lock_path or str(instance_lock_path(repo, home=home)),
        "state": info.state,
        "free": info.is_free,
        "refuse": not info.is_free
        and not info.takeable
        and info.state != "held_reentrant",
        "takeable": info.takeable,
        "owner": None if info.is_free else info.as_dict(),
        "stale_after_s": float(stale_s),
        "lines": info.describe(),
    }
