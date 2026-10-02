"""Atomic checkpoint storage for resumable kernel runs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from harness.trace import redact_secrets

from .contracts import Checkpoint

_IDENTITY_FIELDS = (
    "repository_identity",
    "request_identity",
    "revision_identity",
    "resume_namespace",
    "effort_identity",
)
_REVISION_CONFIG_KEYS = (
    "resume_revision",
    "base_revision",
    "repository_revision",
    "git_revision",
    "revision",
)
_EXCLUDED_REVISION_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
}
_EXCLUDED_ROOT_REVISION_PARTS = {"logs"}


def canonical_repository_identity(repo_path: str) -> str:
    """Return the canonical repository path used by strict resume identity."""
    value = str(repo_path or "")
    if not value:
        return ""
    try:
        resolved = Path(value).expanduser().resolve(strict=False)
    except OSError:
        resolved = Path(os.path.abspath(os.path.expanduser(value)))
    return os.path.normcase(str(resolved))


def request_fingerprint(request: str) -> str:
    """Return a non-secret identity for the exact strict-run request."""
    return hashlib.sha256(
        str(request or "").encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def _git_output(root: Path, args: List[str]) -> Optional[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _tree_revision(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        digest.update(b"missing")
        return "tree:" + digest.hexdigest()
    files: List[Path] = []
    for directory, dirs, names in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if name not in _EXCLUDED_REVISION_PARTS
            and not (
                name in _EXCLUDED_ROOT_REVISION_PARTS
                and Path(directory).resolve() == root.resolve()
            )
        ]
        for name in names:
            candidate = Path(directory) / name
            relative_parts = candidate.relative_to(root).parts
            if not relative_parts or relative_parts[0] in _EXCLUDED_ROOT_REVISION_PARTS:
                continue
            if any(part in _EXCLUDED_REVISION_PARTS for part in relative_parts[1:]):
                continue
            if candidate.is_symlink() or not candidate.is_file():
                continue
            files.append(candidate)
    for candidate in sorted(files, key=lambda item: item.as_posix()):
        try:
            relative = candidate.relative_to(root).as_posix()
            size = candidate.stat().st_size
            digest.update(relative.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            with candidate.open("rb") as handle:
                remaining = min(size, 4 * 1024 * 1024)
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    digest.update(chunk)
                    remaining -= len(chunk)
        except OSError:
            continue
    return "tree:" + digest.hexdigest()


def revision_identity(
    repo_path: str,
    config: Optional[Mapping[str, Any]] = None,
    *,
    include_working_tree: bool = True,
) -> str:
    """Return a stable strict-checkpoint revision identity.

    Strict live-workspace resumes use the immutable Git base revision by
    default; callers that own an unmodified source checkout may include the
    working-tree state as well.
    """
    values = dict(config or {})
    for key in _REVISION_CONFIG_KEYS:
        explicit = values.get(key)
        if explicit is not None and str(explicit).strip():
            payload = str(explicit).strip().encode("utf-8", errors="surrogatepass")
            return "explicit:" + hashlib.sha256(payload).hexdigest()
    repository = canonical_repository_identity(repo_path)
    if not repository:
        return "empty"
    root = Path(repository)
    head = _git_output(root, ["rev-parse", "--verify", "HEAD"])
    if head:
        branch = _git_output(root, ["branch", "--show-current"]) or ""
        status = ""
        diff = ""
        if include_working_tree:
            status = _git_output(root, ["status", "--porcelain=v1", "-z"]) or ""
            diff = (
                _git_output(
                    root,
                    [
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--binary",
                        "HEAD",
                        "--",
                    ],
                )
                or ""
            )
        payload = "\0".join((head, branch, status, diff)).encode(
            "utf-8", errors="surrogatepass"
        )
        return "git:" + hashlib.sha256(payload).hexdigest()
    if include_working_tree:
        return _tree_revision(root)
    return "non-git:" + hashlib.sha256(repository.encode("utf-8")).hexdigest()


def effort_identity_for(
    config: Optional[Mapping[str, Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return the effort rung that identifies this run (AGT-08).

    Resuming a high-effort run at low effort is a lie about the run: the
    second half of the work would come from a model thinking less hard than
    the first half, and every cost/quality claim about the whole would be
    false. The rung therefore joins the strict resume identity, and a
    mismatch takes the existing fail-closed path (a fresh run) rather than
    continuing quietly at a different level.

    Read from ``metadata``/``workspace_policy`` the same way the revision
    keys are, and resolved through the ONE authority so an alias such as
    ``hi`` and the word ``high`` produce the same identity. A broken or
    absent authority degrades to the raw configured string, which still
    distinguishes one rung from another.
    """
    values: Dict[str, Any] = {}
    for source in (config or {}, metadata or {}):
        if isinstance(source, Mapping):
            values.update(source)
    try:
        from runtime.model_capabilities import resolve_effort

        return str(resolve_effort(values)[0] or "auto")
    except Exception:
        return str(values.get("effort") or "").strip().lower() or "auto"


def with_effort(
    metadata: Optional[Mapping[str, Any]] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return ``metadata`` with the run's effort rung folded in (AGT-08).

    The rung lives in the RUN's config (``Task.config``/``harness.config``),
    while the resume identity is built from a ``RunSpec``'s metadata. This is
    the one seam that carries it across, so every strategy gets the same
    identity without each re-deriving the resolution. An explicit metadata
    value wins, so a caller that already knows the rung is not overruled.
    """
    values: Dict[str, Any] = dict(metadata or {})
    if not str(values.get("effort") or "").strip():
        candidate = str((config or {}).get("effort") or "").strip()
        if candidate:
            values["effort"] = candidate
    return values


def checkpoint_identity(
    repository_identity: str,
    request: str,
    *,
    workspace_policy: Optional[Mapping[str, Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    resume_namespace: str = "",
) -> Dict[str, str]:
    """Build the repository/request/revision tuple for a strict checkpoint."""
    values: Dict[str, Any] = {}
    for source in (workspace_policy or {}, metadata or {}):
        if isinstance(source, Mapping):
            values.update(source)
    return {
        "repository_identity": canonical_repository_identity(repository_identity),
        "request_identity": request_fingerprint(request),
        "revision_identity": revision_identity(
            repository_identity, values, include_working_tree=False
        ),
        "resume_namespace": str(resume_namespace or ""),
        "effort_identity": effort_identity_for(workspace_policy, metadata),
    }


def is_continuation_request(request: str) -> bool:
    """Return whether a request is the explicit compatibility continuation token."""
    return str(request or "").strip().lower() in {
        "continue",
        "continue the active task",
    }


def checkpoint_identity_matches(
    checkpoint: Any,
    identity: Optional[Mapping[str, Any]],
    *,
    allow_request_alias: bool = False,
) -> bool:
    """Return whether a strict checkpoint matches expected identity fields."""
    if not isinstance(checkpoint, Checkpoint) or not isinstance(identity, Mapping):
        return False
    fields = _IDENTITY_FIELDS
    if allow_request_alias:
        fields = tuple(field for field in fields if field != "request_identity")
    return all(
        str(getattr(checkpoint, field, "")) == str(identity.get(field, ""))
        for field in fields
    ) and all(str(identity.get(field, "")) for field in _IDENTITY_FIELDS)


class CheckpointStore:
    """Persist and recover checkpoints without exposing partial JSON."""

    def __init__(
        self,
        root: Path | str,
        session_id: str = "",
        run_id: str = "",
        filename: str = "checkpoint.json",
    ) -> None:
        self.root = Path(root)
        self.session_id = str(session_id or "")
        self.run_id = str(run_id or "")
        self.filename = str(filename or "checkpoint.json")
        self._lock = threading.RLock()
        self._warnings: List[str] = []
        self.last_save_succeeded = True

    @property
    def path(self) -> Path:
        """Return the active checkpoint path."""
        if self.run_id and self.root.suffix.lower() != ".json":
            return self.root / self.run_id / self.filename
        return self.root

    @property
    def warnings(self) -> List[str]:
        """Return explicit checkpoint warnings."""
        return list(self._warnings)

    def save(self, checkpoint: Checkpoint) -> bool:
        """Atomically write a checkpoint and return whether it persisted."""
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(
                    f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
                )
                temporary.write_text(
                    json.dumps(
                        redact_secrets(checkpoint.to_dict()),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
                self.last_save_succeeded = True
                return True
            except (OSError, TypeError, ValueError) as exc:
                self.last_save_succeeded = False
                self._warn(f"checkpoint unwritable: {exc}")
                return False

    def load(self) -> Optional[Checkpoint]:
        """Load a checkpoint, returning ``None`` for missing/corrupt data."""
        checkpoint, _ = self.load_with_warnings()
        return checkpoint

    def load_with_warnings(self) -> Tuple[Optional[Checkpoint], List[str]]:
        """Load a checkpoint and report corruption or read failures."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, []
        except OSError as exc:
            message = f"checkpoint unreadable: {exc}"
            self._warn(message)
            return None, [message]
        try:
            raw = json.loads(text)
        except ValueError as exc:
            message = f"checkpoint corrupt JSON: {exc}"
            self._warn(message)
            return None, [message]
        if not isinstance(raw, dict):
            message = "checkpoint corrupt: root is not an object"
            self._warn(message)
            return None, [message]
        try:
            return Checkpoint.from_dict(raw), []
        except (TypeError, ValueError) as exc:
            message = f"checkpoint invalid: {exc}"
            self._warn(message)
            return None, [message]

    def list_checkpoints(self) -> List[Path]:
        """List checkpoint files below the configured root."""
        try:
            if self.path.is_file():
                return [self.path]
            return sorted(self.root.glob("*/" + self.filename))
        except OSError as exc:
            self._warn(f"checkpoint listing failed: {exc}")
            return []

    def latest(self) -> Optional[Checkpoint]:
        """Return the newest checkpoint by modification time."""
        candidates = self.list_checkpoints()
        if not candidates:
            return None
        try:
            selected = max(candidates, key=lambda path: path.stat().st_mtime)
            raw = json.loads(selected.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("checkpoint root is not an object")
            return Checkpoint.from_dict(raw)
        except (OSError, TypeError, ValueError) as exc:
            self._warn(f"checkpoint latest read failed: {exc}")
            return None

    def make_checkpoint(
        self,
        *,
        last_event_sequence: int,
        model_context_references: Optional[List[str]] = None,
        agent_owned_changes: Optional[List[str]] = None,
        active_processes: Optional[List[Dict[str, Any]]] = None,
        spend: float = 0.0,
        resume_token: str = "",
        turn_id: str = "",
        identity: Optional[Mapping[str, Any]] = None,
    ) -> Checkpoint:
        """Create and persist a checkpoint with current run identity."""
        values = dict(identity or {})
        checkpoint = Checkpoint(
            last_event_sequence=int(last_event_sequence),
            model_context_references=list(model_context_references or []),
            agent_owned_changes=list(agent_owned_changes or []),
            active_processes=list(active_processes or []),
            spend=float(spend),
            resume_token=resume_token,
            session_id=self.session_id,
            run_id=self.run_id,
            turn_id=turn_id,
            created_at=time.time(),
            repository_identity=str(values.get("repository_identity", "")),
            request_identity=str(values.get("request_identity", "")),
            revision_identity=str(values.get("revision_identity", "")),
            resume_namespace=str(values.get("resume_namespace", "")),
            effort_identity=str(values.get("effort_identity", "")),
        )
        self.save(checkpoint)
        return checkpoint

    def _warn(self, message: str) -> None:
        with self._lock:
            if message not in self._warnings:
                self._warnings.append(message)
