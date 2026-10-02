"""Safe repository identity, mutation journaling, leases, and undo.

The execution boundary uses this module for interactive workspace mutations.
Docker verification remains in :mod:`execution.sandbox`; this module never
falls back from a sandbox failure to a host command. State is kept outside
the repository by default and journals hashes rather than raw file contents.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import dataclasses
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import urlencode, urlparse

from execution.ingress import OUTPUT_CAP_BYTES, seal_mapping, seal_output
from shared.security import scrub_environment
from shared.types import ExecutionResult

__all__ = [
    "ApprovalGrant",
    "ApprovalResponse",
    "ApprovalStore",
    "BackgroundProcess",
    "CancellationToken",
    "EditResult",
    "ExecutionProfile",
    "FileRevision",
    "LocalExecutionHandle",
    "LocalExecutionResult",
    "MCPChildRegistry",
    "MutationJournal",
    "MutationLease",
    "MutationRecord",
    "NativeSandboxUnavailableError",
    "PermissionDecision",
    "PermissionRule",
    "PolicyContext",
    "ProcessManager",
    "ResourceLimits",
    "SafeToolBackend",
    "ToolPolicy",
    "ToolPolicyEngine",
    "ToolResult",
    "UndoResult",
    "Workspace",
    "WorkspaceConflictError",
    "WorkspaceCrash",
    "WorkspaceEditError",
    "WorkspaceError",
    "WorkspaceExecutionError",
    "WorkspaceIdentity",
    "WorkspaceJournal",
    "WorkspaceLease",
    "WorkspaceLeaseError",
    "WorkspaceSecurityError",
    "WorkspaceStateError",
    "WorkspaceUndoConflictError",
    "acquire_lease",
    "acquire_mutation_lease",
    "apply_exact_edit",
    "apply_patch",
    "canonical_repo_root",
    "canonical_workspace_id",
    "capture_workspace_identity",
    "cleanup_active_processes",
    "describe_tool",
    "execute_local",
    "execute_typed_tool",
    "identify_workspace",
    "is_generated_path",
    "is_protected_path",
    "native_sandbox_available",
    "normalize_relative_path",
    "open_workspace",
    "policy_for_tool",
    "run_local",
    "scrub_env",
    "start_local_execution",
    "tool_policy",
]

PathLike = Union[str, os.PathLike[str]]


class WorkspaceError(RuntimeError):
    """Base class for workspace safety failures."""


class WorkspaceSecurityError(WorkspaceError):
    """A path or operation violates the workspace safety boundary."""


class WorkspaceEditError(WorkspaceError):
    """An edit is malformed, ambiguous, stale, or not applicable."""


class WorkspaceConflictError(WorkspaceEditError):
    """The current file state conflicts with an expected precondition."""


class WorkspaceUndoConflictError(WorkspaceConflictError):
    """Undo cannot proceed because the file is no longer agent-owned."""


class WorkspaceLeaseError(WorkspaceError):
    """A mutation lease is held by another live owner or is invalid."""


class WorkspaceStateError(WorkspaceError):
    """Persisted workspace state is incompatible with the repository."""


class WorkspaceExecutionError(WorkspaceError):
    """A local or sandboxed process could not be started."""


class NativeSandboxUnavailableError(WorkspaceExecutionError):
    """The requested native OS sandbox profile is unavailable."""


class WorkspaceCrash(WorkspaceError):
    """A deliberate crash hook fired during an atomic mutation."""


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _same_revision(actual: FileRevision, expected: FileRevision) -> bool:
    if actual.exists != expected.exists or actual.sha256 != expected.sha256:
        return False
    if expected.kind not in {"", "missing"} and actual.kind != expected.kind:
        return False
    return expected.mode is None or os.name == "nt" or actual.mode == expected.mode


def _lease_paths_overlap(left: str, right: str) -> bool:
    if left == "*" or right == "*":
        return True
    first = str(left).replace("\\", "/").strip("/")
    second = str(right).replace("\\", "/").strip("/")
    return bool(
        first == second
        or first.startswith(second + "/")
        or second.startswith(first + "/")
    )


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _atomic_write_bytes(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.neo-", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(str(temp_path), str(path))
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    _atomic_write_bytes(path, payload.encode("utf-8"))


@contextlib.contextmanager
def _locked_file(path: Path) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(" ")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        handle.close()


def _assign_windows_job(process: subprocess.Popen[Any]) -> Optional[Any]:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class LimitInfo(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInfo(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", LimitInfo),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        configured = kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(process._handle))
        )
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _close_windows_job(job: Any) -> None:
    if os.name != "nt" or job is None:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.TerminateJobObject(job, 1)
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(job)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, ImportError):
        return False


def normalize_relative_path(path: PathLike) -> str:
    """Return a strict repository-relative POSIX path.

    Absolute paths, drive-relative paths, UNC paths, control characters, and
    any ``..`` component are rejected instead of normalized.
    """
    raw = os.fspath(path).replace("\\", "/")
    if not raw or "\x00" in raw or any(ord(char) < 32 for char in raw):
        raise WorkspaceSecurityError(f"invalid workspace path: {path!r}")
    if any(char in raw for char in '<>:"|?*$`();&'):
        raise WorkspaceSecurityError(f"shell or device path refused: {path!r}")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise WorkspaceSecurityError(f"workspace path must be relative: {path!r}")
    parts: List[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspaceSecurityError(f"workspace path traversal refused: {path!r}")
        parts.append(part)
    if not parts:
        raise WorkspaceSecurityError("workspace path must name a file")
    return "/".join(parts)


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _assert_no_symlink_components(root: Path, relative: str) -> Path:
    normalized = normalize_relative_path(relative)
    root = root.resolve(strict=True)
    current = root
    for part in normalized.split("/"):
        current = current / part
        try:
            if current.is_symlink():
                raise WorkspaceSecurityError(
                    f"symbolic-link path refused: {normalized}"
                )
        except OSError as exc:
            raise WorkspaceSecurityError(
                f"could not inspect workspace path {normalized!r}: {exc}"
            ) from exc
    candidate = (root / normalized).resolve(strict=False)
    if not _path_within(candidate, root):
        raise WorkspaceSecurityError(f"workspace path escapes repository: {relative!r}")
    return candidate


def _safe_path(root: Path, relative: PathLike) -> Tuple[str, Path]:
    normalized = normalize_relative_path(relative)
    return normalized, _assert_no_symlink_components(root, normalized)


def _assert_no_symlink_components_in_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise WorkspaceSecurityError(
                f"workspace state path is symlinked: {current}"
            )
    return absolute


_SECRET_TEXT = re.compile(
    r"(?:sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|"
    r"(?:api[_-]?key|access[_-]?token|secret[_-]?access[_-]?key|password)\s*[:=]\s*[^\s]{12,}|"
    r"://[^\s/:@]+:[^\s/@]+@)",
    re.IGNORECASE,
)

_DEFAULT_PROTECTED_PATTERNS = (
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "credentials*",
    "secrets*",
    ".netrc",
    ".pypirc",
    "auth.json",
    "service-account*.json",
)


def _is_artifact(relative: str) -> bool:
    parts = relative.split("/")
    names = {part.lower() for part in parts}
    if names.intersection(
        {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            ".hypothesis",
            ".cache",
            "node_modules",
        }
    ):
        return True
    name = parts[-1].lower()
    return (
        name == ".coverage"
        or name.startswith(".coverage.")
        or name.endswith((".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".dylib"))
    )


def is_generated_path(path: PathLike) -> bool:
    """True when ``path`` names generated output or VCS metadata.

    This is the EXISTING artifact/VCS matcher (the same ``_is_artifact`` set
    and the same VCS directory names :func:`is_protected_path` refuses) exposed
    as a predicate a bulk tree walk can use. It exists because
    :func:`is_protected_path` is a *refusal* API: it raises
    :class:`WorkspaceSecurityError` on an absolute, traversing, or
    shell-shaped path, which is correct for a single tool call and wrong for a
    copier walking a hostile tree where the safe answer to a malformed name is
    "skip it".

    Assumes nothing about the input beyond it being convertible to a string.
    Never raises: an empty, absolute, traversing, control-character, or
    otherwise unusable value answers ``True`` (do not copy it). It does NOT
    consult configured secret patterns - use :func:`is_protected_path` for that.

    The counterpart to the VCS half of :func:`is_protected_path` is spelled
    out here rather than derived from it, because that function's VCS refusal
    is unconditional while this predicate is a walk-time filter.
    """
    try:
        raw = os.fspath(path)
    except TypeError:
        return True
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - decode with replace cannot raise
            return True
    text = str(raw).replace("\\", "/").strip()
    if not text or "\x00" in text or any(ord(char) < 32 for char in text):
        return True
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return True
    if any(part == ".." for part in text.split("/")):
        return True
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return True
    if any(part.lower() in {".git", ".hg", ".svn"} for part in parts):
        return True
    return _is_artifact("/".join(parts))


def is_protected_path(path: PathLike, protected_patterns: Sequence[str] = ()) -> bool:
    """Return whether a relative path is protected from tool access."""
    relative = normalize_relative_path(path)
    parts = relative.split("/")
    lowered = [part.lower() for part in parts]
    if any(part in {".git", ".hg", ".svn"} for part in lowered):
        return True
    if _is_artifact(relative):
        return True
    basename = lowered[-1]
    patterns = (
        *_DEFAULT_PROTECTED_PATTERNS,
        *(str(item) for item in protected_patterns or ()),
    )
    for pattern in patterns:
        text = str(pattern).replace("\\", "/").lower()
        if fnmatch.fnmatchcase(relative.lower(), text) or fnmatch.fnmatchcase(
            basename, text
        ):
            return True
        if any(fnmatch.fnmatchcase(part, text) for part in lowered[:-1]):
            return True
    return False


@dataclass(frozen=True)
class FileRevision:
    """Content and metadata revision of one repository path."""

    exists: bool = False
    sha256: Optional[str] = None
    size: int = 0
    mode: Optional[int] = None
    kind: str = "missing"

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FileRevision":
        return cls(
            exists=bool(value.get("exists", False)),
            sha256=value.get("sha256"),
            size=int(value.get("size", 0) or 0),
            mode=value.get("mode"),
            kind=str(value.get("kind", "missing")),
        )

    @property
    def hash(self) -> Optional[str]:
        return self.sha256


@dataclass(frozen=True)
class WorkspaceIdentity:
    """Canonical repository identity captured before agent mutations."""

    root: str
    revision: Optional[str]
    branch: Optional[str]
    dirty: bool
    dirty_paths: Tuple[str, ...]
    git_repo: bool
    captured_at: float
    device: Optional[int] = None
    inode: Optional[int] = None

    @property
    def canonical_root(self) -> str:
        return self.root

    @property
    def git_revision(self) -> Optional[str]:
        return self.revision

    @property
    def git_branch(self) -> Optional[str]:
        return self.branch

    @property
    def pre_existing_dirty(self) -> bool:
        return self.dirty

    @property
    def dirty_state(self) -> bool:
        return self.dirty

    @property
    def pre_existing_dirty_paths(self) -> Tuple[str, ...]:
        return self.dirty_paths

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "revision": self.revision,
            "branch": self.branch,
            "dirty": self.dirty,
            "dirty_paths": list(self.dirty_paths),
            "git_repo": self.git_repo,
            "captured_at": self.captured_at,
            "device": self.device,
            "inode": self.inode,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceIdentity":
        return cls(
            root=str(value["root"]),
            revision=value.get("revision"),
            branch=value.get("branch"),
            dirty=bool(value.get("dirty", False)),
            dirty_paths=tuple(str(item) for item in value.get("dirty_paths", ())),
            git_repo=bool(value.get("git_repo", False)),
            captured_at=float(value.get("captured_at", 0.0)),
            device=value.get("device"),
            inode=value.get("inode"),
        )


def _git_environment() -> Dict[str, str]:
    environment = scrub_environment(isolated=False)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_COUNT"] = "3"
    environment["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    environment["GIT_CONFIG_VALUE_0"] = os.devnull
    environment["GIT_CONFIG_KEY_1"] = "core.fsmonitor"
    environment["GIT_CONFIG_VALUE_1"] = "false"
    environment["GIT_CONFIG_KEY_2"] = "diff.external"
    environment["GIT_CONFIG_VALUE_2"] = ""
    return environment


def _git_output(
    root: Path, args: Sequence[str], timeout_s: float = 10.0
) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def canonical_repo_root(repo_path: PathLike) -> Path:
    """Return the canonical git root containing ``repo_path``."""
    raw = Path(repo_path).expanduser()
    if not raw.is_dir():
        raise WorkspaceSecurityError(f"workspace is not a directory: {repo_path}")
    resolved = raw.resolve(strict=True)
    top = _git_output(resolved, ["rev-parse", "--show-toplevel"])
    if top:
        candidate = Path(top).expanduser()
        if candidate.is_dir():
            candidate = candidate.resolve(strict=True)
            if not _path_within(resolved, candidate):
                return resolved
            try:
                home = Path.home().resolve(strict=True)
            except OSError:
                home = None
            if (
                home is not None
                and (candidate == home or home.is_relative_to(candidate))
                and not (resolved / ".git").exists()
            ):
                return resolved
            return candidate
    return resolved


def _status_paths(root: Path) -> Tuple[bool, Tuple[str, ...]]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=true",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ()
    if completed.returncode != 0:
        return False, ()
    tokens = completed.stdout.split("\x00")
    paths: List[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        code = token[:2]
        path = token[3:]
        if code and code[0] in {"R", "C"} and index < len(tokens):
            if tokens[index]:
                paths.append(tokens[index])
            index += 1
        if path:
            paths.append(path)
    unique = tuple(sorted({item.replace("\\", "/") for item in paths if item}))
    return bool(unique), unique


def capture_workspace_identity(repo_path: PathLike) -> WorkspaceIdentity:
    """Capture canonical root, git revision/branch, and dirty baseline."""
    root = canonical_repo_root(repo_path)
    inside = _git_output(root, ["rev-parse", "--is-inside-work-tree"])
    top = _git_output(root, ["rev-parse", "--show-toplevel"])
    try:
        top_path = Path(top).resolve(strict=True) if top else None
    except OSError:
        top_path = None
    git_repo = inside == "true" and top_path == root
    revision = _git_output(root, ["rev-parse", "HEAD"]) if git_repo else None
    branch = _git_output(root, ["branch", "--show-current"]) if git_repo else None
    if not branch:
        branch = _git_output(root, ["symbolic-ref", "--short", "-q", "HEAD"])
    dirty, dirty_paths = _status_paths(root) if git_repo else (False, ())
    try:
        info = root.stat()
        device = int(getattr(info, "st_dev", 0)) or None
        inode = int(getattr(info, "st_ino", 0)) or None
    except OSError:
        device = inode = None
    return WorkspaceIdentity(
        root=str(root),
        revision=revision,
        branch=branch,
        dirty=dirty,
        dirty_paths=dirty_paths,
        git_repo=git_repo,
        captured_at=_now(),
        device=device,
        inode=inode,
    )


identify_workspace = capture_workspace_identity


@dataclass
class MutationRecord:
    """One auditable file mutation or undo operation."""

    operation_id: str
    workspace_id: str
    path: str
    kind: str
    owner: str
    lease_id: str
    before: FileRevision
    after: FileRevision
    proposed_effect: Dict[str, Any] = field(default_factory=dict)
    status: str = "proposed"
    hunk_id: Optional[str] = None
    created_at: float = field(default_factory=_now)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        before = self.before.to_dict()
        after = self.after.to_dict()
        value: Dict[str, Any] = {
            "event": "mutation",
            "operation_id": self.operation_id,
            "workspace_id": self.workspace_id,
            "path": self.path,
            "kind": self.kind,
            "owner": self.owner,
            "lease_id": self.lease_id,
            "before": before,
            "after": after,
            "proposed_effect": dict(self.proposed_effect),
            "status": self.status,
            "created_at": self.created_at,
            "pre_hash": before.get("sha256"),
            "post_hash": after.get("sha256"),
            "before_hash": before.get("sha256"),
            "after_hash": after.get("sha256"),
        }
        if self.hunk_id is not None:
            value["hunk_id"] = self.hunk_id
        if self.error is not None:
            value["error"] = self.error
        return value

    @property
    def pre_hash(self) -> Optional[str]:
        return self.before.sha256

    @property
    def post_hash(self) -> Optional[str]:
        return self.after.sha256

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MutationRecord":
        before = value.get("before") or value.get("pre_revision") or {}
        after = value.get("after") or value.get("post_revision") or {}
        return cls(
            operation_id=str(value.get("operation_id") or value.get("op_id") or ""),
            workspace_id=str(value.get("workspace_id") or ""),
            path=str(value.get("path") or ""),
            kind=str(value.get("kind") or "edit"),
            owner=str(value.get("owner") or ""),
            lease_id=str(value.get("lease_id") or ""),
            before=FileRevision.from_dict(before),
            after=FileRevision.from_dict(after),
            proposed_effect=dict(value.get("proposed_effect") or {}),
            status=str(value.get("status") or "proposed"),
            hunk_id=value.get("hunk_id"),
            created_at=float(value.get("created_at") or value.get("ts") or 0.0),
            error=value.get("error"),
        )


class WorkspaceJournal:
    """Append-only, fsynced JSONL journal for workspace operations."""

    def __init__(self, path: PathLike, workspace_id: str = "") -> None:
        candidate = Path(path)
        self.path = candidate / "journal.jsonl" if candidate.is_dir() else candidate
        self.workspace_id = workspace_id
        self._malformed_interior = 0

    def append(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        """Append one event durably and return the stored event."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked_file(self.path.with_name(self.path.name + ".lock")):
            was_new = not self.path.exists()
            record = dict(event)
            record.setdefault("ts", _now())
            record.setdefault("seq", self._last_seq_unlocked() + 1)
            record.setdefault("workspace_id", self.workspace_id)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            if was_new:
                try:
                    directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                except OSError:
                    directory_fd = -1
                if directory_fd >= 0:
                    try:
                        os.fsync(directory_fd)
                    except OSError:
                        pass
                    finally:
                        os.close(directory_fd)
            return record

    def _last_seq_unlocked(self) -> int:
        maximum = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                        maximum = max(maximum, int(value.get("seq", 0)))
                    except (ValueError, TypeError):
                        continue
        except OSError:
            return 0
        return maximum

    def read_events(self) -> List[Dict[str, Any]]:
        """Read valid events, tolerating only a torn final journal line."""
        events: List[Dict[str, Any]] = []
        self._malformed_interior = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        for index, line in enumerate(lines):
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                if index == len(lines) - 1 and not line.endswith("\n"):
                    continue
                self._malformed_interior += 1
                continue
            if isinstance(value, dict):
                events.append(value)
            else:
                self._malformed_interior += 1
        return events

    @property
    def has_interior_corruption(self) -> bool:
        """Whether a non-final journal line could not be decoded."""
        self.read_events()
        return self._malformed_interior > 0

    @property
    def events(self) -> List[Dict[str, Any]]:
        return self.read_events()

    def records(self) -> List[MutationRecord]:
        """Return the latest record for each operation in journal order."""
        latest: Dict[str, MutationRecord] = {}
        order: List[str] = []
        for event in self.read_events():
            if event.get("event") != "mutation":
                continue
            try:
                record = MutationRecord.from_dict(event)
            except (TypeError, ValueError, KeyError):
                continue
            if not record.operation_id:
                continue
            if self.workspace_id and record.workspace_id != self.workspace_id:
                continue
            if record.operation_id not in latest:
                order.append(record.operation_id)
            latest[record.operation_id] = record
        return [latest[operation_id] for operation_id in order]


MutationJournal = WorkspaceJournal


@dataclass(frozen=True)
class WorkspaceLease:
    """A renewable, token-checked mutation lease for a workspace or path set."""

    workspace: "Workspace"
    owner: str
    lease_id: str
    paths: Tuple[str, ...]
    state_path: Path
    created_at: float
    expires_at: float
    heartbeat_s: Optional[float] = None
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)

    def _valid_value(self, value: Mapping[str, Any]) -> bool:
        return bool(
            value.get("lease_id") == self.lease_id
            and value.get("owner") == self.owner
            and float(value.get("expires_at", 0)) > _now()
        )

    def assert_valid(self) -> None:
        """Raise if this lease was released, expired, or replaced."""
        value = _read_json(self.state_path, {})
        if not self._valid_value(value):
            raise WorkspaceLeaseError("workspace mutation lease is no longer valid")

    def renew(self, ttl_s: Optional[float] = None) -> bool:
        """Renew this lease only while its token still owns the record."""
        duration = float(
            self.heartbeat_s * 3
            if self.heartbeat_s and ttl_s is None
            else 300.0
            if ttl_s is None
            else ttl_s
        )
        with _locked_file(self.state_path.with_name(self.state_path.name + ".lock")):
            value = _read_json(self.state_path, {})
            if not self._valid_value(value):
                return False
            value["expires_at"] = _now() + max(1.0, duration)
            _atomic_json(self.state_path, value)
        self.expires_at = float(value["expires_at"])
        return True

    def release(self) -> bool:
        """Release only this token; a replacement lease is never removed."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with _locked_file(self.state_path.with_name(self.state_path.name + ".lock")):
            value = _read_json(self.state_path, {})
            if value.get("lease_id") != self.lease_id:
                return False
            try:
                self.state_path.unlink()
            except FileNotFoundError:
                return False
        return True

    def __enter__(self) -> "WorkspaceLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    @property
    def active(self) -> bool:
        return self._valid_value(_read_json(self.state_path, {}))


MutationLease = WorkspaceLease


@dataclass
class EditResult:
    """Result of one exact, durably journaled mutation."""

    operation_id: str
    path: str
    replacements: int
    before: FileRevision
    after: FileRevision
    created: bool = False
    hunk_id: Optional[str] = None
    effect: Dict[str, Any] = field(default_factory=dict)

    @property
    def created_by_agent(self) -> bool:
        return self.created

    @property
    def pre_hash(self) -> Optional[str]:
        return self.before.sha256

    @property
    def post_hash(self) -> Optional[str]:
        return self.after.sha256

    def to_dict(self) -> Dict[str, Any]:
        value = {
            "operation_id": self.operation_id,
            "path": self.path,
            "replacements": self.replacements,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "pre_hash": self.pre_hash,
            "post_hash": self.post_hash,
            "created": self.created,
            "effect": dict(self.effect),
        }
        if self.hunk_id is not None:
            value["hunk_id"] = self.hunk_id
        return value

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


@dataclass
class UndoResult(dict):
    """Dictionary-compatible undo result with conflict details."""

    restored: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, str]] = field(default_factory=list)
    undone: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._sync_dict()

    def _sync_dict(self) -> None:
        super().clear()
        super().update(
            restored=list(self.restored),
            deleted=list(self.deleted),
            missing=list(self.missing),
            conflicts=list(self.conflicts),
            undone=list(self.undone),
        )


def _patch_path(value: str) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text == "/dev/null":
        return None
    if text.startswith(("a/", "b/")):
        text = text[2:]
    return normalize_relative_path(text)


def _parse_unified_patch(patch: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse a bounded standard unified diff into per-file hunks."""
    if not isinstance(patch, str) or not patch.strip():
        raise WorkspaceEditError("patch must be non-empty text")
    lines = patch.splitlines(keepends=True)
    files: Dict[str, List[Dict[str, Any]]] = {}
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise WorkspaceEditError("patch file header is incomplete")
        source_path = _patch_path(lines[index][4:].split("\t", 1)[0])
        destination_path = _patch_path(lines[index + 1][4:].split("\t", 1)[0])
        path = destination_path or source_path
        if path is None:
            raise WorkspaceEditError("patch file path is missing")
        index += 2
        hunks: List[Dict[str, Any]] = []
        while index < len(lines) and not lines[index].startswith("--- "):
            if not lines[index].startswith("@@ "):
                index += 1
                continue
            match = re.match(
                r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@",
                lines[index].rstrip("\r\n"),
            )
            if match is None:
                raise WorkspaceEditError(f"invalid hunk header for {path}")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            index += 1
            old_lines: List[str] = []
            new_lines: List[str] = []
            while index < len(lines):
                line = lines[index]
                if line.startswith("@@ ") or line.startswith("--- "):
                    break
                if line.startswith("\\ No newline at end of file"):
                    index += 1
                    continue
                if not line:
                    raise WorkspaceEditError(
                        f"unprefixed blank line in patch for {path}"
                    )
                prefix, content = line[0], line[1:]
                if prefix == " ":
                    old_lines.append(content)
                    new_lines.append(content)
                elif prefix == "-":
                    old_lines.append(content)
                elif prefix == "+":
                    new_lines.append(content)
                else:
                    raise WorkspaceEditError(f"invalid hunk line for {path}")
                index += 1
            if len(old_lines) != old_count or len(new_lines) != new_count:
                raise WorkspaceEditError(
                    f"hunk line count mismatch for {path}:{old_start}"
                )
            hunks.append(
                {
                    "old_start": old_start,
                    "new_start": new_start,
                    "old": "".join(old_lines),
                    "new": "".join(new_lines),
                }
            )
        if not hunks:
            raise WorkspaceEditError(f"patch contains no hunks for {path}")
        if path in files:
            raise WorkspaceEditError(f"patch contains duplicate file: {path}")
        files[path] = hunks
    if not files:
        raise WorkspaceEditError("patch contains no file headers")
    return files


class Workspace:
    """A canonical repository plus durable safety state outside the repo."""

    def __init__(
        self,
        repo_path: PathLike,
        state_dir: Optional[PathLike] = None,
        *,
        owner: Optional[str] = None,
        protected_paths: Sequence[str] = (),
        lease_ttl_s: float = 300.0,
        max_file_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.identity = capture_workspace_identity(repo_path)
        self.root = Path(self.identity.root)
        self.owner = owner or f"pid:{os.getpid()}"
        self.protected_paths = tuple(str(item) for item in protected_paths)
        self.lease_ttl_s = max(1.0, float(lease_ttl_s))
        self.max_file_bytes = max(1, int(max_file_bytes))
        if state_dir is None:
            configured = os.environ.get("NEO_WORKSPACE_STATE_DIR")
            base = (
                Path(configured)
                if configured
                else Path(tempfile.gettempdir()) / "neo-workspace"
            )
        else:
            base = Path(state_dir).expanduser()
        base = _assert_no_symlink_components_in_path(base)
        self.state_relocated = _path_within(base, self.root)
        scope = f"|state:{base}" if self.state_relocated else ""
        material = f"{self.root}|{self.identity.device}|{self.identity.inode}{scope}"
        self.workspace_id = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        if self.state_relocated:
            base = Path(tempfile.gettempdir()) / "neo-workspace-external"
        self.state_dir = base / self.workspace_id
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_dir.is_symlink():
            raise WorkspaceSecurityError("workspace state directory is symlinked")
        self.journal = WorkspaceJournal(
            self.state_dir / "journal.jsonl", self.workspace_id
        )
        if self.journal.has_interior_corruption:
            raise WorkspaceStateError("workspace journal contains interior corruption")
        self.baseline_path = self.state_dir / "baseline.json"
        self.lease_path = self.state_dir / "lease.json"
        if self.journal.path.is_symlink():
            raise WorkspaceSecurityError("workspace journal is symlinked")
        if self.baseline_path.is_symlink() or self.lease_path.is_symlink():
            raise WorkspaceSecurityError("workspace state file is symlinked")
        self._local = threading.local()
        self._baseline = self._load_or_create_baseline()
        self.recover()

    @classmethod
    def open(
        cls,
        repo_path: PathLike,
        state_dir: Optional[PathLike] = None,
        **kwargs: Any,
    ) -> "Workspace":
        """Open or create a workspace using durable state."""
        return cls(repo_path, state_dir, **kwargs)

    def _load_or_create_baseline(self) -> Dict[str, Any]:
        existing = _read_json(self.baseline_path, None)
        if self.baseline_path.exists() and not isinstance(existing, dict):
            raise WorkspaceStateError("workspace baseline is corrupt")
        if isinstance(existing, dict):
            if existing.get("workspace_id") != self.workspace_id:
                raise WorkspaceStateError(
                    "workspace state belongs to another repository"
                )
            if existing.get("root") != self.identity.root:
                raise WorkspaceStateError(
                    "workspace baseline root does not match repository"
                )
            previous_identity = existing.get("identity")
            if isinstance(previous_identity, dict):
                if previous_identity.get("revision") != self.identity.revision:
                    raise WorkspaceStateError(
                        "workspace baseline git revision does not match repository"
                    )
                if previous_identity.get("branch") != self.identity.branch:
                    raise WorkspaceStateError(
                        "workspace baseline git branch does not match repository"
                    )
            if not isinstance(existing.get("files"), dict):
                raise WorkspaceStateError("workspace baseline has no file revisions")
            return existing
        with _locked_file(self.state_dir / "baseline.lock"):
            existing = _read_json(self.baseline_path, None)
            if isinstance(existing, dict):
                if existing.get("workspace_id") != self.workspace_id:
                    raise WorkspaceStateError(
                        "workspace state belongs to another repository"
                    )
                if existing.get("root") != self.identity.root:
                    raise WorkspaceStateError(
                        "workspace baseline root does not match repository"
                    )
                previous_identity = existing.get("identity")
                if isinstance(previous_identity, dict):
                    if previous_identity.get("revision") != self.identity.revision:
                        raise WorkspaceStateError(
                            "workspace baseline git revision does not match repository"
                        )
                    if previous_identity.get("branch") != self.identity.branch:
                        raise WorkspaceStateError(
                            "workspace baseline git branch does not match repository"
                        )
                return existing
            baseline = {
                "workspace_id": self.workspace_id,
                "root": self.identity.root,
                "identity": self.identity.to_dict(),
                "files": self._scan_revisions(),
            }
            _atomic_json(self.baseline_path, baseline)
            return baseline

    def _scan_revisions(self) -> Dict[str, Dict[str, Any]]:
        files: Dict[str, Dict[str, Any]] = {}
        if not self.root.is_dir():
            return files
        for directory, dirs, names in os.walk(
            self.root, topdown=True, followlinks=False
        ):
            dirs[:] = [
                name
                for name in dirs
                if name not in {".git", ".hg", ".svn", "__pycache__", ".pytest_cache"}
            ]
            for name in names:
                path = Path(directory) / name
                try:
                    relative = path.relative_to(self.root).as_posix()
                    if path.is_symlink() or not path.is_file():
                        continue
                    if path.stat().st_size > self.max_file_bytes:
                        continue
                    revision = self._revision(path)
                except (OSError, ValueError):
                    continue
                files[relative] = revision.to_dict()
        return files

    def _revision(self, path: Path) -> FileRevision:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return FileRevision()
        except OSError as exc:
            raise WorkspaceSecurityError(f"cannot inspect {path}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            try:
                target = os.readlink(path)
            except OSError as exc:
                raise WorkspaceSecurityError(f"cannot inspect symlink: {exc}") from exc
            return FileRevision(
                True,
                _sha256_bytes(target.encode("utf-8")),
                0,
                stat.S_IMODE(info.st_mode),
                "symlink",
            )
        if not stat.S_ISREG(info.st_mode):
            return FileRevision(
                True, None, int(info.st_size), stat.S_IMODE(info.st_mode), "special"
            )
        if info.st_size > self.max_file_bytes:
            raise WorkspaceSecurityError(
                f"file exceeds workspace mutation limit: {path}"
            )
        try:
            digest = _sha256_path(path)
        except OSError as exc:
            raise WorkspaceSecurityError(f"cannot hash {path}: {exc}") from exc
        return FileRevision(
            True,
            digest,
            int(info.st_size),
            stat.S_IMODE(info.st_mode),
            "file",
        )

    def _path(self, relative: PathLike) -> Tuple[str, Path]:
        normalized, full = _safe_path(self.root, relative)
        if is_protected_path(normalized, self.protected_paths):
            raise WorkspaceSecurityError(f"protected path refused: {normalized}")
        return normalized, full

    def _path_for_read(self, relative: PathLike) -> Tuple[str, Path]:
        normalized, full = _safe_path(self.root, relative)
        if is_protected_path(normalized, self.protected_paths):
            raise WorkspaceSecurityError(f"protected path refused: {normalized}")
        return normalized, full

    def revision(self, relative: PathLike) -> FileRevision:
        """Return the current content revision of a repository-relative path."""
        _relative, full = self._path_for_read(relative)
        return self._revision(full)

    file_revision = revision

    def read_bytes(self, relative: PathLike) -> bytes:
        """Read a regular file without following a symlink."""
        normalized, full = self._path_for_read(relative)
        if not full.is_file() or full.is_symlink():
            raise WorkspaceEditError(f"not a regular file: {normalized}")
        try:
            if full.stat().st_size > self.max_file_bytes:
                raise WorkspaceSecurityError(
                    f"file exceeds workspace read limit: {normalized}"
                )
            return full.read_bytes()
        except OSError as exc:
            raise WorkspaceEditError(f"cannot read {normalized}: {exc}") from exc

    def read_text(self, relative: PathLike, *, encoding: str = "utf-8") -> str:
        """Read strict text; malformed or binary content is refused."""
        data = self.read_bytes(relative)
        if b"\x00" in data:
            raise WorkspaceSecurityError(f"binary file refused: {relative}")
        try:
            return data.decode(encoding, errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkspaceSecurityError(f"non-text file refused: {relative}") from exc

    def list_directory(
        self, relative_path: Optional[PathLike] = None, *, max_entries: int = 500
    ) -> List[Dict[str, Any]]:
        """List one repository directory without traversing symbolic links."""
        if relative_path in (None, "", "."):
            base = self.root
            relative = "."
        else:
            relative, base = self._path_for_read(relative_path)
        if not base.is_dir() or base.is_symlink():
            raise WorkspaceEditError(f"not a directory: {relative}")
        entries: List[Dict[str, Any]] = []
        for path in sorted(base.iterdir(), key=lambda item: item.name.casefold()):
            if path.is_symlink():
                continue
            child = relative if relative != "." else ""
            rel_path = f"{child}/{path.name}" if child else path.name
            if is_protected_path(rel_path, self.protected_paths):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "path": rel_path,
                    "name": path.name,
                    "type": "directory" if path.is_dir() else "file",
                    "size": int(info.st_size),
                }
            )
            if len(entries) >= max(1, int(max_entries)):
                break
        return entries

    def read_image(
        self, relative: PathLike, *, include_data: bool = True
    ) -> Dict[str, Any]:
        """Read a bounded repository image as base64 with its detected MIME type."""
        normalized, _full = self._path_for_read(relative)
        guessed, _encoding = mimetypes.guess_type(normalized)
        if not guessed or not guessed.startswith("image/"):
            raise WorkspaceEditError(f"not a supported image: {normalized}")
        data = self.read_bytes(normalized)
        result: Dict[str, Any] = {
            "path": normalized,
            "media_type": guessed,
            "bytes": len(data),
            "revision": self.revision(normalized).to_dict(),
        }
        if include_data:
            result["data_base64"] = base64.b64encode(data).decode("ascii")
        return result

    def _expected_revision(
        self,
        current: FileRevision,
        expected_sha256: Optional[str],
        expected_revision: Optional[Union[FileRevision, Mapping[str, Any], str]],
        expected_file_hash: Optional[str],
        expected_hash: Optional[str],
    ) -> None:
        candidate = expected_revision
        if candidate is None:
            candidate = expected_sha256
        if candidate is None:
            candidate = expected_file_hash
        if candidate is None:
            candidate = expected_hash
        if candidate is None:
            return
        if isinstance(candidate, FileRevision):
            expected = candidate
        elif isinstance(candidate, Mapping):
            expected = FileRevision.from_dict(candidate)
        else:
            expected = FileRevision(True, str(candidate), kind="file")
        if not _same_revision(current, expected):
            raise WorkspaceConflictError(
                f"stale edit precondition: expected {expected.sha256 or '<absent>'}, "
                f"current {current.sha256 or '<absent>'}"
            )

    def _backup_dir(self, operation_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(operation_id)):
            raise WorkspaceUndoConflictError("invalid backup operation id")
        return self.state_dir / "backups" / str(operation_id)

    def _write_backup(
        self, operation_id: str, relative: str, before: FileRevision
    ) -> Path:
        directory = self._backup_dir(operation_id)
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {
            "path": relative,
            "before": before.to_dict(),
            "backup": "data.bin" if before.exists else "absent",
        }
        _atomic_json(directory / "meta.json", metadata)
        if before.exists:
            _rel, source = self._path_for_read(relative)
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise WorkspaceEditError(f"cannot back up {relative}: {exc}") from exc
            if _sha256_bytes(data) != before.sha256:
                raise WorkspaceConflictError(f"file changed before backup: {relative}")
            _atomic_write_bytes(directory / "data.bin", data, before.mode)
        else:
            try:
                (directory / "absent").touch()
            except OSError as exc:
                raise WorkspaceEditError(f"cannot record absent backup: {exc}") from exc
        return directory

    def _backup_bytes(self, operation_id: str) -> Tuple[FileRevision, Optional[bytes]]:
        directory = self._backup_dir(operation_id)
        metadata = _read_json(directory / "meta.json", {})
        before = FileRevision.from_dict(metadata.get("before") or {})
        if not before.exists:
            return before, None
        try:
            data = (directory / "data.bin").read_bytes()
            if _sha256_bytes(data) != before.sha256:
                raise WorkspaceUndoConflictError(
                    f"backup hash mismatch for {operation_id}"
                )
            return before, data
        except OSError as exc:
            raise WorkspaceUndoConflictError(
                f"backup unavailable for {operation_id}"
            ) from exc

    def _append_record(
        self, record: MutationRecord, status: Optional[str] = None
    ) -> Dict[str, Any]:
        if status is not None:
            record.status = status
        return self.journal.append(record.to_dict())

    def _event(self, event: str, **data: Any) -> None:
        self.journal.append({"event": event, "workspace_id": self.workspace_id, **data})

    @staticmethod
    def _phase(phase: str, hook: Optional[Callable[[str], None]]) -> None:
        if hook is not None:
            hook(phase)

    def _active_lease(self) -> Optional[WorkspaceLease]:
        return getattr(self._local, "lease", None)

    def _ensure_lease(self, paths: Sequence[str]) -> WorkspaceLease:
        current = self._active_lease()
        if current is not None:
            current.assert_valid()
            if not all(
                any(_lease_paths_overlap(path, leased) for leased in current.paths)
                for path in paths
            ):
                raise WorkspaceLeaseError(
                    "active lease does not cover this mutation path"
                )
            return current
        lease = self._acquire_lease(self.owner, paths=paths, ttl_s=self.lease_ttl_s)
        self._local.lease = lease
        return lease

    def _release_call_lease(self, lease: WorkspaceLease) -> None:
        if getattr(self._local, "lease", None) is lease:
            self._local.lease = None
            lease.release()

    def _acquire_lease(
        self,
        owner: str,
        *,
        paths: Optional[Sequence[str]] = None,
        ttl_s: Optional[float] = None,
        heartbeat_s: Optional[float] = None,
    ) -> WorkspaceLease:
        normalized_paths = (
            ("*",)
            if paths is None
            else tuple(sorted({normalize_relative_path(path) for path in paths}))
        )
        duration = max(1.0, float(self.lease_ttl_s if ttl_s is None else ttl_s))
        token = _new_id("lease")
        now = _now()
        metadata = {
            "lease_id": token,
            "owner": str(owner),
            "paths": list(normalized_paths),
            "pid": os.getpid(),
            "created_at": now,
            "expires_at": now + duration,
            "workspace_id": self.workspace_id,
        }
        with _locked_file(self.lease_path.with_name(self.lease_path.name + ".lock")):
            current = _read_json(self.lease_path, None)
            if isinstance(current, dict):
                current_paths = tuple(str(item) for item in current.get("paths", ()))
                requested = set(normalized_paths)
                occupied = any(
                    _lease_paths_overlap(left, right)
                    for left in requested
                    for right in current_paths
                )
                current_pid = int(current.get("pid", 0) or 0)
                expired = float(current.get("expires_at", 0)) <= now
                dead = current_pid > 0 and not _pid_alive(current_pid)
                if occupied and not expired and not dead:
                    raise WorkspaceLeaseError(
                        f"workspace mutation lease held by {current.get('owner', 'unknown')}"
                    )
            _atomic_json(self.lease_path, metadata)
        lease = WorkspaceLease(
            workspace=self,
            owner=str(owner),
            lease_id=token,
            paths=normalized_paths,
            state_path=self.lease_path,
            created_at=now,
            expires_at=now + duration,
            heartbeat_s=heartbeat_s,
        )
        if heartbeat_s and heartbeat_s > 0:

            def heartbeat() -> None:
                while not lease._stop.wait(float(heartbeat_s)):
                    if not lease.renew(self.lease_ttl_s):
                        return

            lease._thread = threading.Thread(
                target=heartbeat,
                name=f"workspace-lease-{token[-8:]}",
                daemon=True,
            )
            lease._thread.start()
        return lease

    def lease(
        self,
        owner: Optional[str] = None,
        *,
        paths: Optional[Sequence[str]] = None,
        ttl_s: Optional[float] = None,
        heartbeat_s: Optional[float] = None,
    ) -> WorkspaceLease:
        """Acquire a mutation lease; use as a context manager or release it."""
        return self._acquire_lease(
            owner or self.owner,
            paths=paths,
            ttl_s=ttl_s,
            heartbeat_s=heartbeat_s,
        )

    def lease_info(self) -> Optional[Dict[str, Any]]:
        """Return current lease metadata, including stale records."""
        return _read_json(self.lease_path, None)

    def _fenced_replace(
        self,
        lease: WorkspaceLease,
        full: Path,
        expected: FileRevision,
        data: bytes,
    ) -> None:
        with _locked_file(lease.state_path.with_name(lease.state_path.name + ".lock")):
            lease_value = _read_json(lease.state_path, {})
            if not lease._valid_value(lease_value):
                raise WorkspaceLeaseError(
                    "workspace mutation lease changed before commit"
                )
            current = self._revision(full)
            if not _same_revision(current, expected):
                raise WorkspaceConflictError(
                    f"file changed before fenced commit: {full.name}"
                )
            mode = expected.mode if expected.exists else stat.S_IMODE(0o600)
            _atomic_write_bytes(full, data, mode)

    def _fenced_unlink(
        self, lease: WorkspaceLease, full: Path, expected: FileRevision
    ) -> None:
        with _locked_file(lease.state_path.with_name(lease.state_path.name + ".lock")):
            lease_value = _read_json(lease.state_path, {})
            if not lease._valid_value(lease_value):
                raise WorkspaceLeaseError(
                    "workspace mutation lease changed before delete"
                )
            current = self._revision(full)
            if not _same_revision(current, expected):
                raise WorkspaceConflictError(
                    f"file changed before fenced delete: {full.name}"
                )
            full.unlink()

    def _fenced_move(
        self,
        lease: WorkspaceLease,
        source: Path,
        destination: Path,
        expected: FileRevision,
    ) -> None:
        with _locked_file(lease.state_path.with_name(lease.state_path.name + ".lock")):
            lease_value = _read_json(lease.state_path, {})
            if not lease._valid_value(lease_value):
                raise WorkspaceLeaseError(
                    "workspace mutation lease changed before rename"
                )
            if not _same_revision(self._revision(source), expected):
                raise WorkspaceConflictError(
                    f"file changed before fenced rename: {source.name}"
                )
            if destination.exists() or destination.is_symlink():
                raise WorkspaceConflictError(
                    f"rename destination already exists: {destination.name}"
                )
            os.replace(str(source), str(destination))

    def _mutation(
        self,
        relative: str,
        full: Path,
        kind: str,
        before: FileRevision,
        after_data: bytes,
        *,
        hunk_id: Optional[str] = None,
        proposed_effect: Optional[Mapping[str, Any]] = None,
        validator: Optional[Callable[[bytes], Any]] = None,
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> EditResult:
        current_before = self._revision(full)
        if not _same_revision(current_before, before):
            raise WorkspaceConflictError(
                f"file changed before atomic mutation: {relative}"
            )
        mode = before.mode if before.exists else stat.S_IMODE(0o600)
        after = FileRevision(
            True,
            _sha256_bytes(after_data),
            len(after_data),
            mode,
            "file",
        )
        operation_id = _new_id("mut")
        prior_lease = self._active_lease()
        lease = self._ensure_lease([relative])
        try:
            self._write_backup(operation_id, relative, before)
            raw_effect = dict(proposed_effect or {})
            private_effect = {
                str(key): value
                for key, value in raw_effect.items()
                if str(key).startswith("_")
            }
            effect = {
                str(key): value
                for key, value in raw_effect.items()
                if not str(key).startswith("_")
            }
            if private_effect:
                _atomic_json(
                    self._backup_dir(operation_id) / "effect.json", private_effect
                )
            effect.setdefault("pre_hash", before.sha256)
            effect.setdefault("post_hash", after.sha256)
            effect.setdefault("pre_size", before.size)
            effect.setdefault("post_size", after.size)
            record = MutationRecord(
                operation_id=operation_id,
                workspace_id=self.workspace_id,
                path=relative,
                kind=kind,
                owner=self.owner,
                lease_id=lease.lease_id,
                before=before,
                after=after,
                proposed_effect=effect,
                status="proposed",
                hunk_id=hunk_id,
            )
            self._append_record(record)
            try:
                self._phase("before_replace", on_phase)
                if validator is not None:
                    validator(after_data)
                _relative, full = self._path(relative)
                self._fenced_replace(lease, full, before, after_data)
                self._phase("after_replace", on_phase)
                self._append_record(record, "committed")
                self._phase("committed", on_phase)
            except BaseException as exc:
                try:
                    current = self._revision(full)
                except Exception:
                    current = FileRevision()
                self._append_record(
                    record,
                    "recoverable" if _same_revision(current, after) else "failed",
                )
                if not _same_revision(current, after):
                    record.error = str(exc)
                raise
            return EditResult(
                operation_id=operation_id,
                path=relative,
                replacements=int(effect.get("replacements", 1)),
                before=before,
                after=after,
                created=not before.exists,
                hunk_id=hunk_id,
                effect=effect,
            )
        finally:
            if prior_lease is None:
                self._release_call_lease(lease)

    def apply_exact_edit(
        self,
        relative_path: PathLike,
        old_string: str,
        new_string: str,
        *,
        expected_sha256: Optional[str] = None,
        expected_revision: Optional[Union[FileRevision, Mapping[str, Any], str]] = None,
        expected_file_hash: Optional[str] = None,
        expected_hash: Optional[str] = None,
        require_unique: bool = True,
        hunk_id: Optional[str] = None,
        validator: Optional[Callable[[bytes], Any]] = None,
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> EditResult:
        """Apply one exact text replacement with a durable precondition.

        The old string must occur exactly once by default.  The proposed
        post-image is journaled before an atomic replace, and a stale or
        ambiguous match raises before any write occurs.
        """
        relative, full = self._path(relative_path)
        if not old_string:
            raise WorkspaceEditError("old_string must not be empty")
        if old_string == new_string:
            raise WorkspaceEditError("old_string and new_string are identical")
        before = self._revision(full)
        if not before.exists or before.kind != "file":
            raise WorkspaceEditError(f"file does not exist: {relative}")
        self._expected_revision(
            before,
            expected_sha256,
            expected_revision,
            expected_file_hash,
            expected_hash,
        )
        data = self.read_bytes(relative)
        if b"\x00" in data:
            raise WorkspaceSecurityError(f"binary file refused: {relative}")
        try:
            text = data.decode("utf-8", errors="strict")
            old_bytes = str(old_string).encode("utf-8", errors="strict")
            new_bytes = str(new_string).encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise WorkspaceSecurityError(
                f"edit text is not valid UTF-8: {exc}"
            ) from exc
        if _SECRET_TEXT.search(text):
            raise WorkspaceSecurityError("secret-like file content refused")
        if _SECRET_TEXT.search(str(old_string)) or _SECRET_TEXT.search(str(new_string)):
            raise WorkspaceSecurityError("secret-like edit text refused")
        count = text.count(str(old_string))
        if count == 0:
            raise WorkspaceEditError(f"old_string not found verbatim in {relative}")
        if require_unique and count != 1:
            raise WorkspaceEditError(
                f"ambiguous edit refused for {relative}: {count} matches; expected one"
            )
        if require_unique or count == 1:
            text = text.replace(str(old_string), str(new_string), 1)
            replacements = 1
        else:
            text = text.replace(str(old_string), str(new_string))
            replacements = count
        after_data = text.encode("utf-8")
        if b"\x00" in after_data:
            raise WorkspaceSecurityError(f"binary post-image refused: {relative}")
        match_offset = data.find(old_bytes)
        context_size = 64
        context_before = data[max(0, match_offset - context_size) : match_offset]
        context_after = data[
            match_offset + len(old_bytes) : match_offset + len(old_bytes) + context_size
        ]
        effect = {
            "replacements": replacements,
            "match_count": count,
            "old_text_sha256": _sha256_bytes(old_bytes),
            "new_text_sha256": _sha256_bytes(new_bytes),
            "old_text_bytes": len(old_bytes),
            "new_text_bytes": len(new_bytes),
            "offset": match_offset,
            "encoding": "utf-8",
            "hunk_undo": True,
            "_old_text": str(old_string),
            "_new_text": str(new_string),
            "_context_before": context_before.decode("utf-8", errors="strict"),
            "_context_after": context_after.decode("utf-8", errors="strict"),
        }
        return self._mutation(
            relative,
            full,
            "edit",
            before,
            after_data,
            hunk_id=hunk_id,
            proposed_effect=effect,
            validator=validator,
            on_phase=on_phase,
        )

    exact_edit = apply_exact_edit
    replace_exact = apply_exact_edit
    apply_edit = apply_exact_edit
    replace = apply_exact_edit
    edit = apply_exact_edit

    def write_file(
        self,
        relative_path: PathLike,
        content: Union[str, bytes],
        *,
        expected_sha256: Optional[str] = None,
        expected_revision: Optional[Union[FileRevision, Mapping[str, Any], str]] = None,
        expected_file_hash: Optional[str] = None,
        expected_hash: Optional[str] = None,
        overwrite: bool = False,
        hunk_id: Optional[str] = None,
        validator: Optional[Callable[[bytes], Any]] = None,
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> EditResult:
        """Create or replace a text file atomically with a precondition."""
        relative, full = self._path(relative_path)
        before = self._revision(full)
        self._expected_revision(
            before,
            expected_sha256,
            expected_revision,
            expected_file_hash,
            expected_hash,
        )
        if before.exists and not overwrite:
            raise WorkspaceConflictError(
                f"refusing to overwrite existing file: {relative}"
            )
        if before.exists and before.kind != "file":
            raise WorkspaceSecurityError(f"special file refused: {relative}")
        if (
            before.exists
            and overwrite
            and all(
                item is None
                for item in (
                    expected_sha256,
                    expected_revision,
                    expected_file_hash,
                    expected_hash,
                )
            )
        ):
            raise WorkspaceConflictError(
                f"overwrite requires an expected file revision: {relative}"
            )
        if isinstance(content, str):
            try:
                data = content.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise WorkspaceSecurityError(
                    f"content is not valid UTF-8: {exc}"
                ) from exc
        elif isinstance(content, bytes):
            data = content
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WorkspaceSecurityError(
                    f"content is not valid UTF-8: {exc}"
                ) from exc
        else:
            raise WorkspaceEditError("content must be str or bytes")
        if b"\x00" in data:
            raise WorkspaceSecurityError(f"binary write refused: {relative}")
        if _SECRET_TEXT.search(data.decode("utf-8", errors="strict")):
            raise WorkspaceSecurityError("secret-like file content refused")
        if len(data) > self.max_file_bytes:
            raise WorkspaceSecurityError(
                f"write exceeds workspace mutation limit: {relative}"
            )
        return self._mutation(
            relative,
            full,
            "write",
            before,
            data,
            hunk_id=hunk_id,
            proposed_effect={
                "replacements": 1,
                "encoding": "utf-8",
                "bytes": len(data),
            },
            validator=validator,
            on_phase=on_phase,
        )

    write = write_file

    def apply_unified_patch(
        self,
        patch: str,
        *,
        expected_revisions: Mapping[str, Any],
        allow_delete: bool = True,
    ) -> List[EditResult]:
        """Apply a unified patch with mandatory per-file revision preconditions."""
        parsed = _parse_unified_patch(patch)
        missing = sorted(set(parsed) - set(expected_revisions))
        if missing:
            raise WorkspaceConflictError(
                f"patch is missing expected revisions: {', '.join(missing)}"
            )
        prepared: List[Tuple[str, str, FileRevision]] = []
        for relative, hunks in parsed.items():
            _relative, full = self._path(relative)
            before = self._revision(full)
            self._expected_revision(
                before,
                None,
                expected_revisions[relative],
                None,
                None,
            )
            if before.exists and before.kind != "file":
                raise WorkspaceSecurityError(f"special file refused: {relative}")
            text = ""
            if before.exists:
                text = self.read_text(relative)
            cursor = 0
            for hunk in hunks:
                old = str(hunk["old"])
                new = str(hunk["new"])
                if "\r\n" in text and "\r\n" not in old:
                    old = old.replace("\n", "\r\n")
                    new = new.replace("\n", "\r\n")
                expected = max(0, int(hunk["old_start"]) - 1 + cursor)
                if text[expected : expected + len(old)] == old:
                    position = expected
                else:
                    window_start = max(0, expected - 200)
                    window_end = min(len(text), expected + len(old) + 200)
                    candidates = [
                        candidate
                        for candidate in range(window_start, window_end + 1)
                        if text[candidate : candidate + len(old)] == old
                    ]
                    if len(candidates) != 1:
                        raise WorkspaceConflictError(
                            f"patch hunk does not apply uniquely: {relative}:{hunk['old_start']}"
                        )
                    position = candidates[0]
                text = text[:position] + new + text[position + len(old) :]
                cursor = position + len(new)
            if not before.exists and not text:
                raise WorkspaceEditError(f"patch creates an empty file: {relative}")
            if before.exists and not text and not allow_delete:
                raise WorkspaceEditError(f"patch deletion is disabled: {relative}")
            prepared.append((relative, text, before))
        results: List[EditResult] = []
        try:
            for relative, text, before in prepared:
                if not text and before.exists:
                    results.append(
                        self.delete_file(
                            relative,
                            expected_revision=before,
                            hunk_id=f"patch:{relative}",
                        )
                    )
                elif before.exists:
                    results.append(
                        self.write_file(
                            relative,
                            text,
                            overwrite=True,
                            expected_revision=before,
                            hunk_id=f"patch:{relative}",
                        )
                    )
                else:
                    results.append(
                        self.write_file(relative, text, hunk_id=f"patch:{relative}")
                    )
        except Exception:
            for result in reversed(results):
                try:
                    self.undo_operation(result.operation_id)
                except Exception:
                    pass
            raise
        return results

    apply_patch = apply_unified_patch

    def delete_file(
        self,
        relative_path: PathLike,
        *,
        expected_sha256: Optional[str] = None,
        expected_revision: Optional[Union[FileRevision, Mapping[str, Any], str]] = None,
        hunk_id: Optional[str] = None,
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> EditResult:
        """Delete a file only when its expected revision is current."""
        relative, full = self._path(relative_path)
        before = self._revision(full)
        if not before.exists:
            raise WorkspaceEditError(f"file does not exist: {relative}")
        if before.kind != "file":
            raise WorkspaceSecurityError(f"special file refused: {relative}")
        if expected_sha256 is None and expected_revision is None:
            raise WorkspaceConflictError(
                f"delete requires an expected file revision: {relative}"
            )
        self._expected_revision(before, expected_sha256, expected_revision, None, None)
        operation_id = _new_id("mut")
        prior_lease = self._active_lease()
        lease = self._ensure_lease([relative])
        try:
            self._write_backup(operation_id, relative, before)
            after = FileRevision()
            record = MutationRecord(
                operation_id=operation_id,
                workspace_id=self.workspace_id,
                path=relative,
                kind="delete",
                owner=self.owner,
                lease_id=lease.lease_id,
                before=before,
                after=after,
                proposed_effect={"replacements": 1},
                hunk_id=hunk_id,
            )
            self._append_record(record)
            try:
                self._phase("before_replace", on_phase)
                _relative, full = self._path(relative)
                self._fenced_unlink(lease, full, before)
                self._phase("after_replace", on_phase)
                self._append_record(record, "committed")
                self._phase("committed", on_phase)
            except BaseException:
                self._append_record(
                    record, "recoverable" if not full.exists() else "failed"
                )
                raise
            return EditResult(
                operation_id,
                relative,
                1,
                before,
                after,
                False,
                hunk_id,
                {"replacements": 1},
            )
        finally:
            if prior_lease is None:
                self._release_call_lease(lease)

    def rename_file(
        self,
        source_path: PathLike,
        destination_path: PathLike,
        *,
        expected_sha256: Optional[str] = None,
        expected_revision: Optional[Union[FileRevision, Mapping[str, Any], str]] = None,
        hunk_id: Optional[str] = None,
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> EditResult:
        """Rename a regular file atomically with a source revision precondition."""
        source, source_full = self._path(source_path)
        destination, destination_full = self._path(destination_path)
        if source == destination:
            raise WorkspaceEditError("rename source and destination are identical")
        before = self._revision(source_full)
        if not before.exists or before.kind != "file":
            raise WorkspaceEditError(f"rename source is not a regular file: {source}")
        self._expected_revision(
            before,
            expected_sha256,
            expected_revision,
            None,
            None,
        )
        destination_before = self._revision(destination_full)
        if destination_before.exists:
            raise WorkspaceConflictError(
                f"rename destination already exists: {destination}"
            )
        operation_id = _new_id("mut")
        prior_lease = self._active_lease()
        lease = self._ensure_lease([source, destination])
        try:
            self._write_backup(operation_id, source, before)
            after = FileRevision(
                True,
                before.sha256,
                before.size,
                before.mode,
                "file",
            )
            record = MutationRecord(
                operation_id=operation_id,
                workspace_id=self.workspace_id,
                path=destination,
                kind="rename",
                owner=self.owner,
                lease_id=lease.lease_id,
                before=before,
                after=after,
                proposed_effect={
                    "replacements": 1,
                    "source_path": source,
                    "destination_path": destination,
                },
                hunk_id=hunk_id,
            )
            self._append_record(record)
            try:
                self._phase("before_replace", on_phase)
                _source, source_full = self._path(source)
                _destination, destination_full = self._path(destination)
                self._fenced_move(lease, source_full, destination_full, before)
                self._phase("after_replace", on_phase)
                self._append_record(record, "committed")
                self._phase("committed", on_phase)
            except BaseException as exc:
                current_source = self._revision(source_full)
                current_destination = self._revision(destination_full)
                recovered = (
                    _same_revision(current_destination, after)
                    and not current_source.exists
                )
                self._append_record(record, "recoverable" if recovered else "failed")
                if not recovered:
                    record.error = str(exc)
                raise
            return EditResult(
                operation_id=operation_id,
                path=destination,
                replacements=1,
                before=before,
                after=after,
                created=True,
                hunk_id=hunk_id,
                effect={
                    "replacements": 1,
                    "source_path": source,
                    "destination_path": destination,
                },
            )
        finally:
            if prior_lease is None:
                self._release_call_lease(lease)

    move_file = rename_file

    def _record_map(self) -> Dict[str, MutationRecord]:
        return {record.operation_id: record for record in self.journal.records()}

    def recover(self) -> Dict[str, Any]:
        """Classify proposed mutations left by a crash without overwriting data."""
        recovered: List[Dict[str, Any]] = []
        for record in self.journal.records():
            if record.status not in {"proposed", "recoverable"}:
                continue
            try:
                _relative, full = self._path_for_read(record.path)
                current = self._revision(full)
            except Exception:
                current = FileRevision()
            if _same_revision(current, record.after):
                self._append_record(record, "committed")
                recovered.append(
                    {"operation_id": record.operation_id, "status": "committed"}
                )
            elif _same_revision(current, record.before):
                self._append_record(record, "aborted")
                recovered.append(
                    {"operation_id": record.operation_id, "status": "aborted"}
                )
            else:
                self._append_record(record, "conflict")
                recovered.append(
                    {"operation_id": record.operation_id, "status": "conflict"}
                )
        return {"recovered": recovered, "workspace_id": self.workspace_id}

    def _restore_record(
        self,
        record: MutationRecord,
        lease: WorkspaceLease,
        *,
        hunk: bool = False,
    ) -> Tuple[str, Optional[str]]:
        current = self.revision(record.path)
        exact_post = _same_revision(current, record.after)
        if record.kind == "rename" and exact_post:
            source = str(record.proposed_effect.get("source_path") or "")
            try:
                source_relative, source_full = self._path(source)
            except WorkspaceSecurityError:
                return "conflict", None
            if source_full.exists() or source_full.is_symlink():
                return "conflict", None
            _destination, destination_full = self._path(record.path)
            try:
                self._fenced_move(lease, destination_full, source_full, record.after)
            except WorkspaceError:
                return "conflict", None
            return "restored", source_relative
        if not exact_post:
            private = _read_json(
                self._backup_dir(record.operation_id) / "effect.json", {}
            )
            old_text = private.get("_old_text")
            new_text = private.get("_new_text")
            if (
                not hunk
                or record.kind != "edit"
                or not isinstance(old_text, str)
                or not isinstance(new_text, str)
            ):
                self._event(
                    "undo_conflict",
                    operation_id=record.operation_id,
                    path=record.path,
                    expected_hash=record.after.sha256,
                    actual_hash=current.sha256,
                )
                return "conflict", None
            try:
                current_bytes = self.read_bytes(record.path)
                current_text = current_bytes.decode("utf-8", errors="strict")
            except (WorkspaceError, UnicodeDecodeError):
                return "conflict", None
            before_image, before_data = self._backup_bytes(record.operation_id)
            if not before_image.exists or before_data is None:
                return "conflict", None
            try:
                expected_text = before_data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return "conflict", None
            if expected_text.count(old_text) != 1:
                return "conflict", None
            expected_text = expected_text.replace(old_text, new_text, 1)
            ordered = self.journal.records()
            target_index = next(
                (
                    index
                    for index, item in enumerate(ordered)
                    if item.operation_id == record.operation_id
                ),
                -1,
            )
            if target_index < 0:
                return "conflict", None
            for prior in reversed(ordered[:target_index]):
                if prior.path != record.path or prior.status != "undone":
                    continue
                if prior.kind != "edit":
                    return "conflict", None
                prior_private = _read_json(
                    self._backup_dir(prior.operation_id) / "effect.json", {}
                )
                prior_old = prior_private.get("_old_text")
                prior_new = prior_private.get("_new_text")
                if not isinstance(prior_old, str) or not isinstance(prior_new, str):
                    return "conflict", None
                if expected_text.count(prior_new) != 1:
                    return "conflict", None
                expected_text = expected_text.replace(prior_new, prior_old, 1)
            for later in ordered[target_index + 1 :]:
                if later.path != record.path or later.status in {
                    "undone",
                    "aborted",
                    "failed",
                    "conflict",
                }:
                    continue
                if later.kind != "edit":
                    return "conflict", None
                later_private = _read_json(
                    self._backup_dir(later.operation_id) / "effect.json", {}
                )
                later_old = later_private.get("_old_text")
                later_new = later_private.get("_new_text")
                if not isinstance(later_old, str) or not isinstance(later_new, str):
                    return "conflict", None
                if expected_text.count(later_old) != 1:
                    return "conflict", None
                expected_text = expected_text.replace(later_old, later_new, 1)
            if current_text != expected_text:
                self._event(
                    "undo_conflict",
                    operation_id=record.operation_id,
                    path=record.path,
                    reason="post-image includes changes outside agent-owned hunks",
                )
                return "conflict", None
            positions = []
            start = 0
            while True:
                position = current_text.find(new_text, start)
                if position < 0:
                    break
                positions.append(position)
                start = position + max(1, len(new_text))
            if len(positions) != 1:
                self._event(
                    "undo_conflict",
                    operation_id=record.operation_id,
                    path=record.path,
                    reason="hunk no longer uniquely matches",
                )
                return "conflict", None
            position = positions[0]
            restored_text = (
                current_text[:position]
                + old_text
                + current_text[position + len(new_text) :]
            )
            _relative, full = self._path_for_read(record.path)
            try:
                self._fenced_replace(
                    lease,
                    full,
                    current,
                    restored_text.encode("utf-8", errors="strict"),
                )
            except WorkspaceError:
                return "conflict", None
            return "restored", record.path
        before, data = self._backup_bytes(record.operation_id)
        if before.exists:
            if data is None:
                return "missing", None
            _relative, full = self._path_for_read(record.path)
            try:
                self._fenced_replace(lease, full, current, data)
            except WorkspaceError:
                return "conflict", None
            return "restored", record.path
        _relative, full = self._path_for_read(record.path)
        if full.is_symlink() or not full.is_file():
            return "conflict", None
        try:
            self._fenced_unlink(lease, full, current)
        except WorkspaceError:
            return "missing", None
        return "deleted", record.path

    def undo(
        self,
        operation_id: Optional[str] = None,
        *,
        hunk_id: Optional[str] = None,
        steps: Union[int, str] = 1,
        targets: Optional[Sequence[PathLike]] = None,
    ) -> UndoResult:
        """Undo operation-level edits in reverse order with hash conflicts."""
        records = self.journal.records()
        if operation_id is not None and hunk_id is not None:
            raise WorkspaceEditError("specify operation_id or hunk_id, not both")
        if operation_id is not None:
            selected = [
                record for record in records if record.operation_id == operation_id
            ]
            if not selected:
                return UndoResult(missing=[str(operation_id)])
        elif hunk_id is not None:
            selected = [record for record in records if record.hunk_id == hunk_id]
            if len(selected) > 1:
                raise WorkspaceEditError(f"ambiguous hunk id: {hunk_id}")
            if not selected:
                return UndoResult(missing=[str(hunk_id)])
        else:
            wanted = None
            if targets:
                wanted = {normalize_relative_path(item) for item in targets}
            candidates = [
                record
                for record in records
                if record.status not in {"undone", "aborted", "failed", "conflict"}
                and (
                    wanted is None
                    or record.path in wanted
                    or str(record.proposed_effect.get("source_path") or "") in wanted
                )
            ]
            candidates.reverse()
            if isinstance(steps, str) and steps.lower() == "all":
                selected = candidates
            else:
                try:
                    count = max(1, int(steps))
                except (TypeError, ValueError) as exc:
                    raise WorkspaceEditError(
                        "steps must be an integer or 'all'"
                    ) from exc
                selected = candidates[:count]
        result = UndoResult()
        for record in selected:
            if record.status == "undone":
                continue
            self._event(
                "undo_proposed", operation_id=record.operation_id, path=record.path
            )
            prior_lease = self._active_lease()
            lease = self._ensure_lease([record.path])
            try:
                try:
                    lease.assert_valid()
                    action, path = self._restore_record(
                        record,
                        lease,
                        hunk=bool(hunk_id),
                    )
                except WorkspaceUndoConflictError:
                    action, path = "conflict", None
            finally:
                if prior_lease is None:
                    self._release_call_lease(lease)
            if action in {"restored", "deleted"} and path:
                if action == "restored":
                    result.restored.append(path)
                else:
                    result.deleted.append(path)
                result.undone.append(record.operation_id)
                self._append_record(record, "undone")
            elif action == "missing":
                result.missing.append(record.path)
            else:
                result.conflicts.append(
                    {"operation_id": record.operation_id, "path": record.path}
                )
        result._sync_dict()
        return result

    def undo_file(
        self, relative_path: PathLike, steps: Union[int, str] = 1
    ) -> UndoResult:
        """Undo the newest one or more operations for one file."""
        return self.undo(steps=steps, targets=[relative_path])

    def undo_hunk(self, hunk_id: str) -> UndoResult:
        """Undo one named edit hunk by its unique identifier."""
        return self.undo(hunk_id=hunk_id)

    def undo_operation(self, operation_id: str) -> UndoResult:
        """Undo one operation by its unique identifier."""
        return self.undo(operation_id=operation_id)

    def undo_last(self, steps: Union[int, str] = 1) -> UndoResult:
        """Undo the newest operation or set of operations."""
        return self.undo(steps=steps)

    def classify_changes(self) -> Dict[str, Any]:
        """Separate committed agent effects from unattributed user changes."""
        expected: Dict[str, FileRevision] = {
            key: FileRevision.from_dict(value)
            for key, value in (self._baseline.get("files") or {}).items()
        }
        for path in self.identity.dirty_paths:
            expected.setdefault(path, self.revision(path))
        conflicts: List[str] = []
        records = self.journal.records()
        for record in records:
            if record.status in {"failed", "aborted", "conflict"}:
                continue
            if record.kind == "rename":
                source = str(record.proposed_effect.get("source_path") or "")
                try:
                    source = normalize_relative_path(source)
                except WorkspaceSecurityError:
                    conflicts.append(record.path)
                    continue
                if not _same_revision(
                    expected.get(source, FileRevision()), record.before
                ):
                    conflicts.extend((source, record.path))
                    continue
                if record.status == "undone":
                    expected[source] = record.before
                    expected[record.path] = FileRevision()
                else:
                    expected[source] = FileRevision()
                    expected[record.path] = record.after
                continue
            wanted = expected.get(record.path, FileRevision())
            if not _same_revision(wanted, record.before):
                conflicts.append(record.path)
                continue
            expected[record.path] = (
                record.before if record.status == "undone" else record.after
            )
        agent_owned: List[str] = []
        user_changes: List[str] = []
        for relative in sorted(set(expected) | set(self._scan_revisions())):
            actual = self.revision(relative)
            wanted = expected.get(relative, FileRevision())
            if _same_revision(actual, wanted):
                if any(
                    record.path == relative
                    and record.status not in {"undone", "aborted"}
                    for record in records
                ):
                    agent_owned.append(relative)
            else:
                user_changes.append(relative)
        return {
            "agent_owned": agent_owned,
            "user_changes": user_changes,
            "conflicts": sorted(set(conflicts)),
            "preexisting_dirty": list(self.identity.dirty_paths),
        }

    def agent_owned_files(self) -> List[str]:
        """List files whose current content is attributable to agent records."""
        return list(self.classify_changes()["agent_owned"])

    def changed_files(self) -> List[str]:
        """Return paths changed from the captured baseline, without attribution."""
        baseline = self._baseline.get("files") or {}
        result: List[str] = []
        for relative in sorted(set(baseline) | set(self._scan_revisions())):
            before = FileRevision.from_dict(baseline.get(relative) or {})
            after = self.revision(relative)
            if not _same_revision(after, before):
                result.append(relative)
        return result

    def close(self) -> None:
        """Release a lease held by the current thread, if any."""
        lease = self._active_lease()
        if lease is not None:
            self._release_call_lease(lease)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def canonical_workspace_id(repo_path: PathLike) -> str:
    """Return the stable identifier used for a repository's state directory."""
    identity = capture_workspace_identity(repo_path)
    material = f"{identity.root}|{identity.device}|{identity.inode}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def open_workspace(
    repo_path: PathLike,
    *,
    state_dir: Optional[PathLike] = None,
    owner: Optional[str] = None,
    protected_paths: Sequence[str] = (),
    lease_ttl_s: float = 300.0,
) -> Workspace:
    """Open a durable workspace for a repository path."""
    return Workspace(
        repo_path,
        state_dir,
        owner=owner,
        protected_paths=protected_paths,
        lease_ttl_s=lease_ttl_s,
    )


def acquire_lease(
    workspace: Union[Workspace, PathLike],
    *,
    state_dir: Optional[PathLike] = None,
    owner: Optional[str] = None,
    paths: Optional[Sequence[PathLike]] = None,
    ttl_s: float = 300.0,
    heartbeat_s: Optional[float] = None,
) -> WorkspaceLease:
    """Acquire a lease for a workspace object or repository path."""
    if not isinstance(workspace, Workspace):
        workspace = Workspace(workspace, state_dir, owner=owner, lease_ttl_s=ttl_s)
    return workspace.lease(owner, paths=paths, ttl_s=ttl_s, heartbeat_s=heartbeat_s)


acquire_mutation_lease = acquire_lease


def apply_exact_edit(
    repo_path: PathLike,
    relative_path: PathLike,
    old_string: str,
    new_string: str,
    *,
    state_dir: Optional[PathLike] = None,
    owner: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    expected_revision: Optional[Union[FileRevision, Mapping[str, Any], str]] = None,
    expected_file_hash: Optional[str] = None,
    expected_hash: Optional[str] = None,
    protected_paths: Sequence[str] = (),
    hunk_id: Optional[str] = None,
    validator: Optional[Callable[[bytes], Any]] = None,
    on_phase: Optional[Callable[[str], None]] = None,
) -> EditResult:
    """Open a workspace and apply one exact edit."""
    workspace = Workspace(
        repo_path,
        state_dir,
        owner=owner,
        protected_paths=protected_paths,
    )
    try:
        return workspace.apply_exact_edit(
            relative_path,
            old_string,
            new_string,
            expected_sha256=expected_sha256,
            expected_revision=expected_revision,
            expected_file_hash=expected_file_hash,
            expected_hash=expected_hash,
            hunk_id=hunk_id,
            validator=validator,
            on_phase=on_phase,
        )
    finally:
        workspace.close()


def apply_patch(
    repo_path: PathLike,
    patch: str,
    *,
    state_dir: Optional[PathLike] = None,
    expected_revisions: Mapping[str, Any],
    protected_paths: Sequence[str] = (),
    allow_delete: bool = True,
) -> List[EditResult]:
    """Apply a unified patch through a temporary conflict-safe workspace."""
    workspace = Workspace(
        repo_path,
        state_dir,
        protected_paths=protected_paths,
    )
    try:
        return workspace.apply_unified_patch(
            patch,
            expected_revisions=expected_revisions,
            allow_delete=allow_delete,
        )
    finally:
        workspace.close()


class CancellationToken:
    """Thread-safe cancellation signal shared by local process handles."""

    def __init__(self, external: Any = None) -> None:
        self._event = threading.Event()
        self.external = external

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        if self._event.is_set():
            return True
        for name in ("is_cancelled", "is_set"):
            checker = getattr(self.external, name, None)
            if callable(checker):
                try:
                    return bool(checker())
                except Exception:
                    return False
        return False

    @property
    def cancelled(self) -> bool:
        return self.is_cancelled()


def _capture_stream(stream: Any, limit: int) -> Tuple[str, int]:
    head: List[bytes] = []
    tail = bytearray()
    total = 0
    limit = max(0, int(limit))
    head_limit = limit // 2
    tail_limit = limit - head_limit
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        remaining = head_limit - sum(len(item) for item in head)
        if remaining > 0:
            take = min(len(chunk), remaining)
            head.append(chunk[:take])
            chunk = chunk[take:]
        if chunk:
            tail.extend(chunk)
            if len(tail) > tail_limit:
                del tail[: len(tail) - tail_limit]
    if total <= limit:
        raw = b"".join(head) + bytes(tail)
    else:
        raw = (
            b"".join(head)
            + f"\n[... {total - limit} bytes omitted ...]\n".encode()
            + bytes(tail)
        )
    return raw.decode("utf-8", errors="replace"), total


def _kill_process_tree(process: subprocess.Popen[Any], force: bool = False) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T"]
                + (["/F"] if force else []),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        elif force:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill() if force else process.terminate()
        except OSError:
            pass


@dataclass(frozen=True)
class ResourceLimits:
    """Resource and output limits applied to an execution request."""

    timeout_s: Optional[float] = None
    max_output_bytes: int = 1_000_000
    memory_limit_mb: Optional[int] = None
    max_processes: Optional[int] = None
    cpu_seconds: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class LocalExecutionResult:
    """Result of an explicitly local process run."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool = False
    process_id: Optional[int] = None
    output_bytes: int = 0
    limits: Dict[str, Any] = field(default_factory=dict)

    def to_execution_result(self) -> ExecutionResult:
        return ExecutionResult(self.exit_code, self.stdout, self.stderr, self.timed_out)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class LocalExecutionHandle:
    """A cancellable local process with bounded output and descendant cleanup."""

    def __init__(
        self,
        process: subprocess.Popen[Any],
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
        stdout_box: List[Any],
        stderr_box: List[Any],
        token: CancellationToken,
        timeout_s: Optional[float],
        cwd: Path,
        limits: Optional[Dict[str, Any]] = None,
        windows_job: Optional[Any] = None,
        temporary_home: Optional[Path] = None,
    ) -> None:
        self.process = process
        self._stdout_thread = stdout_thread
        self._stderr_thread = stderr_thread
        self._stdout_box = stdout_box
        self._stderr_box = stderr_box
        self.token = token
        self.timeout_s = timeout_s
        self.cwd = cwd
        self.limits = dict(limits or {})
        self.windows_job = windows_job
        self.temporary_home = temporary_home
        self._result: Optional[LocalExecutionResult] = None
        self._cancelled = False
        self._timed_out = False
        _register_process(self)

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid

    def cancel(self) -> None:
        """Request cancellation and terminate the process tree."""
        self.token.cancel()
        self._cancelled = True
        if self.windows_job is not None:
            _close_windows_job(self.windows_job)
            self.windows_job = None
        _kill_process_tree(self.process, force=False)
        _kill_process_tree(self.process, force=True)

    def poll(self) -> Optional[int]:
        return self.process.poll()

    def wait(self, timeout_s: Optional[float] = None) -> LocalExecutionResult:
        """Wait for completion, enforcing timeout and cancellation."""
        configured_deadline = (
            None if self.timeout_s is None else time.monotonic() + self.timeout_s
        )
        requested_deadline = (
            None if timeout_s is None else time.monotonic() + float(timeout_s)
        )
        deadlines = [
            item
            for item in (configured_deadline, requested_deadline)
            if item is not None
        ]
        deadline = min(deadlines) if deadlines else None
        while self.process.poll() is None:
            if self.token.is_cancelled():
                self._cancelled = True
                _kill_process_tree(self.process, force=False)
                _kill_process_tree(self.process, force=True)
                break
            if deadline is not None and time.monotonic() >= deadline:
                self._timed_out = True
                _kill_process_tree(self.process, force=False)
                _kill_process_tree(self.process, force=True)
                break
            time.sleep(0.05)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_process_tree(self.process, force=True)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._stdout_thread.join(timeout=5)
        self._stderr_thread.join(timeout=5)
        stdout, out_bytes = self._stdout_box
        stderr, err_bytes = self._stderr_box
        if self._cancelled:
            exit_code = 130
        elif self._timed_out:
            exit_code = 124
        else:
            exit_code = (
                self.process.returncode if self.process.returncode is not None else 1
            )
        self._result = LocalExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=self._timed_out,
            cancelled=self._cancelled,
            process_id=self.process.pid,
            output_bytes=int(out_bytes) + int(err_bytes),
            limits=dict(self.limits),
        )
        # THE SUBPROCESS-OUTPUT INGRESS for the local path. A local child can
        # `cat .env` exactly as easily as a containerized one, and before this
        # line its stdout went to the model, the journal and the TUI verbatim.
        # Sealed HERE, once, at the point the process is finished and the
        # result is final — so no later reader of `handle.result()` can get an
        # unsealed copy, and `output_bytes` keeps reporting the TRUE byte
        # count the collector saw (a cap must never make the receipt smaller
        # than what happened).
        self._result.stdout, out_r = seal_output(stdout, cap_bytes=self._output_cap())
        self._result.stderr, err_r = seal_output(stderr, cap_bytes=self._output_cap())
        self._ingress = (out_r.to_dict(), err_r.to_dict())
        if self.windows_job is not None:
            _close_windows_job(self.windows_job)
            self.windows_job = None
        if self.temporary_home is not None:
            shutil.rmtree(self.temporary_home, ignore_errors=True)
            self.temporary_home = None
        _unregister_process(self)
        return self._result

    def _output_cap(self) -> int:
        """The ingress byte cap for this handle's declared purpose.

        ``limits["max_output_bytes"]`` is the collector's own cap and is
        honoured verbatim — a caller that asked for a smaller ring gets a
        smaller ring, and a caller that asked for a larger one than the ingress
        cap is bounded BY THE INGRESS, not by its own request. The ingress is
        the ceiling; the collector is the collector.
        """
        requested = int((self.limits or {}).get("max_output_bytes") or 0)
        return requested if requested > 0 else OUTPUT_CAP_BYTES

    def ingress_report(self) -> Dict[str, Any]:
        """What the ingress did to this run's two streams (empty until wait)."""
        return dict(getattr(self, "_ingress", ()) or ())

    def result(self, timeout_s: Optional[float] = None) -> LocalExecutionResult:
        return self.wait(timeout_s)

    @property
    def cancelled(self) -> bool:
        return self._cancelled or self.token.is_cancelled()

    @property
    def timed_out(self) -> bool:
        return self._timed_out


_ACTIVE_PROCESSES: Dict[int, LocalExecutionHandle] = {}
_ACTIVE_PROCESS_LOCK = threading.Lock()


def _register_process(handle: LocalExecutionHandle) -> None:
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES[id(handle)] = handle


def _unregister_process(handle: LocalExecutionHandle) -> None:
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES.pop(id(handle), None)


def cleanup_active_processes(timeout_s: float = 10.0) -> int:
    """Cancel and reap every process started by this safe backend."""
    with _ACTIVE_PROCESS_LOCK:
        handles = list(_ACTIVE_PROCESSES.values())
    for handle in handles:
        try:
            handle.cancel()
            handle.wait(timeout_s)
        except Exception:
            continue
    return len(handles)


atexit.register(cleanup_active_processes)


class MCPChildRegistry:
    """Track MCP child handles so cancellation can terminate every child."""

    def __init__(self) -> None:
        self._children: Dict[int, Any] = {}
        self._lock = threading.Lock()

    def register(self, child: Any) -> Any:
        """Register a child exposing cancel/terminate or close."""
        if child is not None:
            with self._lock:
                self._children[id(child)] = child
        return child

    def unregister(self, child: Any) -> None:
        with self._lock:
            self._children.pop(id(child), None)

    def cancel_all(self) -> int:
        """Cancel and close every registered child; return the count."""
        with self._lock:
            children = list(self._children.values())
            self._children.clear()
        for child in children:
            for name in ("cancel", "terminate", "close"):
                method = getattr(child, name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:
                        pass
                    break
        return len(children)


MCP_CHILDREN = MCPChildRegistry()


def scrub_env(
    environ: Optional[Mapping[str, str]] = None,
    *,
    extra: Optional[Mapping[str, str]] = None,
    allow: Iterable[str] = (),
    home: Optional[PathLike] = None,
    isolated: bool = True,
) -> Dict[str, str]:
    """Return a child environment scrubbed by the shared security policy."""
    scrubbed = scrub_environment(
        environ,
        extra=extra,
        allow=allow,
        home=home,
        isolated=isolated,
    )
    return {
        str(key): str(value)
        for key, value in scrubbed.items()
        if not _SECRET_TEXT.search(str(value))
    }


def start_local_execution(
    repo_path: PathLike,
    command: str,
    *,
    timeout_s: Optional[float] = 120.0,
    cancel_event: Optional[Any] = None,
    cancellation_token: Optional[CancellationToken] = None,
    cwd: Optional[PathLike] = None,
    env: Optional[Mapping[str, str]] = None,
    max_output_bytes: int = 1_000_000,
    memory_limit_mb: Optional[int] = None,
    max_processes: Optional[int] = None,
    cpu_seconds: Optional[int] = None,
) -> LocalExecutionHandle:
    """Start an explicitly local command with cancellation and cleanup."""
    root = canonical_repo_root(repo_path)
    cwd_path = Path(cwd).expanduser() if cwd is not None else root
    try:
        cwd_path = cwd_path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceSecurityError(f"invalid local command cwd: {cwd}") from exc
    if not _path_within(cwd_path, root):
        raise WorkspaceSecurityError("local command cwd escapes workspace")
    if not command or not str(command).strip():
        raise WorkspaceSecurityError("local command must not be empty")
    if cancellation_token is not None:
        token = cancellation_token
    elif cancel_event is not None:
        token = (
            cancel_event
            if isinstance(cancel_event, CancellationToken)
            else CancellationToken(cancel_event)
        )
    else:
        token = CancellationToken()
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    if memory_limit_mb is not None and int(memory_limit_mb) <= 0:
        raise ValueError("memory_limit_mb must be positive")
    if max_processes is not None and int(max_processes) <= 0:
        raise ValueError("max_processes must be positive")
    if cpu_seconds is not None and int(cpu_seconds) <= 0:
        raise ValueError("cpu_seconds must be positive")
    limits = ResourceLimits(
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
        memory_limit_mb=memory_limit_mb,
        max_processes=max_processes,
        cpu_seconds=cpu_seconds,
    ).to_dict()
    temporary_home = Path(tempfile.mkdtemp(prefix="neo-home-"))
    child_env = scrub_env(extra=env, home=temporary_home)
    kwargs: Dict[str, Any] = {
        "cwd": str(cwd_path),
        "env": child_env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
        if (
            memory_limit_mb is not None
            or max_processes is not None
            or cpu_seconds is not None
        ):
            import resource

            def apply_limits() -> None:
                if memory_limit_mb is not None:
                    bytes_limit = int(memory_limit_mb) * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
                if max_processes is not None:
                    resource.setrlimit(
                        resource.RLIMIT_NPROC, (int(max_processes), int(max_processes))
                    )
                if cpu_seconds is not None:
                    resource.setrlimit(
                        resource.RLIMIT_CPU, (int(cpu_seconds), int(cpu_seconds))
                    )

            kwargs["preexec_fn"] = apply_limits
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        shutil.rmtree(temporary_home, ignore_errors=True)
        raise WorkspaceExecutionError(f"could not start local command: {exc}") from exc
    stdout_box: List[Any] = ["", 0]
    stderr_box: List[Any] = ["", 0]

    def read_stdout() -> None:
        stdout_box[:] = list(_capture_stream(process.stdout, max_output_bytes))

    def read_stderr() -> None:
        stderr_box[:] = list(_capture_stream(process.stderr, max_output_bytes))

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    return LocalExecutionHandle(
        process,
        stdout_thread,
        stderr_thread,
        stdout_box,
        stderr_box,
        token,
        timeout_s,
        cwd_path,
        limits,
        _assign_windows_job(process),
        temporary_home,
    )


start_local = start_local_execution


def execute_local(
    repo_path: PathLike,
    command: str,
    *,
    timeout_s: Optional[float] = 120.0,
    cancel_event: Optional[Any] = None,
    cancellation_token: Optional[CancellationToken] = None,
    cwd: Optional[PathLike] = None,
    env: Optional[Mapping[str, str]] = None,
    max_output_bytes: int = 1_000_000,
    memory_limit_mb: Optional[int] = None,
    max_processes: Optional[int] = None,
    cpu_seconds: Optional[int] = None,
) -> LocalExecutionResult:
    """Run one explicitly local command and return bounded output."""
    handle = start_local_execution(
        repo_path,
        command,
        timeout_s=timeout_s,
        cancel_event=cancel_event,
        cancellation_token=cancellation_token,
        cwd=cwd,
        env=env,
        max_output_bytes=max_output_bytes,
        memory_limit_mb=memory_limit_mb,
        max_processes=max_processes,
        cpu_seconds=cpu_seconds,
    )
    return handle.wait()


run_local = execute_local


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._data = bytearray()
        self._base = 0
        self._total = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._data.extend(data)
            self._total += len(data)
            overflow = len(self._data) - self.limit
            if overflow > 0:
                del self._data[:overflow]
                self._base += overflow

    def read(self, offset: int, limit: int) -> Dict[str, Any]:
        with self._lock:
            requested = max(0, int(offset))
            if requested < self._base:
                requested = self._base
            local = requested - self._base
            raw = bytes(self._data[local : local + max(1, int(limit))])
            return {
                "text": raw.decode("utf-8", errors="replace"),
                "next_offset": min(self._total, requested + len(raw)),
                "total_bytes": self._total,
                "discarded_before": requested if requested > int(offset) else 0,
            }

    @property
    def total(self) -> int:
        with self._lock:
            return self._total


class BackgroundProcess:
    """A bounded bidirectional background process owned by one tool backend."""

    def __init__(
        self,
        process: subprocess.Popen[Any],
        stdout_buffer: _BoundedOutput,
        stderr_buffer: _BoundedOutput,
        reader_threads: Sequence[threading.Thread],
        temporary_home: Path,
        windows_job: Optional[Any],
        *,
        pty_requested: bool,
        pty_allocated: bool,
        command: str,
        cwd: Path,
        workspace_lease: Optional[WorkspaceLease] = None,
    ) -> None:
        self.id = f"proc-{uuid.uuid4().hex}"
        self.process = process
        self.stdout = stdout_buffer
        self.stderr = stderr_buffer
        self.reader_threads = tuple(reader_threads)
        self.temporary_home = temporary_home
        self.windows_job = windows_job
        self.pty_requested = bool(pty_requested)
        self.pty_allocated = bool(pty_allocated)
        self.command = command
        self.cwd = cwd
        self.workspace_lease = workspace_lease
        self.started_at = _now()
        self._wait_lock = threading.Lock()
        _register_process(self)

    @property
    def pid(self) -> int:
        """Return the host process identifier."""
        return int(self.process.pid)

    def _output_cap(self) -> int:
        """The ingress byte cap for a background read.

        The ring buffer's own limit is the ceiling here: a background process
        is polled repeatedly by offset, so a cap SMALLER than the ring would
        be the only thing bounding what a single read can pull, and re-applying
        the default 1 MB on top of a caller's 20-char-per-read window would be
        a no-op dressed as a control. The ring's limit is therefore both the
        memory bound and the redaction bound, which is one number rather than
        two that can disagree.
        """
        return max(1, int(getattr(self.stdout, "limit", OUTPUT_CAP_BYTES)))

    def poll(self) -> Optional[int]:
        """Return the process exit code without blocking."""
        return self.process.poll()

    def read_output(
        self, *, stdout_offset: int = 0, stderr_offset: int = 0, max_chars: int = 20_000
    ) -> Dict[str, Any]:
        """Read bounded process output without blocking.

        THE SUBPROCESS-OUTPUT INGRESS for the background path. The two
        offsets are the caller's cursor into the RAW stream and they are
        deliberately NOT reinterpreted: `next_offset` and `total_bytes` keep
        counting raw bytes, so a redacted stream cannot make a caller re-read
        a range or skip one. Only the returned `text` is sealed, because only
        `text` is displayed. `discarded_before` therefore still reports what
        the ring dropped — a cap is never allowed to look like a short read.
        """
        if self.poll() is not None:
            self._release_workspace_lease()
        cap = max(1, int(max_chars))
        payload = {
            "process_id": self.id,
            "pid": self.pid,
            "running": self.poll() is None,
            "exit_code": self.poll(),
            "stdout": self.stdout.read(stdout_offset, cap),
            "stderr": self.stderr.read(stderr_offset, cap),
            "pty_requested": self.pty_requested,
            "pty_allocated": self.pty_allocated,
        }
        for name in ("stdout", "stderr"):
            stream = payload.get(name)
            if not isinstance(stream, dict):
                continue
            sealed, report = seal_output(
                stream.get("text"), cap_bytes=self._output_cap()
            )
            stream["text"] = sealed
            stream["ingress"] = report.to_dict()
        return payload

    def write_stdin(self, data: str, *, append_newline: bool = False) -> Dict[str, Any]:
        """Write text to a live background process stdin stream."""
        if self.poll() is not None:
            raise WorkspaceExecutionError("background process is not running")
        payload = str(data or "") + ("\n" if append_newline else "")
        stream = self.process.stdin
        if stream is None:
            raise WorkspaceExecutionError("background process stdin is unavailable")
        try:
            stream.write(payload.encode("utf-8", errors="strict"))
            stream.flush()
        except (OSError, UnicodeEncodeError) as exc:
            raise WorkspaceExecutionError(
                f"could not write process stdin: {exc}"
            ) from exc
        return {"process_id": self.id, "bytes_written": len(payload.encode("utf-8"))}

    def _release_workspace_lease(self) -> None:
        if self.workspace_lease is not None:
            self.workspace_lease.release()
            self.workspace_lease = None

    def kill(self, *, force: bool = True) -> Dict[str, Any]:
        """Terminate the process and every registered descendant."""
        if self.windows_job is not None:
            _close_windows_job(self.windows_job)
            self.windows_job = None
        _kill_process_tree(self.process, force=force)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_tree(self.process, force=True)
        self._release_workspace_lease()
        return {"process_id": self.id, "pid": self.pid, "exit_code": self.poll()}

    def cancel(self) -> Dict[str, Any]:
        """Cancel this background process and its workspace lease."""
        return self.kill(force=True)

    def wait(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """Wait for process exit and return its final bounded output."""
        with self._wait_lock:
            try:
                self.process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.kill(force=True)
            for thread in self.reader_threads:
                thread.join(timeout=2)
            if self.windows_job is not None:
                _close_windows_job(self.windows_job)
                self.windows_job = None
            shutil.rmtree(self.temporary_home, ignore_errors=True)
            self.temporary_home = None
            self._release_workspace_lease()
            _unregister_process(self)
            return self.read_output()

    def status(self) -> Dict[str, Any]:
        """Return current process state and output byte counters."""
        if self.poll() is not None:
            self._release_workspace_lease()
        return {
            "process_id": self.id,
            "pid": self.pid,
            "running": self.poll() is None,
            "exit_code": self.poll(),
            "stdout_bytes": self.stdout.total,
            "stderr_bytes": self.stderr.total,
            "pty_requested": self.pty_requested,
            "pty_allocated": self.pty_allocated,
        }


class ProcessManager:
    """Thread-safe registry for background processes in one backend session."""

    def __init__(self) -> None:
        self._processes: Dict[str, BackgroundProcess] = {}
        self._lock = threading.Lock()

    def add(self, process: BackgroundProcess) -> BackgroundProcess:
        """Register and return one background process."""
        with self._lock:
            self._processes[process.id] = process
        return process

    def get(self, process_id: str) -> BackgroundProcess:
        """Return one live or exited process by opaque identifier."""
        with self._lock:
            process = self._processes.get(str(process_id))
        if process is None:
            raise WorkspaceEditError(f"unknown process id: {process_id}")
        return process

    def remove(self, process_id: str) -> Optional[BackgroundProcess]:
        """Remove and return one process if present."""
        with self._lock:
            return self._processes.pop(str(process_id), None)

    def list(self) -> List[Dict[str, Any]]:
        """List bounded status rows for all owned processes."""
        with self._lock:
            processes = list(self._processes.values())
        return [process.status() for process in processes]

    def kill_all(self) -> int:
        """Kill every registered process and return the count."""
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            try:
                process.kill(force=True)
            except Exception:
                pass
        with self._lock:
            self._processes.clear()
        return len(processes)


def start_background_process(
    repo_path: PathLike,
    command: str,
    *,
    cwd: Optional[PathLike] = None,
    env: Optional[Mapping[str, str]] = None,
    max_output_bytes: int = 1_000_000,
    pty: bool = False,
    workspace_lease: Optional[WorkspaceLease] = None,
) -> BackgroundProcess:
    """Start a bounded bidirectional process with optional POSIX PTY allocation."""
    root = canonical_repo_root(repo_path)
    cwd_path = Path(cwd).expanduser() if cwd is not None else root
    try:
        cwd_path = cwd_path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceSecurityError(f"invalid process cwd: {cwd}") from exc
    if not _path_within(cwd_path, root):
        raise WorkspaceSecurityError("process cwd escapes workspace")
    if not command or not str(command).strip():
        raise WorkspaceSecurityError("process command must not be empty")
    temporary_home = Path(tempfile.mkdtemp(prefix="neo-home-"))
    child_env = scrub_env(extra=env, home=temporary_home)
    pty_master: Optional[Any] = None
    pty_slave: Optional[Any] = None
    pty_allocated = False
    kwargs: Dict[str, Any] = {
        "cwd": str(cwd_path),
        "env": child_env,
        "shell": True,
        "bufsize": 0,
    }
    if pty and os.name != "nt":
        try:
            import fcntl
            import pty as pty_module
            import termios

            pty_master, pty_slave = pty_module.openpty()
            fcntl.ioctl(pty_slave, termios.TIOCSCTTY, 0)
            kwargs.update(stdin=pty_slave, stdout=pty_slave, stderr=pty_slave)
            pty_allocated = True
        except (ImportError, OSError):
            if pty_master is not None:
                os.close(pty_master)
            if pty_slave is not None:
                os.close(pty_slave)
            pty_master = pty_slave = None
            kwargs.update(
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
    else:
        kwargs.update(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        if pty_master is not None:
            os.close(pty_master)
        if pty_slave is not None:
            os.close(pty_slave)
        shutil.rmtree(temporary_home, ignore_errors=True)
        if workspace_lease is not None:
            workspace_lease.release()
        raise WorkspaceExecutionError(
            f"could not start background process: {exc}"
        ) from exc
    if pty_slave is not None:
        os.close(pty_slave)
    stdout_buffer = _BoundedOutput(max_output_bytes)
    stderr_buffer = _BoundedOutput(max_output_bytes)
    threads: List[threading.Thread] = []
    if pty_allocated and pty_master is not None:

        def read_combined() -> None:
            while True:
                try:
                    chunk = os.read(pty_master.fileno(), 65536)
                except OSError:
                    break
                if not chunk:
                    break
                stdout_buffer.append(chunk)

        thread = threading.Thread(target=read_combined, daemon=True)
    else:

        def read_stdout() -> None:
            stream = process.stdout
            if stream is None:
                return
            for chunk in iter(lambda: stream.read(65536), b""):
                stdout_buffer.append(chunk)

        def read_stderr() -> None:
            stream = process.stderr
            if stream is None:
                return
            for chunk in iter(lambda: stream.read(65536), b""):
                stderr_buffer.append(chunk)

        thread = threading.Thread(target=read_stdout, daemon=True)
        threads.append(threading.Thread(target=read_stderr, daemon=True))
    threads.append(thread)
    for reader in threads:
        reader.start()
    background = BackgroundProcess(
        process,
        stdout_buffer,
        stderr_buffer,
        threads,
        temporary_home,
        _assign_windows_job(process),
        pty_requested=pty,
        pty_allocated=pty_allocated,
        command=command,
        cwd=cwd_path,
        workspace_lease=workspace_lease,
    )
    if pty_allocated and pty_master is not None:
        process.stdin = pty_master
    return background


def native_sandbox_available() -> bool:
    """Return whether a supported native OS sandbox launcher is installed."""
    if os.name == "nt":
        return False
    return any(
        (Path(directory) / executable).is_file()
        for directory in (os.environ.get("PATH") or "").split(os.pathsep)
        if directory
        for executable in ("bwrap",)
    )


def execute_native_sandboxed(
    repo_path: PathLike,
    command: str,
    *,
    timeout_s: float = 120.0,
    allow_network: bool = False,
    env: Optional[Mapping[str, str]] = None,
    max_output_bytes: int = 1_000_000,
) -> LocalExecutionResult:
    """Run a command in an optional native Linux bubblewrap profile."""
    if not native_sandbox_available():
        raise NativeSandboxUnavailableError(
            "native OS sandbox profile is unavailable on this host"
        )
    root = canonical_repo_root(repo_path)
    launcher = next(
        Path(directory) / "bwrap"
        for directory in (os.environ.get("PATH") or "").split(os.pathsep)
        if directory and (Path(directory) / "bwrap").is_file()
    )
    network = "--share-net" if allow_network else "--unshare-net"
    wrapped = " ".join(
        [
            shlex.quote(str(launcher)),
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            network,
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(root),
            str(root),
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--chdir",
            str(root),
            "/bin/sh",
            "-lc",
            shlex.quote(command),
        ]
    )
    return execute_local(
        root,
        wrapped,
        timeout_s=timeout_s,
        env=env,
        max_output_bytes=max_output_bytes,
    )


@dataclass(frozen=True)
class ToolPolicy:
    """Static effect and boundary classification for one typed tool."""

    name: str
    effect: str
    backend: str
    mutating: bool = False
    network: bool = False
    process: bool = False
    mcp: bool = False
    sandboxed: bool = False
    requires_approval: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


_TOOL_POLICIES: Dict[str, ToolPolicy] = {
    "read": ToolPolicy("read", "read_only", "local"),
    "glob": ToolPolicy("glob", "read_only", "local"),
    "grep": ToolPolicy("grep", "read_only", "local"),
    "list": ToolPolicy("list", "read_only", "local"),
    "image": ToolPolicy("image", "read_only", "local"),
    "apply_patch": ToolPolicy(
        "apply_patch", "workspace_write", "local", mutating=True, requires_approval=True
    ),
    "edit": ToolPolicy(
        "edit", "workspace_write", "local", mutating=True, requires_approval=True
    ),
    "write": ToolPolicy(
        "write", "workspace_write", "local", mutating=True, requires_approval=True
    ),
    "rename": ToolPolicy(
        "rename", "workspace_write", "local", mutating=True, requires_approval=True
    ),
    "delete": ToolPolicy(
        "delete", "workspace_write", "local", mutating=True, requires_approval=True
    ),
    "undo": ToolPolicy(
        "undo", "workspace_write", "local", mutating=True, requires_approval=True
    ),
    "git_status": ToolPolicy("git_status", "read_only", "local"),
    "git_diff": ToolPolicy("git_diff", "read_only", "local"),
    "git_log": ToolPolicy("git_log", "read_only", "local"),
    "git_show": ToolPolicy("git_show", "read_only", "local"),
    "git_blame": ToolPolicy("git_blame", "read_only", "local"),
    "git_branch": ToolPolicy("git_branch", "read_only", "local"),
    "git_worktree": ToolPolicy("git_worktree", "read_only", "local"),
    "shell": ToolPolicy(
        "shell", "process", "local", process=True, requires_approval=True
    ),
    "process": ToolPolicy(
        "process", "process", "local", process=True, requires_approval=True
    ),
    "process_read_output": ToolPolicy(
        "process_read_output", "process", "local", process=True
    ),
    "process_write_stdin": ToolPolicy(
        "process_write_stdin", "process", "local", process=True, requires_approval=True
    ),
    "process_kill": ToolPolicy(
        "process_kill", "process", "local", process=True, requires_approval=True
    ),
    "test": ToolPolicy(
        "test",
        "process",
        "sandboxed",
        process=True,
        sandboxed=True,
        requires_approval=True,
    ),
    "lint": ToolPolicy(
        "lint",
        "process",
        "sandboxed",
        process=True,
        sandboxed=True,
        requires_approval=True,
    ),
    "typecheck": ToolPolicy(
        "typecheck",
        "process",
        "sandboxed",
        process=True,
        sandboxed=True,
        requires_approval=True,
    ),
    "build": ToolPolicy(
        "build",
        "process",
        "sandboxed",
        process=True,
        sandboxed=True,
        requires_approval=True,
    ),
    "web_fetch": ToolPolicy(
        "web_fetch", "network", "remote", network=True, requires_approval=True
    ),
    "web_search": ToolPolicy(
        "web_search", "network", "remote", network=True, requires_approval=True
    ),
    "mcp": ToolPolicy("mcp", "mcp", "mcp", mcp=True, requires_approval=True),
    "memory": ToolPolicy("memory", "memory", "mcp", mcp=True),
    "question": ToolPolicy("question", "control", "local"),
    "todo": ToolPolicy("todo", "control", "local"),
    "plan": ToolPolicy("plan", "control", "local"),
    "task": ToolPolicy("task", "control", "local"),
    "finish": ToolPolicy("finish", "control", "local"),
    "verify": ToolPolicy(
        "verify", "process", "sandboxed", process=True, sandboxed=True
    ),
    "sandbox": ToolPolicy(
        "sandbox",
        "process",
        "sandboxed",
        process=True,
        sandboxed=True,
        requires_approval=True,
    ),
}


def policy_for_tool(tool: str) -> ToolPolicy:
    """Return the effect/backend policy for a typed tool name."""
    name = str(tool or "").strip().lower()
    canonical = {
        "bash": "shell",
        "fetch": "web_fetch",
        "search": "web_search",
        "mcp_call": "mcp",
        "ask": "question",
        "done": "finish",
    }.get(name, name)
    selected = _TOOL_POLICIES.get(
        canonical,
        ToolPolicy(canonical, "unknown", "unknown", requires_approval=True),
    )
    return (
        selected if selected.name == name else dataclasses.replace(selected, name=name)
    )


tool_policy = policy_for_tool


def describe_tool(tool: str, *, sandboxed: bool = False) -> Dict[str, Any]:
    """Return metadata showing local, sandboxed, remote, or MCP scope."""
    if sandboxed and str(tool).lower() in {"bash", "shell"}:
        name = str(tool).lower()
        return ToolPolicy(
            name,
            "process",
            "sandboxed",
            process=True,
            sandboxed=True,
            requires_approval=True,
        ).to_dict()
    return policy_for_tool(tool).to_dict()


class ExecutionProfile(str, Enum):
    """Explicit execution boundary selected by a safe backend."""

    LOCAL_TRUSTED = "local_trusted"
    DOCKER = "docker"
    NATIVE_OS = "native_os"
    VERIFIED_FIX = "verified_fix"


@dataclass(frozen=True)
class PolicyContext:
    """Identity dimensions bound to every policy decision and approval."""

    session_id: str = ""
    project_id: str = ""
    agent_mode: str = "daily"
    actor: str = "agent"
    protected_paths: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible policy context."""
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class PermissionRule:
    """One allow, ask, or deny rule across all required policy dimensions."""

    action: str
    tool: Optional[str] = None
    path: Optional[str] = None
    command_prefix: Optional[str] = None
    command_arity: Optional[int] = None
    network_domain: Optional[str] = None
    mcp_server: Optional[str] = None
    side_effect_class: Optional[str] = None
    agent_mode: Optional[str] = None
    actor: str = "*"
    scope: str = "once"
    name: str = ""

    def __post_init__(self) -> None:
        action = str(self.action or "deny").strip().lower()
        if action not in {"allow", "ask", "deny"}:
            raise ValueError(f"unsupported permission action: {self.action}")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "scope", normalize_approval_scope(self.scope))
        if self.command_arity is not None and int(self.command_arity) < 0:
            raise ValueError("command_arity must be non-negative")

    def matches(self, dimensions: Mapping[str, Any], context: PolicyContext) -> bool:
        """Return whether every configured rule dimension matches."""
        if self.tool and self.tool != dimensions["tool"]:
            return False
        if self.path and not _policy_path_matches(
            str(dimensions.get("path") or ""), self.path
        ):
            return False
        if self.command_prefix and not _policy_prefix_matches(
            str(dimensions.get("command") or ""), self.command_prefix
        ):
            return False
        if self.command_arity is not None and int(
            dimensions.get("command_arity") or 0
        ) != int(self.command_arity):
            return False
        if self.network_domain and not _policy_domain_matches(
            str(dimensions.get("network_domain") or ""), self.network_domain
        ):
            return False
        if self.mcp_server and self.mcp_server != dimensions.get("mcp_server"):
            return False
        if self.side_effect_class and self.side_effect_class != dimensions.get(
            "side_effect_class"
        ):
            return False
        if self.agent_mode and self.agent_mode != context.agent_mode:
            return False
        return self.actor in {"*", context.actor}

    def specificity(self) -> int:
        """Return a stable count of configured dimensions."""
        return sum(
            bool(value)
            for value in (
                self.tool,
                self.path,
                self.command_prefix,
                self.command_arity is not None,
                self.network_domain,
                self.mcp_server,
                self.side_effect_class,
                self.agent_mode,
            )
        )


@dataclass(frozen=True)
class PermissionDecision:
    """Resolved policy action and its exact non-secret effect binding."""

    action: str
    matched_rule: str
    scope: str
    effect_hash: str
    tool: str
    reason: str
    dimensions: Dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        """Return whether the decision authorizes dispatch."""
        return self.action == "allow"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible policy decision."""
        return {
            "action": self.action,
            "matched_rule": self.matched_rule,
            "scope": self.scope,
            "effect_hash": self.effect_hash,
            "tool": self.tool,
            "reason": self.reason,
            "dimensions": dict(self.dimensions),
        }


@dataclass(frozen=True)
class ApprovalGrant:
    """A scoped, expiring approval that never grants an unrelated effect."""

    scope: str
    effect_hash: str
    tool: str
    path: str = ""
    command_prefix: str = ""
    command_arity: Optional[int] = None
    network_domain: str = ""
    mcp_server: str = ""
    side_effect_class: str = ""
    agent_mode: str = ""
    actor: str = ""
    project_id: str = ""
    session_id: str = ""
    expires_at: Optional[float] = None

    def __post_init__(self) -> None:
        scope = normalize_approval_scope(self.scope)
        if scope not in {"once", "session", "project", "global"}:
            raise ValueError(f"unsupported approval scope: {self.scope}")
        if not self.tool or not self.effect_hash:
            raise ValueError("approval grants require tool and effect bindings")
        object.__setattr__(self, "scope", scope)

    def matches(self, dimensions: Mapping[str, Any], context: PolicyContext) -> bool:
        """Return whether this grant covers the exact current effect and context."""
        if self.expires_at is not None and float(self.expires_at) <= _now():
            return False
        if self.tool != dimensions.get("tool"):
            return False
        if self.effect_hash != dimensions.get("effect_hash"):
            return False
        if self.path and not _policy_path_matches(
            str(dimensions.get("path") or ""), self.path
        ):
            return False
        if self.command_prefix and not _policy_prefix_matches(
            str(dimensions.get("command") or ""), self.command_prefix
        ):
            return False
        if self.command_arity is not None and int(
            dimensions.get("command_arity") or 0
        ) != int(self.command_arity):
            return False
        if self.network_domain and self.network_domain != dimensions.get(
            "network_domain"
        ):
            return False
        if self.mcp_server and self.mcp_server != dimensions.get("mcp_server"):
            return False
        if self.side_effect_class and self.side_effect_class != dimensions.get(
            "side_effect_class"
        ):
            return False
        if self.agent_mode and self.agent_mode != context.agent_mode:
            return False
        if self.actor and self.actor != context.actor:
            return False
        if self.scope == "session" and (
            self.session_id != context.session_id
            or self.project_id != context.project_id
        ):
            return False
        return not (self.scope == "project" and self.project_id != context.project_id)

    def to_dict(self) -> Dict[str, Any]:
        """Return a secret-free persisted grant representation."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApprovalGrant":
        """Build a grant from persisted data."""
        data = dict(value or {})
        return cls(
            scope=str(data.get("scope") or "once"),
            effect_hash=str(data.get("effect_hash") or ""),
            tool=str(data.get("tool") or ""),
            path=str(data.get("path") or ""),
            command_prefix=str(data.get("command_prefix") or ""),
            command_arity=data.get("command_arity"),
            network_domain=str(data.get("network_domain") or ""),
            mcp_server=str(data.get("mcp_server") or ""),
            side_effect_class=str(data.get("side_effect_class") or ""),
            agent_mode=str(data.get("agent_mode") or ""),
            actor=str(data.get("actor") or ""),
            project_id=str(data.get("project_id") or ""),
            session_id=str(data.get("session_id") or ""),
            expires_at=data.get("expires_at"),
        )


class ApprovalStore:
    """Atomic JSON grant store that rejects symlink and traversal substitution."""

    def __init__(self, path: PathLike) -> None:
        candidate = Path(path).expanduser()
        if candidate.name in {"", ".", ".."}:
            raise WorkspaceSecurityError("approval store path is invalid")
        self.path = candidate
        current = candidate
        while not current.exists() and current != current.parent:
            current = current.parent
        if current.exists() and current.is_symlink():
            raise WorkspaceSecurityError("approval store path is symlinked")
        if candidate.exists() and candidate.is_symlink():
            raise WorkspaceSecurityError("approval store file is symlinked")

    def _read(self) -> List[Dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError) as exc:
            raise WorkspaceStateError("approval store is corrupt") from exc
        if not isinstance(value, list):
            raise WorkspaceStateError("approval store root must be a list")
        return [dict(item) for item in value if isinstance(item, Mapping)]

    def _write(self, values: Sequence[Mapping[str, Any]]) -> None:
        payload = (
            json.dumps(list(values), ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        )
        _atomic_write_bytes(self.path, payload.encode("utf-8"), 0o600)

    def load(self) -> List[ApprovalGrant]:
        """Load all valid, unexpired grants."""
        grants: List[ApprovalGrant] = []
        now = _now()
        for value in self._read():
            try:
                grant = ApprovalGrant.from_dict(value)
            except (TypeError, ValueError):
                continue
            if grant.expires_at is None or float(grant.expires_at) > now:
                grants.append(grant)
        return grants

    def put(self, grant: ApprovalGrant) -> None:
        """Insert or replace a grant without storing call arguments."""
        values = [
            value
            for value in self._read()
            if value.get("effect_hash") != grant.effect_hash
            or value.get("scope") != grant.scope
            or value.get("project_id") != grant.project_id
            or value.get("session_id") != grant.session_id
        ]
        values.append(grant.to_dict())
        self._write(values[-1000:])

    def remove(self, effect_hash: str, *, scope: Optional[str] = None) -> int:
        """Remove matching grants and return the removed count."""
        before = self._read()
        values = [
            value
            for value in before
            if not (
                value.get("effect_hash") == effect_hash
                and (scope is None or value.get("scope") == scope)
            )
        ]
        self._write(values)
        return len(before) - len(values)


@dataclass(frozen=True)
class ApprovalResponse:
    """Normal approver response with an explicit, bounded persistence scope."""

    approved: bool
    scope: str = "once"
    expires_at: Optional[float] = None
    reason: str = ""


def normalize_approval_scope(value: Any) -> str:
    """Normalize approval aliases to once, session, project, or global."""
    text = str(value or "once").strip().lower().replace("-", "_")
    aliases = {
        "call": "once",
        "exact": "once",
        "exact_call": "once",
        "once": "once",
        "session": "session",
        "session_path": "session",
        "session_command_prefix": "session",
        "project": "project",
        "global": "global",
    }
    return aliases.get(text, text)


def _policy_path_matches(value: str, pattern: str) -> bool:
    left = str(value or "").replace("\\", "/").strip("/")
    right = str(pattern or "").replace("\\", "/").strip("/")
    if fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(left, right + "/*"):
        return True
    return bool(left and right and (left == right or left.startswith(right + "/")))


def _command_tokens(command: str) -> List[str]:
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return str(command or "").split()


def _policy_prefix_matches(command: str, prefix: str) -> bool:
    if re.search(r"(?:&&|\|\||[;|<>`]|\$\()", str(command or "")):
        return False
    actual = _command_tokens(command)
    expected = _command_tokens(prefix)
    return bool(actual and expected and actual[: len(expected)] == expected)


def _policy_domain_matches(domain: str, pattern: str) -> bool:
    left = str(domain or "").lower().rstrip(".")
    right = str(pattern or "").lower().lstrip(".").rstrip(".")
    return bool(left and right and (left == right or left.endswith("." + right)))


def policy_dimensions(
    tool: str,
    arguments: Mapping[str, Any],
    *,
    side_effect_class: str,
) -> Dict[str, Any]:
    """Extract normalized policy dimensions from one typed tool call."""
    command = str(arguments.get("command") or "")
    paths = [
        str(arguments.get(key) or "").replace("\\", "/")
        for key in ("path", "target", "source_path", "destination_path", "file")
        if arguments.get(key)
    ]
    domain = ""
    url = str(arguments.get("url") or "")
    if not url and str(tool or "").lower() in {"search", "web_search"}:
        url = "https://html.duckduckgo.com/html/"
    if url:
        try:
            domain = (urlparse(url).hostname or "").lower()
        except ValueError:
            domain = ""
    raw = {
        "tool": str(tool or "").strip().lower(),
        "path": paths[0] if paths else "",
        "paths": tuple(paths),
        "command": command,
        "command_arity": len(_command_tokens(command)),
        "network_domain": domain,
        "mcp_server": str(arguments.get("server") or "").strip().lower(),
        "side_effect_class": str(side_effect_class or "unknown"),
    }
    from shared.security import redact_secrets, stable_digest

    raw["effect_hash"] = stable_digest(
        {"tool": raw["tool"], "arguments": redact_secrets(dict(arguments))}
    )
    return raw


class ToolPolicyEngine:
    """Deny-over-ask-over-allow policy with durable scoped approval grants."""

    def __init__(
        self,
        rules: Optional[Iterable[PermissionRule | Mapping[str, Any]]] = None,
        *,
        context: Optional[PolicyContext] = None,
        default_action: str = "auto",
        store: Optional[ApprovalStore] = None,
        grants: Optional[Iterable[ApprovalGrant | Mapping[str, Any]]] = None,
    ) -> None:
        self.context = context or PolicyContext()
        self.default_action = str(default_action or "auto").strip().lower()
        self.store = store
        self.rules = [self._rule_from_value(rule) for rule in (rules or ())]
        loaded = list(store.load()) if store is not None else []
        self.grants: List[ApprovalGrant] = loaded + [
            self._grant_from_value(grant) for grant in (grants or ())
        ]
        self.decisions: List[PermissionDecision] = []

    @property
    def audit_log(self) -> List[PermissionDecision]:
        """Return evaluated policy decisions."""
        return list(self.decisions)

    def evaluate(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        side_effect_class: str,
    ) -> PermissionDecision:
        """Evaluate one call with hard path protection and scoped grants."""
        dimensions = policy_dimensions(
            tool, arguments, side_effect_class=side_effect_class
        )
        hard_reason = ""
        for path in dimensions["paths"]:
            try:
                if is_protected_path(path, self.context.protected_paths):
                    hard_reason = f"protected path refused: {path}"
                    break
            except WorkspaceSecurityError:
                hard_reason = f"unsafe path refused: {path}"
                break
        matches = [
            rule for rule in self.rules if rule.matches(dimensions, self.context)
        ]
        if hard_reason:
            action = "deny"
            matched_rule = "hard_deny_protected_path"
            scope = "once"
            reason = hard_reason
        elif matches:
            selected = sorted(
                matches,
                key=lambda rule: (
                    {"deny": 3, "ask": 2, "allow": 1}.get(rule.action, 0),
                    rule.specificity(),
                ),
                reverse=True,
            )[0]
            action = selected.action
            matched_rule = selected.name or self._rule_label(selected)
            scope = selected.scope
            reason = "matched policy rule"
        else:
            action = self._default_for(side_effect_class)
            matched_rule = "default"
            scope = "once"
            reason = "default policy"
        if action != "deny":
            grant = self._covering_grant(dimensions)
            if grant is not None:
                action = "allow"
                matched_rule = f"{matched_rule}+approval"
                scope = grant.scope
                reason = "covered by scoped approval"
        decision = PermissionDecision(
            action=action,
            matched_rule=matched_rule,
            scope=scope,
            effect_hash=str(dimensions["effect_hash"]),
            tool=str(dimensions["tool"]),
            reason=reason,
            dimensions=dict(dimensions),
        )
        self.decisions.append(decision)
        return decision

    decide = evaluate

    def record_approval(
        self,
        decision: PermissionDecision,
        approved: bool,
        *,
        scope: Optional[str] = None,
        expires_at: Optional[float] = None,
    ) -> ApprovalGrant:
        """Record an approval response and optionally persist its bounded grant."""
        selected_scope = normalize_approval_scope(scope or decision.scope)
        grant = ApprovalGrant(
            scope=selected_scope,
            effect_hash=decision.effect_hash,
            tool=decision.tool,
            path=str(decision.dimensions.get("path") or ""),
            command_prefix=str(decision.dimensions.get("command") or ""),
            command_arity=decision.dimensions.get("command_arity"),
            network_domain=str(decision.dimensions.get("network_domain") or ""),
            mcp_server=str(decision.dimensions.get("mcp_server") or ""),
            side_effect_class=str(decision.dimensions.get("side_effect_class") or ""),
            agent_mode=self.context.agent_mode,
            actor=self.context.actor,
            project_id=self.context.project_id,
            session_id=self.context.session_id,
            expires_at=expires_at,
        )
        if approved:
            self.grants.append(grant)
            if self.store is not None and selected_scope != "once":
                self.store.put(grant)
        return grant

    def _covering_grant(self, dimensions: Mapping[str, Any]) -> Optional[ApprovalGrant]:
        for grant in reversed(self.grants):
            if grant.matches(dimensions, self.context):
                return grant
        return None

    def _default_for(self, side_effect_class: str) -> str:
        if self.default_action in {"allow", "ask", "deny"}:
            return self.default_action
        return "allow" if side_effect_class == "read_only" else "ask"

    @staticmethod
    def _rule_from_value(value: PermissionRule | Mapping[str, Any]) -> PermissionRule:
        if isinstance(value, PermissionRule):
            return value
        data = dict(value or {})
        return PermissionRule(
            action=str(data.get("action") or "deny"),
            tool=data.get("tool"),
            path=data.get("path"),
            command_prefix=data.get("command_prefix", data.get("command")),
            command_arity=data.get("command_arity", data.get("arity")),
            network_domain=data.get("network_domain", data.get("domain")),
            mcp_server=data.get("mcp_server", data.get("server")),
            side_effect_class=data.get("side_effect_class", data.get("side_effect")),
            agent_mode=data.get("agent_mode", data.get("mode")),
            actor=str(data.get("actor") or "*"),
            scope=str(data.get("scope") or "once"),
            name=str(data.get("name") or ""),
        )

    @staticmethod
    def _grant_from_value(value: ApprovalGrant | Mapping[str, Any]) -> ApprovalGrant:
        if isinstance(value, ApprovalGrant):
            return value
        return ApprovalGrant.from_dict(value)

    @staticmethod
    def _rule_label(rule: PermissionRule) -> str:
        fields = [
            rule.tool,
            rule.path,
            rule.command_prefix,
            None if rule.command_arity is None else str(rule.command_arity),
            rule.network_domain,
            rule.mcp_server,
            rule.side_effect_class,
            rule.agent_mode,
        ]
        matched = [str(value) for value in fields if value]
        return "rule:" + ("|".join(matched) if matched else rule.action)


@dataclass
class ToolResult:
    """Uniform result envelope for the safe typed-tool backend."""

    ok: bool
    tool: str
    effect: str
    backend: str
    value: Any = None
    error: Optional[str] = None
    operation_id: Optional[str] = None
    policy: Optional[Dict[str, Any]] = None
    profile: Optional[str] = None
    references: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "effect": self.effect,
            "backend": self.backend,
            "value": self.value,
            "error": self.error,
            "operation_id": self.operation_id,
            "policy": dict(self.policy) if self.policy is not None else None,
            "profile": self.profile,
            "references": list(self.references),
        }


class SafeToolBackend:
    """Dispatch typed tools through explicit policy and execution profiles."""

    _CANONICAL_TOOLS = frozenset(_TOOL_POLICIES) - {"sandbox"}
    _PROCESS_TOOLS = frozenset(
        {
            "shell",
            "process",
            "process_read_output",
            "process_write_stdin",
            "process_kill",
            "test",
            "lint",
            "typecheck",
            "build",
            "verify",
        }
    )

    def __init__(
        self,
        workspace: Workspace,
        *,
        approve: Optional[Callable[[str, Mapping[str, Any]], Any]] = None,
        network_call: Optional[Callable[..., Any]] = None,
        search_call: Optional[Callable[..., Any]] = None,
        mcp_call: Optional[Callable[..., Any]] = None,
        memory_call: Optional[Callable[..., Any]] = None,
        control_call: Optional[Callable[..., Any]] = None,
        mcp_registry: Optional[MCPChildRegistry] = None,
        sandbox_call: Optional[Callable[..., Any]] = None,
        native_call: Optional[Callable[..., Any]] = None,
        verify_call: Optional[Callable[..., Any]] = None,
        profile: Union[ExecutionProfile, str] = ExecutionProfile.LOCAL_TRUSTED,
        policy_engine: Optional[ToolPolicyEngine] = None,
        policy_context: Optional[PolicyContext] = None,
        approval_audit_root: Optional[PathLike] = None,
        require_revisions: bool = True,
    ) -> None:
        self.workspace = workspace
        self.approve = approve
        self.network_call = network_call
        self.search_call = search_call
        self.mcp_call = mcp_call
        self.memory_call = memory_call
        self.control_call = control_call
        self.mcp_registry = mcp_registry or MCPChildRegistry()
        self.sandbox_call = sandbox_call
        self.native_call = native_call
        self.verify_call = verify_call
        self.profile = ExecutionProfile(
            str(profile.value if isinstance(profile, ExecutionProfile) else profile)
        )
        self.policy_context = policy_context or PolicyContext(
            project_id=workspace.workspace_id,
            protected_paths=tuple(workspace.protected_paths),
        )
        self.policy_engine = policy_engine
        self.approval_audit_root = Path(
            approval_audit_root or workspace.state_dir
        ).expanduser()
        self.require_revisions = bool(require_revisions)
        self.processes = ProcessManager()
        self.session_state: Dict[str, Any] = {
            "todo": [],
            "plan": [],
            "tasks": [],
            "question": None,
            "finish": None,
        }
        self._state_lock = threading.Lock()

    def cancel_side_effects(self) -> int:
        """Cancel background processes and all MCP children owned by this backend."""
        return self.processes.kill_all() + self.mcp_registry.cancel_all()

    @staticmethod
    def _response(value: Any) -> ApprovalResponse:
        if isinstance(value, ApprovalResponse):
            return value
        if isinstance(value, bool):
            return ApprovalResponse(value)
        if isinstance(value, Mapping):
            return ApprovalResponse(
                approved=bool(value.get("approved", value.get("allow", False))),
                scope=str(value.get("scope") or "once"),
                expires_at=value.get("expires_at"),
                reason=str(value.get("reason") or ""),
            )
        return ApprovalResponse(False, reason="approver returned an unsupported value")

    def _audit_approval(
        self,
        decision: PermissionDecision,
        response: ApprovalResponse,
    ) -> None:
        from shared.security import append_approval_audit

        dimensions = decision.dimensions
        target = str(
            dimensions.get("path")
            or dimensions.get("network_domain")
            or dimensions.get("mcp_server")
            or dimensions.get("tool")
            or ""
        )
        append_approval_audit(
            self.approval_audit_root,
            "approved" if response.approved else "rejected",
            tool=decision.tool,
            target=target,
            actor=self.policy_context.actor,
            scope=response.scope,
            reason=response.reason or decision.reason,
            metadata={
                "effect_hash": decision.effect_hash,
                "session_id": self.policy_context.session_id,
                "project_id": self.policy_context.project_id,
                "agent_mode": self.policy_context.agent_mode,
            },
        )

    def _authorize(
        self, tool: str, values: Mapping[str, Any], policy: ToolPolicy
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        if self.policy_engine is None:
            if not policy.requires_approval:
                return True, None, None
            if self.approve is None:
                return False, None, "approval required"
            try:
                response = self._response(self.approve(tool, values))
            except Exception as exc:
                return False, None, f"approval failed: {exc}"
            return (
                response.approved,
                None,
                None if response.approved else "approval denied",
            )
        decision = self.policy_engine.evaluate(
            tool,
            values,
            side_effect_class=policy.effect,
        )
        if decision.action == "allow":
            return True, decision.to_dict(), None
        if decision.action == "deny":
            return False, decision.to_dict(), decision.reason
        if self.approve is None:
            return False, decision.to_dict(), "approval required"
        try:
            response = self._response(self.approve(tool, values))
            self._audit_approval(decision, response)
            self.policy_engine.record_approval(
                decision,
                response.approved,
                scope=response.scope,
                expires_at=response.expires_at,
            )
        except Exception as exc:
            return False, decision.to_dict(), f"approval failed closed: {exc}"
        return (
            response.approved,
            decision.to_dict(),
            None if response.approved else "approval denied",
        )

    def _execution_profile(self, tool: str, sandboxed: bool) -> ExecutionProfile:
        if sandboxed or tool == "sandbox":
            return ExecutionProfile.DOCKER
        if self.profile in {
            ExecutionProfile.DOCKER,
            ExecutionProfile.NATIVE_OS,
            ExecutionProfile.VERIFIED_FIX,
        }:
            return self.profile
        return ExecutionProfile.LOCAL_TRUSTED

    def _sandbox_options(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            key: values[key]
            for key in (
                "allow_network",
                "env",
                "mem_limit",
                "cpu_limit",
                "pids_limit",
                "cancel_event",
                "cancellation_token",
            )
            if key in values
        }

    def _run_process(self, values: Mapping[str, Any], profile: ExecutionProfile) -> Any:
        lease = self.workspace.lease(
            f"{self.workspace.owner}:process:{uuid.uuid4().hex}",
            paths=None,
        )
        try:
            return self._run_process_with_profile(values, profile)
        finally:
            lease.release()

    def _run_process_with_profile(
        self, values: Mapping[str, Any], profile: ExecutionProfile
    ) -> Any:
        command = str(values.get("command") or "").strip()
        if not command:
            raise WorkspaceEditError("process command is required")
        if profile in {ExecutionProfile.DOCKER, ExecutionProfile.VERIFIED_FIX}:
            if self.sandbox_call is not None:
                return self.sandbox_call(
                    str(self.workspace.root), command, dict(values)
                )
            from execution.sandbox import execute_sandboxed

            return execute_sandboxed(
                str(self.workspace.root),
                command,
                int(values.get("timeout_s", 120)),
                **self._sandbox_options(values),
            )
        if profile == ExecutionProfile.NATIVE_OS:
            if self.native_call is not None:
                return self.native_call(str(self.workspace.root), command, dict(values))
            return execute_native_sandboxed(
                self.workspace.root,
                command,
                timeout_s=float(values.get("timeout_s", 120)),
                allow_network=bool(values.get("allow_network", False)),
                env=values.get("env"),
                max_output_bytes=int(values.get("max_output_bytes", 1_000_000)),
            ).to_dict()
        return execute_local(
            self.workspace.root,
            command,
            timeout_s=float(values.get("timeout_s", 120)),
            cancel_event=values.get("cancel_event"),
            cancellation_token=values.get("cancellation_token"),
            env=values.get("env"),
            max_output_bytes=int(values.get("max_output_bytes", 1_000_000)),
            memory_limit_mb=values.get("memory_limit_mb"),
            max_processes=values.get("max_processes"),
            cpu_seconds=values.get("cpu_seconds"),
        ).to_dict()

    @staticmethod
    def _json_value(value: Any) -> Any:
        return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value

    def _read_cap(self) -> int:
        """The ingress byte cap for a file-content read.

        A repository file is bounded by the workspace's own
        ``max_file_bytes`` before it gets here, so this cap is a second
        ceiling rather than the only one. It is the DEFAULT ingress cap, not
        the verification one: a read is a diagnostic, and a truncated source
        file is a diagnostic the model cannot act on, so there is no reason to
        spend the 4x here.
        """
        return OUTPUT_CAP_BYTES

    def _read_tool(self, tool: str, args: Mapping[str, Any]) -> Any:
        if tool == "read":
            path = str(args.get("path") or "")
            # THE SUBPROCESS-OUTPUT INGRESS, file-read half. A repository file
            # is untrusted CONTENT exactly as much as a command's stdout is,
            # and it is the more direct route: `read` hands the bytes to the
            # model on the same turn that fetched them.
            #
            # The pre-existing fence was the WRITE path only
            # (`apply_exact_edit` / `write` refuse secret-shaped content), plus
            # a NAME-based refusal of `.env` / `*.pem` / `id_rsa*` / etc. That
            # left a real hole, measured on this tree before this change:
            # reading `config.py` containing
            # `API_KEY = "sk-live-AAAA..."` returned the key verbatim in the
            # tool result while `cat config.py` through the sandbox returned
            # `[REDACTED_SECRET]`. Two fences protecting the same secret, and
            # the weaker one on the path a model actually uses.
            #
            # REDACTED, not refused. A refusal would break legitimate work --
            # a test fixture with a fake key, a config sample, a lockfile with
            # an integrity hash -- and a boundary that cries wolf on fixtures
            # gets worked around. The receipt says what happened, so a caller
            # can tell a redacted read from a clean one.
            #
            # NOT fenced here, and deliberately so: PROMPT INJECTION. A file
            # saying "ignore all previous instructions" reaches the model from
            # this path today, and from the sandbox-output path too. The
            # authority for that is `shared.security.review_untrusted_source`
            # (`shared/` is Terminal 5's), which has API and coverage but no
            # production call site for `issue` or `repository_instructions`.
            # Filed as a cross-terminal request; see execution/AGENTS.md.
            sealed, report = seal_output(
                self.workspace.read_text(path), cap_bytes=self._read_cap()
            )
            return {
                "path": path,
                "text": sealed,
                "revision": self.workspace.revision(path).to_dict(),
                "ingress": report.to_dict(),
            }
        if tool == "list":
            return self.workspace.list_directory(
                args.get("path"), max_entries=int(args.get("max_entries", 500))
            )
        if tool == "image":
            return self.workspace.read_image(
                str(args.get("path") or ""),
                include_data=bool(args.get("include_data", True)),
            )
        if tool == "glob":
            pattern = str(args.get("pattern") or "**/*")
            raw = pattern.replace("\\", "/")
            if (
                raw.startswith("/")
                or re.match(r"^[A-Za-z]:", raw)
                or ".." in Path(raw).parts
            ):
                raise WorkspaceSecurityError(
                    "GLOB pattern must stay inside the repository"
                )
            base = self.workspace.root
            subpath = str(args.get("path") or "").strip()
            if subpath:
                _relative, base = self.workspace._path_for_read(subpath)
            hits: List[str] = []
            for hit in base.glob(pattern):
                if hit.is_symlink():
                    continue
                try:
                    relative = hit.relative_to(self.workspace.root).as_posix()
                except ValueError:
                    continue
                if is_protected_path(relative, self.workspace.protected_paths):
                    continue
                hits.append(relative)
                if len(hits) >= int(args.get("max_results", 500)):
                    break
            return sorted(hits)
        pattern_text = str(args.get("pattern") or "")
        if not pattern_text:
            raise WorkspaceEditError("GREP needs a pattern")
        try:
            matcher = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            raise WorkspaceEditError(f"GREP pattern is invalid: {exc}") from exc
        subpath = str(args.get("path") or "").strip()
        if subpath:
            _relative, selected = self.workspace._path_for_read(subpath)
            paths = [selected] if selected.is_file() else list(selected.rglob("*"))
        else:
            paths = list(self.workspace.root.rglob("*"))
        include_glob = str(args.get("glob") or "")
        hits = []
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                relative = path.relative_to(self.workspace.root).as_posix()
            except ValueError:
                continue
            if is_protected_path(relative, self.workspace.protected_paths):
                continue
            if include_glob and not fnmatch.fnmatch(relative, include_glob):
                continue
            try:
                # BOUNDED before the read, not after. The previous version
                # called `path.read_text(errors="strict")` with no size check,
                # so a single multi-gigabyte file in the tree made a `grep`
                # allocate that much -- the same class as the unbounded
                # `flake.run_local_command` capture this round removed. The
                # bound is the workspace's own `max_file_bytes`, which is the
                # number every other read path in this file already honours.
                if path.stat().st_size > int(self.workspace.max_file_bytes):
                    continue
                text = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if matcher.search(line):
                    # THE SUBPROCESS-OUTPUT INGRESS, grep half. A grep for
                    # `password` or `api_key` is the single highest-volume
                    # route untrusted file CONTENT takes to a model: it returns
                    # the matching LINES, so the lines that matched the secret
                    # search are exactly the lines that carry the secret. The
                    # NAME-based `.env` / `*.pem` refusal does not help here at
                    # all, because the hit is in some other file.
                    sealed, _report = seal_output(line[:160], cap_bytes=4_000)
                    hits.append(f"{relative}:{number}: {sealed}")
                    if len(hits) >= int(args.get("max_results", 200)):
                        return hits
        return hits

    def _guard_preexisting(
        self, paths: Sequence[PathLike], values: Mapping[str, Any]
    ) -> None:
        if values.get("allow_preexisting_change") is True:
            return
        dirty = list(self.workspace.identity.dirty_paths)
        for candidate in paths:
            normalized = normalize_relative_path(candidate)
            if any(_lease_paths_overlap(normalized, existing) for existing in dirty):
                raise WorkspaceConflictError(
                    f"pre-existing user change requires explicit takeover: {normalized}"
                )

    def _expected(self, values: Mapping[str, Any]) -> Any:
        for key in (
            "expected_revision",
            "expected_sha256",
            "expected_file_hash",
            "expected_hash",
        ):
            if values.get(key) is not None:
                return values[key]
        if self.require_revisions:
            raise WorkspaceConflictError("typed mutation requires an expected revision")
        return None

    def _git(
        self, arguments: Sequence[str], *, max_chars: int = 100_000
    ) -> Dict[str, Any]:
        """Run one fixed-argv git read and return a SEALED dict.

        THE SUBPROCESS-OUTPUT INGRESS for the `git_*` tool family. This is a
        real subprocess whose stdout is model-visible, and it had no
        redaction at all: `git show` of a commit whose diff contains a
        `.env` line, or `git log` over a message with a token, put the secret
        straight into a tool result. The local truncation below is a LENGTH
        bound only; the secret in the retained head and tail is what the
        ingress removes.

        `max_chars` is MODEL-SUPPLIED and unbounded above, so the ingress cap
        is the real ceiling — a caller cannot buy past it by asking for more
        characters, which is the same rule `ToolPolicyEngine` applies to
        every other bound.
        """
        command = [
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "core.fsmonitor=false",
            *arguments,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.workspace.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(max_chars and 30),
                check=False,
                env=_git_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceExecutionError(f"git command failed: {exc}") from exc
        output = completed.stdout
        if len(output) > max_chars:
            output = (
                output[: max_chars // 2]
                + "\n[truncated]\n"
                + output[-(max_chars // 2) :]
            )
        return seal_mapping(
            {
                "exit_code": completed.returncode,
                "stdout": output,
                "stderr": completed.stderr[-max_chars:],
            }
        )

    def _git_tool(self, tool: str, values: Mapping[str, Any]) -> Dict[str, Any]:
        max_chars = int(values.get("max_chars", 100_000))
        if tool == "git_status":
            return self._git(
                ["status", "--short", "--branch", "--untracked-files=all"],
                max_chars=max_chars,
            )
        path = str(values.get("path") or "")
        if path:
            _relative, _full = self.workspace._path_for_read(path)
        if tool == "git_diff":
            revision = str(values.get("revision") or "")
            if revision.startswith("-"):
                raise WorkspaceEditError("git revision may not start with '-'")
            args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
            if revision:
                args.extend([revision, "--"])
            elif path:
                args.extend(["--", path])
            return self._git(args, max_chars=max_chars)
        if tool == "git_log":
            revision = str(values.get("revision") or "HEAD")
            if revision.startswith("-"):
                raise WorkspaceEditError("git revision may not start with '-'")
            args = [
                "log",
                "--no-ext-diff",
                "--no-color",
                f"-n{max(1, min(int(values.get('max_count', 50)), 500))}",
                revision,
            ]
            if path:
                args.extend(["--", path])
            return self._git(args, max_chars=max_chars)
        if tool == "git_show":
            revision = str(values.get("revision") or "HEAD")
            if revision.startswith("-"):
                raise WorkspaceEditError("git revision may not start with '-'")
            args = ["show", "--no-ext-diff", "--no-textconv", "--no-color", revision]
            if path:
                args.extend(["--", path])
            return self._git(args, max_chars=max_chars)
        if tool == "git_blame":
            if not path:
                raise WorkspaceEditError("git_blame requires path")
            start = max(1, int(values.get("start_line", 1)))
            end = max(start, int(values.get("end_line", start)))
            return self._git(
                ["blame", f"-L{start},{end}", "--", path], max_chars=max_chars
            )
        if tool == "git_branch":
            branch = str(values.get("branch") or "")
            if branch.startswith("-"):
                raise WorkspaceEditError("git branch may not start with '-'")
            args = ["branch", "--list"]
            if branch:
                args.append(branch)
            return self._git(args, max_chars=max_chars)
        if tool == "git_worktree":
            return self._git(["worktree", "list", "--porcelain"], max_chars=max_chars)
        raise WorkspaceEditError(f"unsupported git tool: {tool}")

    def _authorize_redirect(
        self,
        tool: str,
        initial_url: str,
        final_url: str,
        values: Mapping[str, Any],
    ) -> None:
        if self.policy_engine is None:
            return
        try:
            initial_host = (urlparse(initial_url).hostname or "").lower()
            final_host = (urlparse(final_url).hostname or "").lower()
        except ValueError:
            raise WorkspaceSecurityError("redirect URL is invalid") from None
        if not final_host or final_host == initial_host:
            return
        decision = self.policy_engine.evaluate(
            tool,
            {**dict(values), "url": final_url},
            side_effect_class=policy_for_tool(tool).effect,
        )
        if decision.action != "allow":
            raise WorkspaceSecurityError(
                f"redirect domain is not allowed by policy: {final_host}"
            )

    def _web_fetch(self, values: Mapping[str, Any]) -> Any:
        if self.network_call is not None:
            return self.network_call(dict(values))
        from harness.webfetch import fetch_webpage

        requested_url = str(values.get("url") or "")
        result = fetch_webpage(
            requested_url,
            int(values.get("timeout_s", 15)),
            int(values.get("max_bytes", 1_048_576)),
            int(values.get("max_chars", 3000)),
            int(values.get("max_redirects", 3)),
        )
        self._authorize_redirect("web_fetch", requested_url, str(result.url), values)
        return self._json_value(result)

    def _mcp(self, values: Mapping[str, Any]) -> Any:
        if self.mcp_call is not None:
            value = self.mcp_call(dict(values))
            child = value.get("child") if isinstance(value, Mapping) else value
            if child is not None and any(
                callable(getattr(child, method, None))
                for method in ("cancel", "terminate", "close")
            ):
                self.mcp_registry.register(child)
                self.mcp_registry.unregister(child)
            return value
        from memory.mcp_client import call_mcp_tool

        return call_mcp_tool(
            str(values.get("server") or ""),
            str(values.get("name") or ""),
            dict(values.get("args") or {}),
            cwd=str(self.workspace.root),
            env=values.get("env"),
        )

    def _memory(self, values: Mapping[str, Any]) -> Any:
        if self.memory_call is not None:
            return self.memory_call(dict(values))
        from memory.decision_store import open_default_store

        store = open_default_store()
        action = str(values.get("action") or "query").lower()
        if action == "query":
            rows = store.search(
                str(values.get("query") or ""),
                int(values.get("limit", 20)),
                repo_path=str(self.workspace.root),
            )
            return {"action": action, "results": [row.as_dict() for row in rows]}
        if action == "record":
            row_id = store.record(
                str(values.get("text") or ""),
                category=str(values.get("category") or "general"),
                source="agent",
                task_id=str(values.get("task_id") or ""),
                repo_path=str(self.workspace.root),
                dedupe=bool(values.get("dedupe", False)),
            )
            return {"action": action, "id": row_id}
        raise WorkspaceEditError(f"unsupported memory action: {action}")

    def _control(self, tool: str, values: Mapping[str, Any]) -> Any:
        if self.control_call is not None:
            return self.control_call(tool, dict(values))
        with self._state_lock:
            if tool == "todo":
                items = values.get("items")
                if not isinstance(items, list) or not all(
                    isinstance(item, str) and item.strip() for item in items
                ):
                    raise WorkspaceEditError(
                        "todo items must be a list of non-empty strings"
                    )
                if values.get("replace", True):
                    self.session_state["todo"] = list(items)
                else:
                    self.session_state["todo"].extend(
                        item for item in items if item not in self.session_state["todo"]
                    )
                return {"todo": list(self.session_state["todo"])}
            if tool == "plan":
                steps = values.get("steps")
                if not isinstance(steps, list) or not all(
                    isinstance(item, str) and item.strip() for item in steps
                ):
                    raise WorkspaceEditError(
                        "plan steps must be a list of non-empty strings"
                    )
                self.session_state["plan"] = list(steps)
                return {"plan": list(steps), "notes": str(values.get("notes") or "")}
            if tool == "question":
                question = str(values.get("question") or "").strip()
                if not question:
                    raise WorkspaceEditError("question is required")
                self.session_state["question"] = {
                    "question": question,
                    "choices": list(values.get("choices") or []),
                }
                return {"status": "needs_input", **self.session_state["question"]}
            if tool == "task":
                task = {
                    "id": str(values.get("task_id") or _new_id("task")),
                    "description": str(values.get("description") or ""),
                    "status": str(values.get("status") or "pending"),
                }
                if not task["description"]:
                    raise WorkspaceEditError("task description is required")
                self.session_state["tasks"].append(task)
                return {"task": dict(task)}
            if tool == "finish":
                self.session_state["finish"] = {
                    "answer": str(values.get("answer") or ""),
                    "checks": list(values.get("checks") or []),
                }
                return {
                    "status": "completed_unverified",
                    **self.session_state["finish"],
                }
        raise WorkspaceEditError(f"unsupported control tool: {tool}")

    def _quality(
        self, tool: str, values: Mapping[str, Any], profile: ExecutionProfile
    ) -> Any:
        if tool in {"test", "verify"}:
            if self.verify_call is not None:
                value = self.verify_call(str(self.workspace.root), dict(values))
            else:
                from execution.verify import verify

                value = verify(
                    str(self.workspace.root),
                    values.get("target_test"),
                    rerun_for_flake_check=int(values.get("rerun_for_flake_check", 1)),
                    test_command=values.get("test_command", values.get("command")),
                    verify_timeout_s=int(
                        values.get("verify_timeout_s", values.get("timeout_s", 300))
                    ),
                )
            return self._json_value(value)
        defaults = {
            "lint": "python -m ruff check .",
            "typecheck": "python -m mypy .",
            "build": "python -m build",
        }
        command_values = dict(values)
        command_values["command"] = str(values.get("command") or defaults[tool])
        return self._run_process(command_values, profile)

    def execute(
        self,
        tool: str,
        args: Optional[Mapping[str, Any]] = None,
        *,
        sandboxed: bool = False,
    ) -> ToolResult:
        """Execute one typed call without changing policy or execution profile."""
        name = str(tool or "").strip().lower()
        canonical = {
            "bash": "shell",
            "fetch": "web_fetch",
            "search": "web_search",
            "mcp_call": "mcp",
            "ask": "question",
            "done": "finish",
        }.get(name, name)
        if canonical not in self._CANONICAL_TOOLS and canonical != "sandbox":
            policy = policy_for_tool(name)
            return ToolResult(
                False,
                name,
                policy.effect,
                policy.backend,
                error=f"unknown tool: {name}",
                profile=self.profile.value,
            )
        policy = policy_for_tool(canonical)
        profile = self._execution_profile(canonical, sandboxed)
        if (
            profile
            in {
                ExecutionProfile.DOCKER,
                ExecutionProfile.NATIVE_OS,
                ExecutionProfile.VERIFIED_FIX,
            }
            and canonical in self._PROCESS_TOOLS
        ):
            policy = dataclasses.replace(
                policy,
                backend=profile.value,
                sandboxed=profile != ExecutionProfile.NATIVE_OS,
            )
        values = dict(args or {})
        approved, policy_value, approval_error = self._authorize(
            canonical, values, policy
        )
        if not approved:
            return ToolResult(
                False,
                canonical,
                policy.effect,
                policy.backend,
                error=approval_error,
                policy=policy_value,
                profile=profile.value,
            )
        try:
            operation_id: Optional[str] = None
            if canonical in {"read", "glob", "grep", "list", "image"}:
                value = self._read_tool(canonical, values)
            elif canonical == "edit":
                if (
                    not values.get("path")
                    or values.get("old_string") is None
                    or values.get("new_string") is None
                ):
                    raise WorkspaceEditError(
                        "edit requires path, old_string, and new_string"
                    )
                expected = self._expected(values)
                self._guard_preexisting([str(values["path"])], values)
                result = self.workspace.apply_exact_edit(
                    values["path"],
                    str(values["old_string"]),
                    str(values["new_string"]),
                    expected_sha256=expected if isinstance(expected, str) else None,
                    expected_revision=expected
                    if not isinstance(expected, str)
                    else None,
                    expected_hash=expected if isinstance(expected, str) else None,
                    hunk_id=values.get("hunk_id"),
                )
                value = result.to_dict()
                operation_id = result.operation_id
            elif canonical == "apply_patch":
                expected_revisions = values.get("expected_revisions")
                if not isinstance(expected_revisions, Mapping):
                    raise WorkspaceConflictError(
                        "apply_patch requires expected_revisions"
                    )
                for path in _parse_unified_patch(str(values.get("patch") or "")):
                    self._guard_preexisting([path], values)
                results = self.workspace.apply_unified_patch(
                    str(values.get("patch") or ""),
                    expected_revisions=expected_revisions,
                    allow_delete=bool(values.get("allow_delete", True)),
                )
                value = {"files": [result.to_dict() for result in results]}
                operation_id = results[0].operation_id if results else None
            elif canonical == "write":
                if not values.get("path") or values.get("content") is None:
                    raise WorkspaceEditError("write requires path and content")
                path = str(values["path"])
                self._guard_preexisting([path], values)
                current = self.workspace.revision(path)
                overwrite = bool(values.get("overwrite", current.exists))
                expected = self._expected(values) if current.exists else None
                result = self.workspace.write_file(
                    path,
                    values["content"],
                    expected_sha256=expected if isinstance(expected, str) else None,
                    expected_revision=expected
                    if not isinstance(expected, str)
                    else None,
                    expected_hash=expected if isinstance(expected, str) else None,
                    overwrite=overwrite,
                    hunk_id=values.get("hunk_id"),
                )
                value = result.to_dict()
                operation_id = result.operation_id
            elif canonical == "rename":
                source = str(values.get("source_path") or "")
                destination = str(values.get("destination_path") or "")
                if not source or not destination:
                    raise WorkspaceEditError(
                        "rename requires source_path and destination_path"
                    )
                self._guard_preexisting([source, destination], values)
                expected = self._expected(values)
                result = self.workspace.rename_file(
                    source,
                    destination,
                    expected_sha256=expected if isinstance(expected, str) else None,
                    expected_revision=expected
                    if not isinstance(expected, str)
                    else None,
                    hunk_id=values.get("hunk_id"),
                )
                value = result.to_dict()
                operation_id = result.operation_id
            elif canonical == "delete":
                path = str(values.get("path") or "")
                if not path:
                    raise WorkspaceEditError("delete requires path")
                self._guard_preexisting([path], values)
                expected = self._expected(values)
                result = self.workspace.delete_file(
                    path,
                    expected_sha256=expected if isinstance(expected, str) else None,
                    expected_revision=expected
                    if not isinstance(expected, str)
                    else None,
                    hunk_id=values.get("hunk_id"),
                )
                value = result.to_dict()
                operation_id = result.operation_id
            elif canonical == "undo":
                operation_id = values.get("operation_id")
                hunk_id = values.get("hunk_id")
                targets = values.get("targets") or (
                    [values["path"]] if values.get("path") else None
                )
                if targets is not None:
                    self._guard_preexisting([str(item) for item in targets], values)
                undo_result = self.workspace.undo(
                    str(operation_id) if operation_id else None,
                    hunk_id=str(hunk_id) if hunk_id else None,
                    steps=values.get("steps", 1),
                    targets=targets,
                )
                value = (
                    undo_result.to_dict()
                    if hasattr(undo_result, "to_dict")
                    else dict(undo_result)
                )
                operation_id = None
            elif canonical.startswith("git_"):
                value = self._git_tool(canonical, values)
            elif canonical == "shell":
                value = self._run_process(values, profile)
            elif canonical == "process":
                if values.get("background"):
                    if profile != ExecutionProfile.LOCAL_TRUSTED:
                        raise WorkspaceExecutionError(
                            "background processes require the local trusted profile"
                        )
                    process_lease = self.workspace.lease(
                        f"{self.workspace.owner}:process:{uuid.uuid4().hex}",
                        paths=None,
                    )
                    background = self.processes.add(
                        start_background_process(
                            self.workspace.root,
                            str(values.get("command") or ""),
                            cwd=values.get("cwd"),
                            env=values.get("env"),
                            max_output_bytes=int(
                                values.get("max_output_bytes", 1_000_000)
                            ),
                            pty=bool(values.get("pty", False)),
                            workspace_lease=process_lease,
                        )
                    )
                    value = background.status()
                elif values.get("process_id"):
                    value = self.processes.get(str(values["process_id"])).status()
                else:
                    value = self._run_process(values, profile)
            elif canonical == "process_read_output":
                value = self.processes.get(
                    str(values.get("process_id") or "")
                ).read_output(
                    stdout_offset=int(values.get("stdout_offset", 0)),
                    stderr_offset=int(values.get("stderr_offset", 0)),
                    max_chars=int(values.get("max_chars", 20_000)),
                )
            elif canonical == "process_write_stdin":
                value = self.processes.get(
                    str(values.get("process_id") or "")
                ).write_stdin(
                    str(values.get("data") or ""),
                    append_newline=bool(values.get("append_newline", False)),
                )
            elif canonical == "process_kill":
                process = self.processes.get(str(values.get("process_id") or ""))
                value = process.kill(force=bool(values.get("force", True)))
                self.processes.remove(process.id)
            elif canonical in {"test", "verify", "lint", "typecheck", "build"}:
                value = self._quality(canonical, values, profile)
            elif canonical == "sandbox":
                value = self._run_process(values, ExecutionProfile.DOCKER)
            elif canonical == "web_fetch":
                value = self._web_fetch(values)
            elif canonical == "web_search":
                if self.search_call is not None:
                    value = self.search_call(dict(values))
                else:
                    from harness.webfetch import fetch_webpage

                    query = str(values.get("query") or "").strip()
                    if not query:
                        raise WorkspaceEditError("web search query is required")
                    endpoint = "https://html.duckduckgo.com/html/?" + urlencode(
                        {"q": query[:512]}
                    )
                    result = fetch_webpage(
                        endpoint,
                        int(values.get("timeout_s", 15)),
                        1_048_576,
                        int(values.get("max_chars", 8000)),
                        2,
                    )
                    self._authorize_redirect(
                        "web_search", endpoint, str(result.url), values
                    )
                    value = {
                        "query": query,
                        "provider": "duckduckgo-html",
                        "result": self._json_value(result),
                    }
            elif canonical == "mcp":
                value = self._mcp(values)
            elif canonical == "memory":
                value = self._memory(values)
            elif canonical in {"question", "todo", "plan", "task", "finish"}:
                value = self._control(canonical, values)
            else:
                raise WorkspaceEditError(f"unknown tool: {name}")
            return ToolResult(
                True,
                canonical,
                policy.effect,
                policy.backend,
                self._json_value(value),
                operation_id=str(operation_id) if operation_id else None,
                policy=policy_value,
                profile=profile.value,
            )
        except Exception as exc:
            return ToolResult(
                False,
                canonical,
                policy.effect,
                policy.backend,
                error=str(exc),
                policy=policy_value,
                profile=profile.value,
            )


def execute_typed_tool(
    workspace: Union[Workspace, PathLike],
    tool: str,
    args: Optional[Mapping[str, Any]] = None,
    *,
    backend: Optional[SafeToolBackend] = None,
    sandboxed: bool = False,
) -> ToolResult:
    """Execute a typed tool through the safe workspace backend."""
    selected = backend or SafeToolBackend(
        workspace if isinstance(workspace, Workspace) else Workspace(workspace)
    )
    return selected.execute(tool, args, sandboxed=sandboxed)
