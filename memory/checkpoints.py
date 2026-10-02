"""Durable workspace checkpoints with conflict-safe restore and review APIs.

The checkpoint store is a persistence adapter owned by the memory layer. It
captures an immutable file manifest plus a shadow Git repository, and can
optionally capture a conversation snapshot. Restore is preflighted against the
file and conversation revisions observed at checkpoint creation, so a later
user edit is reported as a conflict instead of being overwritten.

AGT-09 adds a SECOND, deliberately different mechanism on the same boundary:
``StagedSnapshotStore``. A checkpoint stack is not scriptable and not
idempotent - it is a list you pop. The staged store is a content-addressed
private object store plus an append-only journal, so it can be driven from a
script, re-run without consequence, and asked for a RANGE rather than asked to
pop. It never creates a commit, never moves a branch, and never touches the
user's index or ``HEAD``; the only thing it writes inside the repository is the
file content a revert is asked to put back.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_RESTORE_SCOPE",
    "EXCLUSION_REASONS",
    "RESTORE_SCOPES",
    "CheckpointConflictError",
    "CheckpointCorruptError",
    "CheckpointError",
    "CheckpointManager",
    "CheckpointNotFoundError",
    "CheckpointResult",
    "RestoreResult",
    "StagedSnapshotStore",
    "capture_staged_snapshot",
    "checkpoint_before_mutation",
    "checkpoint_guard",
    "commit_staged_revert",
    "create_checkpoint",
    "diff_checkpoint",
    "discard_staged_revert",
    "list_checkpoints",
    "load_checkpoint",
    "restore_checkpoint",
    "review_checkpoint",
    "stage_staged_revert",
    "staged_revert_plan",
    "staged_revert_state",
    "widen_staged_revert",
]

_SCHEMA_VERSION = 1
_DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        "venv",
        ".venv",
        "logs",
        ".harness",
        "_checkpoints",
        "_undo",
    }
)
_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL"})

# --- AGT-09: the staged-undo store ------------------------------------------
#
# The three restore granularities are the vocabulary the product ALREADY has
# for the context-budget rewind (`harness.agent_kernel.context.rewind_run`:
# `files` / `conversation` / `both`). This module reuses those three names
# rather than inventing a fourth, and `tests/test_agt_09_staged_undo.py` pins
# the two sets equal so they cannot drift. `memory` may not import `harness`
# (the dependency direction is cli -> memory), so equality is enforced by a
# test instead of an import.
RESTORE_SCOPES: Tuple[str, ...] = ("files", "conversation", "both")

#: The middle granularity, and the DEFAULT, because "rewind the code, keep the
#: conversation" is what a user wants almost every time. The other two exist so
#: the choice is explicit rather than implied.
DEFAULT_RESTORE_SCOPE = "files"

#: Why a path is not in the store. Every one of these is REPORTED, never a
#: silent skip: a revert that quietly did not restore a file is a lie.
EXCLUSION_REASONS: Tuple[str, ...] = (
    "gitignored",
    "ignored_directory",
    "too_large",
    "out_of_scope",
    "symlink",
    "unreadable",
    "not_a_regular_file",
    "checkpoint_storage",
)

#: Snapshot lifecycle events, in the order a well-behaved turn produces them.
STAGED_EVENTS: Tuple[str, ...] = (
    "step_before",
    "step_after",
    "clean_completion",
    "manual",
)

_UNDO_DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
_UNDO_DEFAULT_MAX_SNAPSHOTS = 512
_UNDO_DEFAULT_MAX_TURNS = 64
_UNDO_JOURNAL = "journal.jsonl"
_UNDO_STAGED = "staged.json"
_UNDO_SCHEMA_VERSION = 1

#: The sentinel a manifest uses for "this path did not exist at capture time".
#: It has to be distinguishable from every real SHA-256, so it is not one.
_ABSENT = "\x00absent"


class CheckpointError(RuntimeError):
    """Base error for durable checkpoint operations."""


class CheckpointNotFoundError(CheckpointError):
    """Raised when a requested checkpoint does not exist."""


class CheckpointCorruptError(CheckpointError):
    """Raised when checkpoint metadata is not trustworthy."""


class CheckpointConflictError(CheckpointError):
    """Raised when strict restore detects a later workspace edit."""


class CheckpointResult(dict):
    """Dictionary-compatible checkpoint result."""


class RestoreResult(dict):
    """Dictionary-compatible restore result."""


def _safe_segment(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or text != str(value or "").strip():
        raise ValueError("invalid checkpoint identifier")
    if any(char in text for char in '/\\:*?"<>|\x00') or any(
        ord(char) < 32 for char in text
    ):
        raise ValueError("invalid checkpoint identifier")
    if text.rstrip(". ") in {"", ".", ".."}:
        raise ValueError("invalid checkpoint identifier")
    if text.split(".", 1)[0].upper() in _RESERVED_NAMES:
        raise ValueError("invalid checkpoint identifier")
    return text


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> Optional[str]:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None


def _atomic_bytes(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise CheckpointError("checkpoint paths must not be symbolic links")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        last_error: Optional[OSError] = None
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(0.03 * (attempt + 1))
        if last_error is not None:
            raise last_error
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _run_git(
    repo: Path, *args: str, timeout: float = 15.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            args=list(args), returncode=127, stdout="", stderr=str(exc)
        )


def _repo_key(repo: Path) -> str:
    return os.path.normcase(str(repo.resolve()))


def _workspace(repo: Path) -> Dict[str, str]:
    root = _run_git(repo, "rev-parse", "--show-toplevel")
    revision = _run_git(repo, "rev-parse", "HEAD")
    branch = _run_git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    return {
        "path": str(repo.resolve()),
        "key": _repo_key(repo),
        "name": repo.resolve().name,
        "git_root": root.stdout.strip() if root.returncode == 0 else "",
        "git_revision": revision.stdout.strip() if revision.returncode == 0 else "",
        "git_branch": branch.stdout.strip() if branch.returncode == 0 else "",
    }


def _relative_path(repo: Path, value: Any) -> str:
    raw = str(value or "").replace("\\", "/")
    path = Path(raw)
    if path.is_absolute() or not raw or raw in {".", ".."}:
        raise CheckpointError(f"invalid workspace path: {raw!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise CheckpointError(f"invalid workspace path: {raw!r}")
    candidate = (repo / path).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError as exc:
        raise CheckpointError("workspace path escapes the repository") from exc
    if any(part in _IGNORED_DIRS for part in path.parts):
        raise CheckpointError("checkpoint paths must not enter ignored directories")
    return path.as_posix()


def _iter_current_files(
    repo: Path,
    *,
    excluded_roots: Optional[Sequence[Path]] = None,
) -> List[str]:
    paths: List[str] = []
    excluded = []
    for item in excluded_roots or ():
        try:
            excluded.append(Path(item).resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    for current, directories, files in os.walk(repo, topdown=True, followlinks=False):
        current_path = Path(current)
        retained: List[str] = []
        for name in directories:
            candidate = current_path / name
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if name in _IGNORED_DIRS or candidate.is_symlink():
                continue
            if any(resolved == root or root in resolved.parents for root in excluded):
                continue
            retained.append(name)
        directories[:] = retained
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink():
                raise CheckpointError("workspace contains a symbolic-link file")
            try:
                relative = candidate.resolve().relative_to(repo.resolve()).as_posix()
            except ValueError as exc:
                raise CheckpointError("workspace file escaped its repository") from exc
            paths.append(relative)
    return sorted(set(paths))


def _current_manifest(
    repo: Path,
    selected: Optional[Sequence[str]] = None,
    *,
    excluded_roots: Optional[Sequence[Path]] = None,
) -> List[Dict[str, Any]]:
    excluded = [Path(item).resolve() for item in (excluded_roots or ())]
    if selected is not None:
        paths: List[str] = []
        for item in selected:
            relative = _relative_path(repo, item)
            candidate = (repo / Path(relative)).resolve()
            if any(candidate == root or root in candidate.parents for root in excluded):
                raise CheckpointError(
                    "checkpoint paths must not enter the checkpoint log root"
                )
            paths.append(relative)
    else:
        paths = _iter_current_files(repo, excluded_roots=excluded)
    manifest: List[Dict[str, Any]] = []
    for relative in sorted(set(paths)):
        candidate = repo / Path(relative)
        if candidate.is_symlink():
            raise CheckpointError("workspace contains a symbolic-link file")
        if not candidate.exists():
            manifest.append(
                {
                    "path": relative,
                    "exists": False,
                    "hash": None,
                    "size": 0,
                    "mode": None,
                }
            )
            continue
        if not candidate.is_file():
            raise CheckpointError(f"workspace path is not a regular file: {relative}")
        size = candidate.stat().st_size
        manifest.append(
            {
                "path": relative,
                "exists": True,
                "hash": _digest_file(candidate),
                "size": int(size),
                "mode": int(candidate.stat().st_mode & 0o7777),
            }
        )
    return manifest


def _safe_target(repo: Path, relative: str) -> Path:
    normalized = _relative_path(repo, relative)
    target = repo / Path(normalized)
    current = repo
    for part in Path(normalized).parts:
        current = current / part
        if current.is_symlink():
            raise CheckpointError("workspace path contains a symbolic-link component")
    return target


def _copy_snapshot_file(source: Path, target: Path, max_bytes: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise CheckpointError("snapshot paths must not be symbolic links")
    with source.open("rb") as source_handle, target.open("wb") as target_handle:
        copied = 0
        while True:
            chunk = source_handle.read(1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > max_bytes:
                raise CheckpointError(
                    f"checkpoint file exceeds the size limit: {source.name}"
                )
            target_handle.write(chunk)
        target_handle.flush()
        os.fsync(target_handle.fileno())


def _safe_json(value: Any) -> Any:
    from shared.security import redact_secrets

    return redact_secrets(value)


def _has_symlink_component(path: Path, stop: Optional[Path] = None) -> bool:
    """Return whether a path or one of its parents below ``stop`` is a symlink."""
    boundary = stop.resolve() if stop is not None else None
    current = path
    while True:
        try:
            if current.is_symlink():
                return True
            resolved = current.resolve()
            if boundary is not None and resolved == boundary:
                return False
        except (OSError, RuntimeError, ValueError):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _session_bytes(session: Any) -> Optional[bytes]:
    if session is None:
        return None
    if isinstance(session, Mapping):
        value = dict(session)
    else:
        serializer = getattr(session, "to_dict", None)
        if not callable(serializer):
            return None
        value = serializer()
    return json.dumps(
        _safe_json(value), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")


def _captures_all_files(record: Mapping[str, Any]) -> bool:
    """Return whether a checkpoint protects the complete workspace manifest."""
    return str(record.get("selection") or "all").casefold() != "selected"


class CheckpointManager:
    """Create, inspect, and restore durable workspace checkpoints."""

    def __init__(
        self,
        repo_path: Any,
        log_root: Any,
        session_id: str = "",
        *,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        raw_repo = Path(repo_path).expanduser()
        if raw_repo.is_symlink():
            raise CheckpointError("checkpoint repository must not be a symbolic link")
        self.repo_path = raw_repo.resolve()
        if not self.repo_path.is_dir():
            raise CheckpointError("checkpoint repository must be a real directory")
        raw_log_root = Path(log_root).expanduser()
        if raw_log_root.is_symlink():
            raise CheckpointError("checkpoint log root must not be a symbolic link")
        self.log_root = raw_log_root.resolve()
        self.session_id = _safe_segment(session_id) if session_id else "global"
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.root = self.log_root / "_checkpoints" / self.session_id
        if _has_symlink_component(self.root, self.log_root):
            raise CheckpointError("checkpoint root must not contain symbolic links")
        if self.log_root == self.repo_path:
            raise CheckpointError(
                "checkpoint log root must be separate from the repository"
            )

    def _excluded_roots(self) -> List[Path]:
        """Return workspace subtrees that contain checkpoint storage."""
        try:
            self.log_root.relative_to(self.repo_path)
        except ValueError:
            return []
        return [self.log_root]

    def _checkpoint_dir(self, checkpoint_id: str) -> Path:
        candidate = self.root / _safe_segment(checkpoint_id)
        if _has_symlink_component(candidate, self.log_root):
            raise CheckpointCorruptError("checkpoint storage contains a symbolic link")
        return candidate

    def _metadata_path(self, checkpoint_id: str) -> Path:
        return self._checkpoint_dir(checkpoint_id) / "checkpoint.json"

    def _capture_conversation(
        self,
        checkpoint_dir: Path,
        session: Any = None,
    ) -> Dict[str, Any]:
        if session is None and not self.session_id:
            return {}
        sid = self.session_id if self.session_id != "global" else ""
        target = None
        if sid:
            target = self.log_root / "_conversations" / f"{sid}.json"
        payload = _session_bytes(session)
        if (
            payload is None
            and target is not None
            and target.is_file()
            and not target.is_symlink()
        ):
            payload = target.read_bytes()
        if payload is None:
            return {}
        snapshot = checkpoint_dir / "conversation.json"
        _atomic_bytes(snapshot, payload)
        current_payload = b""
        if target is not None and target.is_file() and not target.is_symlink():
            current_payload = target.read_bytes()
        elif session is not None:
            current_payload = payload
        event_target = None
        event_payload = b""
        if sid:
            event_target = self.log_root / "_conversations" / f"{sid}.events.jsonl"
            if event_target.is_file() and not event_target.is_symlink():
                event_payload = event_target.read_bytes()
        if event_payload:
            _atomic_bytes(checkpoint_dir / "conversation.events.jsonl", event_payload)
        return {
            "session_id": sid,
            "snapshot": "conversation.json",
            "target": str(target) if target is not None else "",
            "expected_hash": _digest_bytes(current_payload)
            if current_payload
            else None,
            "snapshot_hash": _digest_bytes(payload),
            "event_target": str(event_target) if event_target is not None else "",
            "event_snapshot": "conversation.events.jsonl" if event_payload else "",
            "event_expected_hash": _digest_bytes(event_payload)
            if event_payload
            else None,
            "event_snapshot_hash": _digest_bytes(event_payload)
            if event_payload
            else None,
        }

    def _create_shadow_git(
        self, snapshot_dir: Path, checkpoint_dir: Path, mode: str
    ) -> Dict[str, Any]:
        shadow = checkpoint_dir / ("shadow" if mode == "shadow_git" else "worktree")
        if shadow.exists():
            raise CheckpointError("checkpoint storage already exists")
        try:
            if (
                mode == "worktree"
                and _run_git(self.repo_path, "rev-parse", "--git-dir").returncode == 0
            ):
                clone = _run_git(
                    self.repo_path,
                    "clone",
                    "--no-hardlinks",
                    "--no-local",
                    str(self.repo_path),
                    str(shadow),
                    timeout=30.0,
                )
            else:
                shutil.copytree(snapshot_dir, shadow)
                clone = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "available": False,
                "error": type(exc).__name__,
                "path": str(shadow),
            }
        if clone.returncode != 0:
            return {
                "available": False,
                "error": "git_clone_failed",
                "path": str(shadow),
            }
        if mode != "worktree":
            init = _run_git(shadow, "init", "--quiet", timeout=15.0)
            config_name = _run_git(
                shadow, "config", "user.name", "Neo Checkpoint", timeout=15.0
            )
            config_email = _run_git(
                shadow, "config", "user.email", "checkpoint@localhost", timeout=15.0
            )
            add = _run_git(shadow, "add", "-A", timeout=30.0)
            commit = _run_git(
                shadow,
                "-c",
                "user.name=Neo Checkpoint",
                "-c",
                "user.email=checkpoint@localhost",
                "commit",
                "--quiet",
                "-m",
                "neo checkpoint",
                timeout=30.0,
            )
            if (
                init.returncode != 0
                or config_name.returncode != 0
                or config_email.returncode != 0
                or add.returncode != 0
                or commit.returncode != 0
            ):
                return {
                    "available": False,
                    "error": "git_checkpoint_failed",
                    "path": str(shadow),
                }
        revision = _run_git(shadow, "rev-parse", "HEAD", timeout=15.0)
        return {
            "available": revision.returncode == 0,
            "path": str(shadow),
            "commit": revision.stdout.strip() if revision.returncode == 0 else "",
            "mode": mode,
        }

    def create_checkpoint(
        self,
        *,
        session: Any = None,
        files: Optional[Sequence[str]] = None,
        mode: str = "shadow_git",
        label: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> CheckpointResult:
        """Capture a checkpoint that can be reviewed and restored later."""
        selected_mode = str(mode or "shadow_git").strip().lower()
        if selected_mode not in {"shadow_git", "worktree"}:
            raise ValueError("checkpoint mode must be shadow_git or worktree")
        checkpoint_id = f"cp-{uuid.uuid4().hex[:16]}"
        checkpoint_dir = self._checkpoint_dir(checkpoint_id)
        checkpoint_dir.mkdir(parents=True, exist_ok=False)
        snapshot_dir = checkpoint_dir / "snapshot"
        snapshot_dir.mkdir()
        try:
            full_manifest = _current_manifest(
                self.repo_path,
                None,
                excluded_roots=self._excluded_roots(),
            )
            if files is None:
                selected_paths = {str(item["path"]) for item in full_manifest}
            else:
                selected_paths = {
                    _relative_path(self.repo_path, item) for item in files
                }
            manifest: List[Dict[str, Any]] = []
            seen_paths = set()
            for item in full_manifest:
                copied = dict(item)
                copied["captured"] = str(item["path"]) in selected_paths
                manifest.append(copied)
                seen_paths.add(str(item["path"]))
            for selected in sorted(selected_paths - seen_paths):
                manifest.append(
                    {
                        "path": selected,
                        "exists": False,
                        "hash": None,
                        "size": 0,
                        "mode": None,
                        "captured": True,
                    }
                )
            for item in manifest:
                if not item.get("captured") or not item["exists"]:
                    continue
                source = self.repo_path / Path(item["path"])
                destination = snapshot_dir / Path(item["path"])
                _copy_snapshot_file(source, destination, self.max_file_bytes)
            conversation = self._capture_conversation(checkpoint_dir, session)
            git_info = self._create_shadow_git(
                snapshot_dir, checkpoint_dir, selected_mode
            )
            record: Dict[str, Any] = {
                "schema_version": _SCHEMA_VERSION,
                "checkpoint_id": checkpoint_id,
                "label": str(label or "")[:256],
                "created_at": time.time(),
                "mode": selected_mode,
                "repo": _workspace(self.repo_path),
                "files": manifest,
                "selection": "all" if files is None else "selected",
                "captured_paths": sorted(
                    str(item["path"]) for item in manifest if item.get("captured")
                ),
                "conversation": conversation,
                "git": git_info,
                "metadata": _safe_json(dict(metadata or {})),
            }
            _atomic_bytes(
                self._metadata_path(checkpoint_id),
                json.dumps(
                    record, ensure_ascii=False, indent=2, sort_keys=True, default=str
                ).encode("utf-8"),
            )
            return CheckpointResult(record)
        except Exception:
            shutil.rmtree(checkpoint_dir, ignore_errors=True)
            raise

    def checkpoint_before_mutation(self, **kwargs: Any) -> CheckpointResult:
        """Create a checkpoint immediately before a caller-authorized mutation."""
        return self.create_checkpoint(**kwargs)

    def list_checkpoints(self) -> List[CheckpointResult]:
        """List checkpoint metadata ordered newest first."""
        if not self.root.is_dir():
            return []
        result: List[CheckpointResult] = []
        for path in sorted(
            self.root.glob("*/checkpoint.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(raw, dict)
                    and int(raw.get("schema_version", 0)) == _SCHEMA_VERSION
                ):
                    result.append(CheckpointResult(raw))
            except (OSError, UnicodeError, ValueError, TypeError):
                continue
        return result

    def load_checkpoint(self, checkpoint_id: str) -> CheckpointResult:
        """Load one validated checkpoint record."""
        path = self._metadata_path(checkpoint_id)
        if not path.is_file() or path.is_symlink():
            raise CheckpointNotFoundError(f"checkpoint {checkpoint_id!r} was not found")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise CheckpointCorruptError("checkpoint metadata is corrupt") from exc
        if (
            not isinstance(raw, dict)
            or int(raw.get("schema_version", 0)) != _SCHEMA_VERSION
        ):
            raise CheckpointCorruptError("checkpoint metadata schema is unsupported")
        if str(raw.get("checkpoint_id") or "") != str(checkpoint_id):
            raise CheckpointCorruptError("checkpoint identity does not match its path")
        if not isinstance(raw.get("repo"), Mapping) or not str(
            raw["repo"].get("key") or ""
        ):
            raise CheckpointCorruptError("checkpoint workspace identity is missing")
        if not isinstance(raw.get("files"), list):
            raise CheckpointCorruptError("checkpoint file manifest is missing")
        return CheckpointResult(raw)

    def latest_checkpoint(self) -> Optional[CheckpointResult]:
        """Return the newest valid checkpoint, if one exists."""
        records = self.list_checkpoints()
        return records[0] if records else None

    def _current_state(self, record: Mapping[str, Any]) -> Dict[str, Optional[str]]:
        state: Dict[str, Optional[str]] = {}
        for item in record.get("files", []) or []:
            if not isinstance(item, dict):
                continue
            relative = str(item.get("path") or "")
            try:
                target = _safe_target(self.repo_path, relative)
            except CheckpointError:
                state[relative] = None
                continue
            state[relative] = _digest_file(target)
        return state

    def review_checkpoint(self, checkpoint_id: str) -> CheckpointResult:
        """Return file-by-file drift and a unified diff against a checkpoint."""
        record = self.load_checkpoint(checkpoint_id)
        current = self._current_state(record)
        files: List[Dict[str, Any]] = []
        diff_parts: List[str] = []
        for item in record.get("files", []) or []:
            if not isinstance(item, dict) or not item.get("captured", True):
                continue
            relative = str(item.get("path") or "")
            expected = item.get("hash")
            actual = current.get(relative)
            if item.get("exists"):
                status = "unchanged" if actual == expected else "modified"
            else:
                status = "unchanged" if actual is None else "added"
            entry: Dict[str, Any] = {
                "path": relative,
                "status": status,
                "expected_hash": expected,
                "actual_hash": actual,
            }
            try:
                snapshot = self._snapshot_path(record, relative)
                target = _safe_target(self.repo_path, relative)
            except CheckpointError as exc:
                raise CheckpointCorruptError(
                    f"checkpoint manifest path is unsafe: {relative!r}"
                ) from exc
            if status in {"modified", "added"}:
                try:
                    before = (
                        snapshot.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines(keepends=True)
                        if snapshot.is_file()
                        else []
                    )
                    after = (
                        target.read_text(encoding="utf-8", errors="replace").splitlines(
                            keepends=True
                        )
                        if target.is_file()
                        else []
                    )
                    diff = list(
                        difflib.unified_diff(
                            before,
                            after,
                            fromfile=f"checkpoint/{relative}",
                            tofile=f"workspace/{relative}",
                        )
                    )
                    entry["diff"] = "".join(diff)[:12000]
                    diff_parts.extend(diff[:200])
                except (OSError, UnicodeError, ValueError):
                    entry["diff"] = "[binary or unreadable file]"
            else:
                entry["diff"] = ""
            files.append(entry)
        current_paths = set(
            _iter_current_files(
                self.repo_path,
                excluded_roots=self._excluded_roots(),
            )
        )
        known_paths = {
            str(item.get("path") or "")
            for item in record.get("files", []) or []
            if isinstance(item, dict) and item.get("captured", True)
        }
        if _captures_all_files(record):
            for relative in sorted(current_paths - known_paths):
                files.append(
                    {
                        "path": relative,
                        "status": "added",
                        "expected_hash": None,
                        "actual_hash": _digest_file(self.repo_path / Path(relative)),
                        "diff": "[new workspace file]",
                    }
                )
        return CheckpointResult(
            {
                "checkpoint_id": checkpoint_id,
                "changed": any(item.get("status") != "unchanged" for item in files),
                "files": files,
                "diff": "".join(diff_parts)[:50000],
                "review": "safe"
                if not any(item.get("status") != "unchanged" for item in files)
                else "conflict",
            }
        )

    def _snapshot_path(
        self,
        record: Mapping[str, Any],
        relative: str,
        *,
        conversation: bool = False,
    ) -> Path:
        checkpoint_dir = self._checkpoint_dir(str(record["checkpoint_id"]))
        base = checkpoint_dir if conversation else checkpoint_dir / "snapshot"
        candidate = base / str(relative or "")
        try:
            resolved_base = base.resolve()
            resolved = candidate.resolve()
            resolved.relative_to(resolved_base)
        except ValueError as exc:
            raise CheckpointCorruptError(
                "checkpoint snapshot path escapes its root"
            ) from exc
        if candidate.is_symlink() or resolved.is_symlink():
            raise CheckpointCorruptError(
                "checkpoint snapshot must not be a symbolic link"
            )
        return resolved

    def _restore_files(self, record: Mapping[str, Any], force: bool) -> RestoreResult:
        conflicts: List[Dict[str, Any]] = []
        for item in record.get("files", []) or []:
            if (
                not isinstance(item, dict)
                or not item.get("captured", True)
                or not item.get("exists")
            ):
                continue
            relative = str(item.get("path") or "")
            try:
                source = self._snapshot_path(record, relative)
            except CheckpointError as exc:
                conflicts.append({"path": relative, "reason": str(exc)})
                continue
            if not source.is_file() or _digest_file(source) != item.get("hash"):
                conflicts.append(
                    {
                        "path": relative,
                        "reason": "checkpoint snapshot failed its integrity check",
                    }
                )
        current_paths = set(
            _iter_current_files(
                self.repo_path,
                excluded_roots=self._excluded_roots(),
            )
        )
        known_paths = {
            str(item.get("path") or "")
            for item in record.get("files", []) or []
            if isinstance(item, dict) and item.get("captured", True)
        }
        if not force and _captures_all_files(record):
            for relative in sorted(current_paths - known_paths):
                conflicts.append({"path": relative, "reason": "new workspace file"})
        for item in record.get("files", []) or []:
            if not isinstance(item, dict) or not item.get("captured", True):
                continue
            relative = str(item.get("path") or "")
            target = _safe_target(self.repo_path, relative)
            actual = _digest_file(target)
            if actual != item.get("hash"):
                conflicts.append(
                    {
                        "path": relative,
                        "reason": "workspace changed after checkpoint",
                        "expected_hash": item.get("hash"),
                        "actual_hash": actual,
                    }
                )
        if conflicts:
            return RestoreResult(
                {
                    "ok": False,
                    "status": "conflict",
                    "conflicts": conflicts,
                    "restored_files": [],
                }
            )
        restored: List[str] = []
        for item in record.get("files", []) or []:
            if not isinstance(item, dict):
                continue
            relative = str(item.get("path") or "")
            if not item.get("captured", True):
                continue
            target = _safe_target(self.repo_path, relative)
            source = self._snapshot_path(record, relative)
            if item.get("exists"):
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_bytes(target, source.read_bytes())
                try:
                    os.chmod(target, int(item.get("mode") or 0o644))
                except OSError:
                    pass
                restored.append(relative)
            elif target.exists():
                target.unlink()
                restored.append(relative)
        return RestoreResult(
            {
                "ok": True,
                "status": "restored",
                "conflicts": [],
                "restored_files": restored,
            }
        )

    def _conversation_target(self, value: Any) -> Path:
        raw_root = self.log_root / "_conversations"
        if _has_symlink_component(raw_root, self.log_root):
            raise CheckpointError(
                "conversation checkpoint root must not contain symbolic links"
            )
        root = raw_root.resolve()
        candidate = Path(str(value or "")).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise CheckpointError(
                "conversation checkpoint path escapes its root"
            ) from exc
        if candidate.is_symlink() or resolved.is_symlink():
            raise CheckpointError(
                "conversation checkpoint path must not be a symbolic link"
            )
        return resolved

    def _conversation_conflicts(
        self,
        record: Mapping[str, Any],
        force: bool,
    ) -> List[Dict[str, Any]]:
        conversation = record.get("conversation")
        if not isinstance(conversation, dict) or not conversation.get("target"):
            return []
        conflicts: List[Dict[str, Any]] = []
        target_value = str(conversation.get("target") or "")
        try:
            target = self._conversation_target(target_value)
        except CheckpointError as exc:
            return [{"path": target_value, "reason": str(exc)}]
        expected = conversation.get("expected_hash")
        actual = _digest_file(target)
        if not force and actual != expected:
            conflicts.append(
                {
                    "path": target_value,
                    "reason": "conversation changed after checkpoint",
                    "expected_hash": expected,
                    "actual_hash": actual,
                }
            )
        snapshot_name = str(conversation.get("snapshot") or "")
        if snapshot_name:
            try:
                snapshot = self._snapshot_path(record, snapshot_name, conversation=True)
            except CheckpointError as exc:
                conflicts.append({"path": snapshot_name, "reason": str(exc)})
            else:
                if not snapshot.is_file() or _digest_file(snapshot) != conversation.get(
                    "snapshot_hash"
                ):
                    conflicts.append(
                        {
                            "path": snapshot_name,
                            "reason": "conversation checkpoint snapshot failed its integrity check",
                        }
                    )
        event_target_value = str(conversation.get("event_target") or "")
        if event_target_value:
            try:
                event_target = self._conversation_target(event_target_value)
            except CheckpointError as exc:
                conflicts.append({"path": event_target_value, "reason": str(exc)})
            else:
                event_actual = _digest_file(event_target)
                event_expected = conversation.get("event_expected_hash")
                if not force and event_actual != event_expected:
                    conflicts.append(
                        {
                            "path": event_target_value,
                            "reason": "conversation event journal changed after checkpoint",
                            "expected_hash": event_expected,
                            "actual_hash": event_actual,
                        }
                    )
                event_snapshot_name = str(conversation.get("event_snapshot") or "")
                if event_snapshot_name:
                    try:
                        event_snapshot = self._snapshot_path(
                            record, event_snapshot_name, conversation=True
                        )
                    except CheckpointError as exc:
                        conflicts.append(
                            {"path": event_snapshot_name, "reason": str(exc)}
                        )
                    else:
                        if not event_snapshot.is_file() or _digest_file(
                            event_snapshot
                        ) != conversation.get("event_snapshot_hash"):
                            conflicts.append(
                                {
                                    "path": event_snapshot_name,
                                    "reason": "conversation event snapshot failed its integrity check",
                                }
                            )
        return conflicts

    def _restore_conversation(
        self, record: Mapping[str, Any], force: bool
    ) -> RestoreResult:
        conversation = record.get("conversation")
        if not isinstance(conversation, dict) or not conversation.get("target"):
            return RestoreResult(
                {"ok": True, "status": "not_requested", "conflicts": []}
            )
        conflicts = self._conversation_conflicts(record, force)
        if conflicts:
            return RestoreResult(
                {"ok": False, "status": "conflict", "conflicts": conflicts}
            )
        target = self._conversation_target(str(conversation.get("target") or ""))
        snapshot_name = str(conversation.get("snapshot") or "conversation.json")
        try:
            snapshot = self._snapshot_path(record, snapshot_name, conversation=True)
        except CheckpointError as exc:
            return RestoreResult(
                {"ok": False, "status": "conflict", "conflicts": [{"reason": str(exc)}]}
            )
        if not snapshot.is_file():
            return RestoreResult(
                {
                    "ok": False,
                    "status": "conflict",
                    "conflicts": [
                        {
                            "path": snapshot_name,
                            "reason": "conversation checkpoint snapshot is missing",
                        }
                    ],
                }
            )
        _atomic_bytes(target, snapshot.read_bytes())
        event_target_value = str(conversation.get("event_target") or "")
        event_snapshot_name = str(conversation.get("event_snapshot") or "")
        if event_target_value and event_snapshot_name:
            event_target = self._conversation_target(event_target_value)
            event_snapshot = self._snapshot_path(
                record, event_snapshot_name, conversation=True
            )
            _atomic_bytes(event_target, event_snapshot.read_bytes())
        return RestoreResult(
            {"ok": True, "status": "restored", "conflicts": [], "path": str(target)}
        )

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        *,
        restore_files: bool = True,
        restore_conversation: bool = False,
        force: bool = False,
        strict: bool = False,
    ) -> RestoreResult:
        """Restore files and/or a conversation only when no later edits conflict."""
        record = self.load_checkpoint(checkpoint_id)
        record_repo_key = str(record.get("repo", {}).get("key") or "")
        if not record_repo_key or _repo_key(self.repo_path) != record_repo_key:
            result = RestoreResult(
                {
                    "ok": False,
                    "status": "conflict",
                    "conflicts": [
                        {
                            "path": str(self.repo_path),
                            "reason": "workspace identity mismatch",
                        }
                    ],
                }
            )
            if strict:
                raise CheckpointConflictError(
                    "workspace identity does not match checkpoint"
                )
            return result
        conversation_preflight = (
            self._conversation_conflicts(record, force) if restore_conversation else []
        )
        if conversation_preflight:
            result = RestoreResult(
                {
                    "ok": False,
                    "status": "conflict",
                    "conflicts": conversation_preflight,
                }
            )
            if strict:
                raise CheckpointConflictError("conversation has edits after checkpoint")
            return result
        if restore_files:
            result = self._restore_files(record, force)
            if not result.get("ok"):
                if strict:
                    raise CheckpointConflictError(
                        "workspace has edits after checkpoint"
                    )
                return result
        else:
            result = RestoreResult(
                {
                    "ok": True,
                    "status": "files_not_requested",
                    "conflicts": [],
                    "restored_files": [],
                }
            )
        conversation_result = (
            self._restore_conversation(record, force)
            if restore_conversation
            else RestoreResult({"ok": True, "status": "not_requested", "conflicts": []})
        )
        if not conversation_result.get("ok"):
            if strict:
                raise CheckpointConflictError("conversation has edits after checkpoint")
            return conversation_result
        result["conversation"] = conversation_result
        result["checkpoint_id"] = checkpoint_id
        result["ok"] = True
        result["status"] = "restored"
        _atomic_bytes(
            self._checkpoint_dir(checkpoint_id) / "restore.json",
            json.dumps(result, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            ),
        )
        return result

    def diff_checkpoint(self, checkpoint_id: str) -> str:
        """Return the bounded unified diff for a checkpoint review."""
        return str(self.review_checkpoint(checkpoint_id).get("diff") or "")


# ---------------------------------------------------------------------------
# AGT-09: staged snapshots in a private object store
# ---------------------------------------------------------------------------


def _staged_segment(value: Any, *, fallback: str = "turn") -> str:
    """Return a filesystem-safe segment for a caller-supplied turn id."""
    try:
        return _safe_segment(value)
    except ValueError:
        digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
        return f"{fallback}-{digest}"


def _git_ignored(repo: Path, relatives: Sequence[str]) -> Tuple[set, str]:
    """Return the gitignored subset of ``relatives`` and which authority answered.

    ``git check-ignore`` is the authority because it is the one that knows the
    repository's own rules. It is asked ONCE for the whole batch, because a
    subprocess per file is a per-file subprocess. When git is unavailable the
    built-in directory set answers instead, and the receipt says so - a caller
    must be able to tell "nothing was ignored" from "we could not ask".
    """
    if not relatives:
        return set(), "git"
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--stdin", "-z"],
            input="\0".join(relatives) + "\0",
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {rel for rel in relatives if _builtin_ignored(rel)}, "builtin"
    if completed.returncode not in (0, 1):
        return {rel for rel in relatives if _builtin_ignored(rel)}, "builtin"
    answered = {line for line in (completed.stdout or "").split("\0") if line}
    return {rel for rel in relatives if rel in answered}, "git"


def _builtin_ignored(relative: str) -> bool:
    """Return whether a repository-relative path is in a built-in ignored set."""
    return any(
        part in _IGNORED_DIRS for part in str(relative).replace("\\", "/").split("/")
    )


class StagedSnapshotStore:
    """Content-addressed pre-images with a staged, widening revert.

    Three properties distinguish this from the checkpoint stack above, and all
    three are the reason this exists rather than a second call into
    ``CheckpointManager``:

    * **Scriptable.** Every operation is a named method over a private
      directory plus an append-only journal. There is no hidden in-memory
      state, so a script, a second terminal, and a resumed process see the same
      staged range.
    * **Idempotent.** A snapshot's id is a digest of its CONTENT, so capturing
      an unchanged state twice writes the same object and yields the same id.
      Re-running a capture cannot grow the store.
    * **Ranged.** Staging takes a RANGE of turns and a repeated stage call
      WIDENS it. Nothing is ever popped, so a user who presses undo three times
      gets one coherent revert of three turns rather than three partial reverts
      racing each other.

    Nothing here creates a commit, moves a branch, or touches the user's index
    or ``HEAD``. The only bytes written inside the repository are the file
    content a committed revert is asked to put back, and every one of those
    writes is hash-verified in both directions.
    """

    def __init__(
        self,
        repo_path: Any,
        log_root: Any,
        session_id: str = "",
        *,
        max_file_bytes: int = _UNDO_DEFAULT_MAX_FILE_BYTES,
        max_snapshots: int = _UNDO_DEFAULT_MAX_SNAPSHOTS,
        max_turns: int = _UNDO_DEFAULT_MAX_TURNS,
        trace: Any = None,
    ) -> None:
        raw_repo = Path(repo_path).expanduser()
        if raw_repo.is_symlink():
            raise CheckpointError("staged undo repository must not be a symbolic link")
        self.repo_path = raw_repo.resolve()
        if not self.repo_path.is_dir():
            raise CheckpointError("staged undo repository must be a real directory")
        raw_log_root = Path(log_root).expanduser()
        if raw_log_root.is_symlink():
            raise CheckpointError("staged undo log root must not be a symbolic link")
        self.log_root = raw_log_root.resolve()
        if self.log_root == self.repo_path:
            raise CheckpointError(
                "staged undo log root must be separate from the repository"
            )
        self.session_id = (
            _staged_segment(session_id, fallback="session") if session_id else "global"
        )
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_snapshots = max(1, int(max_snapshots))
        self.max_turns = max(1, int(max_turns))
        self.trace = trace
        self.root = self.log_root / "_undo" / self.session_id
        if _has_symlink_component(self.root, self.log_root):
            raise CheckpointError("staged undo root must not contain symbolic links")
        self.objects = self.root / "objects"
        self.snapshots = self.root / "snapshots"
        self.receipts = self.root / "receipts"
        self.journal_path = self.root / _UNDO_JOURNAL
        self.staged_path = self.root / _UNDO_STAGED

    # -- storage -------------------------------------------------------

    def _object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def _write_object(self, payload: bytes) -> str:
        digest = _digest_bytes(payload)
        target = self._object_path(digest)
        if target.is_file() and target.stat().st_size == len(payload):
            return digest
        _atomic_bytes(target, payload)
        return digest

    def _read_object(self, digest: str) -> Optional[bytes]:
        if not digest:
            return None
        try:
            if _has_symlink_component(self._object_path(digest), self.root):
                return None
            payload = self._object_path(digest).read_bytes()
        except (OSError, RuntimeError, ValueError):
            return None
        return payload if _digest_bytes(payload) == digest else None

    # -- journal -------------------------------------------------------

    def _append(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Append one journal row durably and return it."""
        record = dict(row)
        record.setdefault("session_id", self.session_id)
        record.setdefault("schema_version", _UNDO_SCHEMA_VERSION)
        record["repo_key"] = _repo_key(self.repo_path)
        record["ts"] = time.time()
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            _safe_json(record), ensure_ascii=False, sort_keys=True, default=str
        )
        try:
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError) as exc:
            self._note(
                "undo_journal_write_failed", {"error": f"{type(exc).__name__}: {exc}"}
            )
        return record

    def _rows(self, kind: str = "") -> List[Dict[str, Any]]:
        """Read the journal, tolerating a torn tail and a wrong-repo file."""
        if not self.journal_path.is_file() or self.journal_path.is_symlink():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            text = self.journal_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            if str(value.get("repo_key") or "") != _repo_key(self.repo_path):
                continue
            if kind and str(value.get("kind") or "") != kind:
                continue
            rows.append(value)
        return rows

    def _note(self, kind: str, data: Mapping[str, Any]) -> None:
        """Send a best-effort event to a caller-supplied trace sink."""
        sink = self.trace
        if sink is None:
            return
        try:
            if callable(sink):
                sink(kind, dict(data))
            elif hasattr(sink, "event"):
                sink.event(kind, dict(data))
        except Exception:
            return

    # -- capture -------------------------------------------------------

    def _classify(
        self,
        relatives: Sequence[str],
        *,
        scope_prefixes: Optional[Sequence[str]],
    ) -> Tuple[List[str], List[Dict[str, str]]]:
        """Split candidate paths into capturable and excluded, with reasons."""
        raw: List[str] = []
        excluded: List[Dict[str, str]] = []
        for value in relatives:
            text = str(value or "").replace("\\", "/")
            path = Path(text)
            if (
                not text
                or text in {".", ".."}
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                excluded.append({"path": text, "reason": "out_of_scope"})
                continue
            normalized = path.as_posix()
            if any(part in _IGNORED_DIRS for part in path.parts):
                excluded.append(
                    {
                        "path": normalized,
                        "reason": "checkpoint_storage"
                        if "_undo" in path.parts or "_checkpoints" in path.parts
                        else "ignored_directory",
                    }
                )
                continue
            try:
                resolved = (self.repo_path / path).resolve()
                resolved.relative_to(self.repo_path)
            except (OSError, RuntimeError, ValueError):
                excluded.append({"path": normalized, "reason": "out_of_scope"})
                continue
            raw.append(normalized)
        accepted = sorted(set(raw))
        ignored, authority = _git_ignored(self.repo_path, accepted)
        self._ignore_authority = authority
        prefixes = tuple(
            str(item).replace("\\", "/").strip("/") for item in (scope_prefixes or ())
        )
        final: List[str] = []
        for relative in accepted:
            if relative in ignored:
                excluded.append({"path": relative, "reason": "gitignored"})
                continue
            if prefixes and not any(
                relative == prefix or relative.startswith(prefix + "/")
                for prefix in prefixes
                if prefix
            ):
                # The scope filter runs BEFORE the existence check, so a path
                # that does not exist YET is filtered too. Filtering only the
                # existing files would let an out-of-scope CREATE through.
                excluded.append({"path": relative, "reason": "out_of_scope"})
                continue
            candidate = self.repo_path / Path(relative)
            if candidate.is_symlink():
                excluded.append({"path": relative, "reason": "symlink"})
                continue
            if not candidate.exists():
                # A path the caller named that does not exist yet is a
                # CREATE. It is capturable, and its pre-image is "absent".
                final.append(relative)
                continue
            if not candidate.is_file():
                excluded.append({"path": relative, "reason": "not_a_regular_file"})
                continue
            try:
                if candidate.stat().st_size > self.max_file_bytes:
                    excluded.append({"path": relative, "reason": "too_large"})
                    continue
            except OSError:
                excluded.append({"path": relative, "reason": "unreadable"})
                continue
            final.append(relative)
        excluded.sort(key=lambda item: (item["reason"], item["path"]))
        return final, excluded

    def _capture_conversation(self) -> Dict[str, Any]:
        """Hash the live conversation artifacts without copying them.

        The staged store does not own the conversation; it only needs to know
        which bytes it would have to put back, and must refuse if the user has
        changed them since. That keeps the two axes (``files`` and
        ``conversation``) genuinely independent, which is the whole point of
        having three granularities rather than one.
        """
        if not self.session_id or self.session_id == "global":
            return {}
        captured: Dict[str, Any] = {}
        for name in (f"{self.session_id}.json", f"{self.session_id}.events.jsonl"):
            target = self.log_root / "_conversations" / name
            try:
                if target.is_symlink() or not target.is_file():
                    continue
                payload = target.read_bytes()
            except (OSError, RuntimeError, ValueError):
                continue
            captured[name] = {
                "hash": _digest_bytes(payload),
                "size": len(payload),
                "object": self._write_object(payload),
            }
        return captured

    def capture(
        self,
        *,
        turn_id: str,
        event: str = "step",
        label: str = "",
        paths: Optional[Sequence[str]] = None,
        scope_prefixes: Optional[Sequence[str]] = None,
        conversation: bool = True,
        changed_paths: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Capture a staged pre-image. NEVER raises; a failure is a record.

        The best-effort contract is the point: bookkeeping must never be the
        reason a user's work is lost or a step is refused. A capture that fails
        returns ``status="failed"`` with the error, notes it on the trace, and
        the caller continues.

        ``paths=None`` captures the whole workspace (the step-level seam);
        passing the paths a mutation is about to touch captures just those,
        which is what makes the per-mutation hook affordable on a large tree.
        """
        started = time.time()
        turn = _staged_segment(turn_id)
        selected_event = str(event or "manual")
        if selected_event not in STAGED_EVENTS:
            selected_event = "manual"
        try:
            if paths is None:
                candidates = [
                    str(item["path"])
                    for item in _current_manifest(
                        self.repo_path, None, excluded_roots=self._excluded_roots()
                    )
                    if item.get("exists")
                ]
            else:
                candidates = [str(item) for item in paths]
            accepted, excluded = self._classify(
                candidates, scope_prefixes=scope_prefixes
            )
            files: Dict[str, Any] = {}
            for relative in accepted:
                candidate = self.repo_path / Path(relative)
                if not candidate.exists():
                    files[relative] = {
                        "exists": False,
                        "hash": None,
                        "size": 0,
                        "mode": None,
                    }
                    continue
                try:
                    payload = candidate.read_bytes()
                except (OSError, RuntimeError, ValueError) as exc:
                    excluded.append(
                        {
                            "path": relative,
                            "reason": f"unreadable: {type(exc).__name__}",
                        }
                    )
                    continue
                files[relative] = {
                    "exists": True,
                    "hash": _digest_bytes(payload),
                    "size": len(payload),
                    "mode": int(candidate.stat().st_mode & 0o7777),
                    "object": self._write_object(payload),
                }
            conversation_record = self._capture_conversation() if conversation else {}
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "turn_id": turn,
                        "event": selected_event,
                        "files": files,
                        "conversation": conversation_record,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            snapshot_id = f"snap-{digest[:16]}"
            snapshot_file = self.snapshots / f"{snapshot_id}.json"
            # Idempotency is a PROPERTY, not an aspiration: the id is a
            # content digest, so re-capturing an unchanged state must not add a
            # second journal row either. Only the FIRST write of a snapshot is
            # journalled.
            already_present = snapshot_file.is_file()
            record: Dict[str, Any] = {
                "schema_version": _UNDO_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "turn_id": turn,
                "event": selected_event,
                "label": str(label or "")[:256],
                "created_at": started,
                "status": "captured",
                "selection": "all" if paths is None else "selected",
                "repo_key": _repo_key(self.repo_path),
                "files": files,
                "excluded": excluded,
                "conversation": conversation_record,
                "changed_paths": sorted(
                    {str(item).replace("\\", "/") for item in (changed_paths or [])}
                ),
                "ignore_authority": getattr(self, "_ignore_authority", "git"),
                "error": "",
            }
            _atomic_bytes(
                snapshot_file,
                json.dumps(
                    record, ensure_ascii=False, indent=2, sort_keys=True, default=str
                ).encode("utf-8"),
            )
            if not already_present:
                self._append(
                    {
                        "kind": "snapshot",
                        "snapshot_id": snapshot_id,
                        "turn_id": turn,
                        "event": selected_event,
                        "file_count": len(files),
                        "excluded_count": len(excluded),
                        "selection": record["selection"],
                    }
                )
            self._prune(turn)
            self._note(
                "undo_snapshot_captured",
                {"turn_id": turn, "event": selected_event, "files": len(files)},
            )
            return record
        except Exception as exc:  # best-effort by contract
            failure = {
                "schema_version": _UNDO_SCHEMA_VERSION,
                "snapshot_id": "",
                "turn_id": turn,
                "event": selected_event,
                "label": str(label or "")[:256],
                "created_at": started,
                "status": "failed",
                "selection": "selected" if paths is not None else "all",
                "repo_key": _repo_key(self.repo_path),
                "files": {},
                "excluded": [],
                "conversation": {},
                "changed_paths": [],
                "ignore_authority": "none",
                "error": f"{type(exc).__name__}: {exc}",
            }
            self._append(
                {"kind": "snapshot_failed", "turn_id": turn, "error": failure["error"]}
            )
            self._note(
                "undo_snapshot_failed", {"turn_id": turn, "error": failure["error"]}
            )
            return failure

    def _prune(self, current_turn: str) -> None:
        """Bound the store by turns, keeping the newest and this turn's first."""
        turn_ids: List[str] = []
        for row in self._rows("snapshot"):
            turn = str(row.get("turn_id") or "")
            if turn and turn not in turn_ids:
                turn_ids.append(turn)
        if len(turn_ids) <= self.max_turns:
            return
        keep = set(turn_ids[-(self.max_turns - 1) :]) | {current_turn}
        stale = [turn for turn in turn_ids if turn not in keep]
        for turn in stale:
            self._append({"kind": "turn_pruned", "turn_id": turn})

    def _excluded_roots(self) -> List[Path]:
        try:
            self.log_root.relative_to(self.repo_path)
        except ValueError:
            return []
        return [self.log_root]

    # -- reads ---------------------------------------------------------

    def load_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        """Load one snapshot record, or raise ``CheckpointNotFoundError``."""
        segment = _staged_segment(snapshot_id, fallback="snap")
        if not str(snapshot_id or "").startswith("snap-"):
            raise CheckpointNotFoundError(f"snapshot {snapshot_id!r} was not found")
        path = self.snapshots / f"{segment}.json"
        if not path.is_file() or path.is_symlink():
            raise CheckpointNotFoundError(f"snapshot {snapshot_id!r} was not found")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise CheckpointCorruptError("staged snapshot metadata is corrupt") from exc
        if (
            not isinstance(value, dict)
            or int(value.get("schema_version", 0)) != _UNDO_SCHEMA_VERSION
        ):
            raise CheckpointCorruptError("staged snapshot schema is unsupported")
        return value

    def turns(self, limit: int = 0) -> List[Dict[str, Any]]:
        """Return captured turns newest first, with their snapshot ids."""
        order: List[str] = []
        rows: Dict[str, Dict[str, Any]] = {}
        for row in self._rows("snapshot"):
            turn = str(row.get("turn_id") or "")
            if not turn:
                continue
            if turn not in order:
                order.append(turn)
            rows.setdefault(turn, {"turn_id": turn, "snapshots": [], "file_count": 0})
            snapshot_id = str(row.get("snapshot_id") or "")
            if snapshot_id and snapshot_id not in rows[turn]["snapshots"]:
                rows[turn]["snapshots"].append(snapshot_id)
            rows[turn]["file_count"] = max(
                int(row.get("file_count") or 0), int(rows[turn]["file_count"])
            )
            rows[turn]["event"] = str(row.get("event") or "")
            rows[turn]["created_at"] = float(row.get("ts") or 0.0)
        result = [rows[turn] for turn in reversed(order)]
        if limit and limit > 0:
            result = result[:limit]
        return result

    # -- staging -------------------------------------------------------

    def _read_staged(self) -> Dict[str, Any]:
        if not self.staged_path.is_file() or self.staged_path.is_symlink():
            return {}
        try:
            value = json.loads(self.staged_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        if str(value.get("repo_key") or "") != _repo_key(self.repo_path):
            return {}
        return value

    def _write_staged(self, value: Mapping[str, Any]) -> None:
        _atomic_bytes(
            self.staged_path,
            json.dumps(
                dict(value), ensure_ascii=False, indent=2, sort_keys=True, default=str
            ).encode("utf-8"),
        )

    def staged(self) -> Dict[str, Any]:
        """Return the currently staged revert range, or an empty mapping."""
        staged = self._read_staged()
        if not staged:
            return {}
        scope = str(staged.get("scope") or DEFAULT_RESTORE_SCOPE)
        if scope not in RESTORE_SCOPES:
            scope = DEFAULT_RESTORE_SCOPE
        return {
            "staged": True,
            "turn_ids": [str(item) for item in (staged.get("turn_ids") or [])],
            "snapshot_ids": [str(item) for item in (staged.get("snapshot_ids") or [])],
            "scope": scope,
            "staged_at": float(staged.get("staged_at") or 0.0),
            "widened": int(staged.get("widened") or 0),
            "paths": [str(item) for item in (staged.get("paths") or [])],
        }

    def stage(
        self,
        *,
        count: int = 1,
        turn_ids: Optional[Sequence[str]] = None,
        scope: str = DEFAULT_RESTORE_SCOPE,
        snapshot_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Stage a revert, WIDENING an already-staged range rather than popping.

        ``count`` selects the newest N captured turns. Naming ``turn_ids`` or
        ``snapshot_ids`` selects a range explicitly. Whatever was staged before
        is kept and unioned in, which is the difference from a checkpoint stack
        and the reason a second ``/undo`` is a wider undo rather than a
        different undo.
        """
        selected_scope = str(scope or DEFAULT_RESTORE_SCOPE).strip().lower()
        if selected_scope not in RESTORE_SCOPES:
            return {
                "ok": False,
                "status": "unknown_scope",
                "reason": f"unknown restore scope {scope!r}; use one of {', '.join(RESTORE_SCOPES)}",
                "scopes": list(RESTORE_SCOPES),
            }
        available = self.turns()
        available_ids = [str(row["turn_id"]) for row in available]
        wanted: List[str] = []
        if turn_ids:
            for value in turn_ids:
                segment = _staged_segment(value)
                if segment not in available_ids:
                    return {
                        "ok": False,
                        "status": "unknown_turn",
                        "reason": f"turn {value!r} has no staged snapshot",
                        "turns": available_ids,
                    }
                if segment not in wanted:
                    wanted.append(segment)
        elif snapshot_ids:
            for value in snapshot_ids:
                try:
                    record = self.load_snapshot(str(value))
                except CheckpointError as exc:
                    return {
                        "ok": False,
                        "status": "unknown_snapshot",
                        "reason": str(exc),
                        "turns": available_ids,
                    }
                segment = str(record.get("turn_id") or "")
                if segment and segment not in wanted:
                    wanted.append(segment)
        else:
            span = max(1, int(count or 1))
            # ``turns()`` is newest-first, so the newest span is the HEAD of
            # the list, not its tail. Reading the tail here picked the OLDEST
            # turn, which is the one thing a "revert the last change" must not
            # do.
            wanted = list(available_ids[:span])
        if not wanted:
            return {
                "ok": False,
                "status": "nothing_staged",
                "reason": "no turn has a staged snapshot yet",
                "turns": available_ids,
            }
        previous = self.staged()
        merged = list(previous.get("turn_ids") or [])
        for turn in wanted:
            if turn not in merged:
                merged.append(turn)
        merged = [turn for turn in available_ids if turn in set(merged)]
        widened = (
            0
            if not previous
            else max(0, len(merged) - len(previous.get("turn_ids") or []))
        )
        snapshot_set: List[str] = []
        for row in self._rows("snapshot"):
            if str(row.get("turn_id") or "") in set(merged):
                snapshot_id = str(row.get("snapshot_id") or "")
                if snapshot_id and snapshot_id not in snapshot_set:
                    snapshot_set.append(snapshot_id)
        # A stage call WIDENS the range and never silently re-granularises it:
        # the scope a user already chose is the one they meant. Changing it is
        # its own operation (``set_scope``), so a stray re-stage cannot throw
        # away a deliberate choice.
        chosen_scope = (
            str(previous.get("scope") or selected_scope) if previous else selected_scope
        )
        record = {
            "schema_version": _UNDO_SCHEMA_VERSION,
            "repo_key": _repo_key(self.repo_path),
            "session_id": self.session_id,
            "turn_ids": merged,
            "snapshot_ids": snapshot_set,
            "scope": chosen_scope,
            "staged_at": time.time(),
            "widened": int(previous.get("widened") or 0) + widened,
            "paths": self._target_paths(snapshot_set),
        }
        self._write_staged(record)
        self._append(
            {
                "kind": "staged",
                "turn_ids": merged,
                "snapshot_ids": snapshot_set,
                "scope": record["scope"],
                "widened": record["widened"],
            }
        )
        self._note("undo_revert_staged", {"turns": merged, "scope": record["scope"]})
        return {
            "ok": True,
            "status": "staged",
            "turn_ids": merged,
            "snapshot_ids": snapshot_set,
            "scope": record["scope"],
            "widened": record["widened"],
            "paths": record["paths"],
            "available_turns": available_ids,
        }

    def set_scope(self, scope: str) -> Dict[str, Any]:
        """Change the granularity of the ALREADY-STAGED range.

        Its own operation on purpose. Widening a range must not quietly
        re-granularise it, and narrowing one must not silently widen it: a
        user who said "code only" and then pressed undo again still means code
        only.
        """
        selected = str(scope or "").strip().lower()
        if selected not in RESTORE_SCOPES:
            return {
                "ok": False,
                "status": "unknown_scope",
                "reason": f"unknown restore scope {scope!r}; use one of {', '.join(RESTORE_SCOPES)}",
                "scopes": list(RESTORE_SCOPES),
            }
        staged = self.staged()
        if not staged:
            return {
                "ok": False,
                "status": "nothing_staged",
                "reason": "nothing is staged; run undo first",
            }
        record = dict(staged)
        record["scope"] = selected
        record["repo_key"] = _repo_key(self.repo_path)
        record["session_id"] = self.session_id
        record["schema_version"] = _UNDO_SCHEMA_VERSION
        self._write_staged(record)
        self._append(
            {
                "kind": "staged_scope_changed",
                "turn_ids": staged["turn_ids"],
                "scope": selected,
            }
        )
        return {
            "ok": True,
            "status": "staged",
            "turn_ids": staged["turn_ids"],
            "snapshot_ids": staged["snapshot_ids"],
            "scope": selected,
            "widened": staged.get("widened") or 0,
            "paths": staged.get("paths") or [],
        }

    def widen(self, *, count: int = 1) -> Dict[str, Any]:
        """Extend the staged range by ``count`` more turns."""
        staged = self.staged()
        if not staged:
            return self.stage(count=count)
        span = max(1, int(count or 1)) + len(staged.get("turn_ids") or [])
        return self.stage(
            count=span, scope=str(staged.get("scope") or DEFAULT_RESTORE_SCOPE)
        )

    def discard(self) -> Dict[str, Any]:
        """Drop the staged range without reverting anything."""
        staged = self.staged()
        if not staged:
            return {"ok": True, "status": "nothing_staged", "turn_ids": []}
        try:
            self.staged_path.unlink()
        except OSError as exc:
            return {
                "ok": False,
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        self._append(
            {"kind": "staged_discarded", "turn_ids": staged.get("turn_ids") or []}
        )
        return {
            "ok": True,
            "status": "discarded",
            "turn_ids": staged.get("turn_ids") or [],
        }

    # -- restore -------------------------------------------------------

    def _accounted_hashes(self, snapshot_ids: Sequence[str]) -> Dict[str, set]:
        """Return, per path, every content hash the staged range has recorded.

        This is what separates the run's OWN edits from a concurrent user edit.
        A pre-image alone cannot: a revert is SUPPOSED to overwrite the change
        its own turn made, so "the file differs from the pre-image" is not by
        itself a reason to refuse. The run's post-image is recorded too (every
        step captures before AND after), so a current hash that appears nowhere
        in the range is the only real evidence of a third party, and that is
        what gets refused.
        """
        accounted: Dict[str, set] = {}
        for snapshot_id in snapshot_ids:
            try:
                record = self.load_snapshot(snapshot_id)
            except CheckpointError:
                continue
            for relative, entry in (record.get("files") or {}).items():
                digest = entry.get("hash") if entry.get("exists") else None
                accounted.setdefault(str(relative), set()).add(
                    digest if digest is not None else _ABSENT
                )
        return accounted

    def _target_paths(self, snapshot_ids: Sequence[str]) -> List[str]:
        """Return the sorted pre-image keys a snapshot set would rewrite."""
        return sorted(self._target_path_map(snapshot_ids))

    def _target_path_map(
        self, snapshot_ids: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Return the pre-image each path must return to, earliest snapshot wins.

        Widening the range therefore reaches further back, which is exactly
        what a second ``/undo`` should do.
        """
        target: Dict[str, Dict[str, Any]] = {}
        for snapshot_id in snapshot_ids:
            try:
                record = self.load_snapshot(snapshot_id)
            except CheckpointError:
                continue
            for relative, entry in (record.get("files") or {}).items():
                target.setdefault(str(relative), dict(entry))
        return target

    def _target_conversation(self, snapshot_ids: Sequence[str]) -> Dict[str, Any]:
        target: Dict[str, Any] = {}
        for snapshot_id in snapshot_ids:
            try:
                record = self.load_snapshot(snapshot_id)
            except CheckpointError:
                continue
            for name, entry in (record.get("conversation") or {}).items():
                target.setdefault(str(name), dict(entry))
        return target

    def plan(self, *, scope: Optional[str] = None) -> Dict[str, Any]:
        """Answer "what WOULD this revert do" without touching the workspace."""
        staged = self.staged()
        if not staged:
            return {
                "ok": False,
                "status": "nothing_staged",
                "reason": "nothing is staged",
            }
        return self._revert(
            snapshot_ids=staged.get("snapshot_ids") or [],
            turn_ids=staged.get("turn_ids") or [],
            scope=scope or str(staged.get("scope") or DEFAULT_RESTORE_SCOPE),
            force=False,
            dry_run=True,
        )

    def commit(
        self,
        *,
        scope: Optional[str] = None,
        force: bool = False,
        live_run: bool = False,
    ) -> Dict[str, Any]:
        """Apply the staged revert and return a verified receipt.

        Refuses while ``live_run`` is set: reverting a tree a run is still
        editing is how an agent's work and a user's edit destroy each other.
        Refuses a path whose content changed since the capture unless ``force``
        is set, and a forced receipt lists every overwritten user edit under its
        own key so it can never be read as a clean restore.
        """
        staged = self.staged()
        if not staged:
            return {
                "ok": False,
                "status": "nothing_staged",
                "reason": "nothing is staged; run undo first",
                "restored": [],
                "refused": [],
                "excluded": [],
                "verified": False,
            }
        if live_run:
            self._append(
                {
                    "kind": "revert_refused",
                    "reason": "run_in_flight",
                    "turn_ids": staged.get("turn_ids") or [],
                }
            )
            self._note("undo_revert_refused", {"reason": "run_in_flight"})
            return {
                "ok": False,
                "status": "refused",
                "reason": "run_in_flight",
                "detail": "a run is editing this repository; wait for it or cancel it before undoing",
                "turn_ids": staged.get("turn_ids") or [],
                "restored": [],
                "refused": [],
                "excluded": [],
                "verified": False,
            }
        return self._revert(
            snapshot_ids=staged.get("snapshot_ids") or [],
            turn_ids=staged.get("turn_ids") or [],
            scope=scope or str(staged.get("scope") or DEFAULT_RESTORE_SCOPE),
            force=bool(force),
            dry_run=False,
        )

    def _revert(
        self,
        *,
        snapshot_ids: Sequence[str],
        turn_ids: Sequence[str],
        scope: str,
        force: bool,
        dry_run: bool,
    ) -> Dict[str, Any]:
        started = time.time()
        selected = str(scope or DEFAULT_RESTORE_SCOPE)
        if selected not in RESTORE_SCOPES:
            return {
                "ok": False,
                "status": "unknown_scope",
                "reason": f"unknown restore scope {selected!r}",
                "restored": [],
                "refused": [],
                "excluded": [],
                "verified": False,
            }
        excluded: List[Dict[str, str]] = []
        for snapshot_id in snapshot_ids:
            try:
                record = self.load_snapshot(str(snapshot_id))
            except CheckpointError:
                continue
            for item in record.get("excluded") or []:
                if isinstance(item, Mapping) and item.get("path"):
                    excluded.append(
                        {
                            "path": str(item.get("path")),
                            "reason": str(item.get("reason") or "excluded"),
                        }
                    )
        restored: List[Dict[str, Any]] = []
        refused: List[Dict[str, Any]] = []
        unchanged: List[str] = []
        overwritten: List[Dict[str, Any]] = []
        conversation_receipt: Dict[str, Any]
        if selected in {"files", "both"}:
            target = self._target_path_map(snapshot_ids)
            accounted = self._accounted_hashes(snapshot_ids)
            for relative in target:
                entry = target[relative]
                try:
                    live = _safe_target(self.repo_path, relative)
                except CheckpointError as exc:
                    refused.append({"path": relative, "reason": str(exc)})
                    continue
                before = _digest_file(live)
                expected = entry.get("hash") if entry.get("exists") else None
                if before == expected and bool(entry.get("exists")) == live.is_file():
                    unchanged.append(relative)
                    continue
                known = accounted.get(relative, set())
                current_state = _ABSENT if before is None else before
                if current_state not in known:
                    if not force:
                        refused.append(
                            {
                                "path": relative,
                                "reason": "changed_since_capture",
                                "expected_hash": expected,
                                "actual_hash": before,
                                "detail": "the file changed after it was staged and the change is not one this run recorded; revert refused rather than overwriting it",
                            }
                        )
                        continue
                    # A forced revert still RECORDS that it destroyed somebody's
                    # edit. A force flag that silently skipped the record would
                    # make a destructive revert indistinguishable from a clean
                    # one in the receipt.
                    overwritten.append(
                        {
                            "path": relative,
                            "reason": "changed_since_capture",
                            "overwritten_hash": before,
                            "restored_to_hash": expected,
                        }
                    )
                if entry.get("exists"):
                    payload = self._read_object(str(entry.get("object") or ""))
                    if payload is None:
                        refused.append(
                            {
                                "path": relative,
                                "reason": "pre_image_missing",
                                "detail": "the stored pre-image is missing or failed its integrity check",
                            }
                        )
                        continue
                    if not dry_run:
                        live.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            _atomic_bytes(live, payload)
                            try:
                                os.chmod(live, int(entry.get("mode") or 0o644))
                            except OSError:
                                pass
                        except (OSError, RuntimeError, ValueError) as exc:
                            refused.append(
                                {
                                    "path": relative,
                                    "reason": f"write_failed: {type(exc).__name__}",
                                    "detail": str(exc),
                                }
                            )
                            continue
                    restored.append(
                        {
                            "path": relative,
                            "action": "restored",
                            "before_hash": before,
                            "after_hash": entry.get("hash"),
                            "verified": True,
                        }
                    )
                else:
                    if not dry_run and (live.is_file() or live.is_symlink()):
                        try:
                            live.unlink()
                        except OSError as exc:
                            refused.append(
                                {
                                    "path": relative,
                                    "reason": f"delete_failed: {type(exc).__name__}",
                                    "detail": str(exc),
                                }
                            )
                            continue
                    restored.append(
                        {
                            "path": relative,
                            "action": "deleted",
                            "before_hash": before,
                            "after_hash": None,
                            "verified": True,
                        }
                    )
            conversation_receipt = {
                "action": "kept",
                "reason": "the granularity is code only",
            }
        else:
            # A `conversation` revert must not touch a single file, or the
            # granularity is a lie: the user asked to rewind the thread, not
            # the tree.
            conversation_receipt = {
                "action": "pending",
                "reason": "the granularity is the conversation only",
            }
        if selected in {"conversation", "both"}:
            conversation_receipt = self._restore_conversation(
                self._target_conversation(snapshot_ids),
                snapshot_ids,
                force=force,
                dry_run=dry_run,
            )
        # ``verified`` means "every path this receipt claims to have restored
        # really hashes to its recorded pre-image". ``all([])`` is True, so an
        # empty restore would otherwise claim it verified a revert that
        # restored nothing - which is the exact class of quiet success this
        # repository keeps having to repair. A receipt that restored nothing
        # and refused everything is NOT verified.
        checked_paths = len(restored) + len(unchanged)
        verified = (
            bool(checked_paths)
            and not refused
            and all(item.get("verified") for item in restored)
        )
        receipt: Dict[str, Any] = {
            "schema_version": _UNDO_SCHEMA_VERSION,
            "ok": not refused,
            "status": "dry_run"
            if dry_run
            else ("reverted" if not refused else "partial"),
            "scope": selected,
            "turn_ids": [str(item) for item in turn_ids],
            "snapshot_ids": [str(item) for item in snapshot_ids],
            "restored": restored,
            "unchanged": sorted(unchanged),
            "refused": refused,
            "excluded": excluded,
            "overwritten_user_edits": overwritten,
            "overwritten_count": len(overwritten),
            "conversation": conversation_receipt,
            "forced": bool(force),
            "dry_run": bool(dry_run),
            "verified": verified,
            "verified_paths": len(restored),
            "checked_paths": checked_paths,
            "verified_at": time.time(),
            "duration_ms": round((time.time() - started) * 1000.0, 3),
            "repo_key": _repo_key(self.repo_path),
        }
        if not dry_run:
            receipt_id = f"revert-{uuid.uuid4().hex[:16]}"
            _atomic_bytes(
                self.receipts / f"{receipt_id}.json",
                json.dumps(
                    receipt, ensure_ascii=False, indent=2, sort_keys=True, default=str
                ).encode("utf-8"),
            )
            receipt["receipt_id"] = receipt_id
            self._append(
                {
                    "kind": "reverted",
                    "receipt_id": receipt_id,
                    "scope": selected,
                    "restored": len(restored),
                    "refused": len(refused),
                    "forced": bool(force),
                }
            )
            if not refused:
                try:
                    self.staged_path.unlink()
                except OSError:
                    pass
            self._note(
                "undo_revert_applied",
                {"scope": selected, "restored": len(restored), "refused": len(refused)},
            )
        return receipt

    def _accounted_conversation(self, snapshot_ids: Sequence[str]) -> Dict[str, set]:
        """Every conversation hash the staged range recorded.

        The same rule as the files axis, and for the same reason: a
        conversation grows as the run talks, and a revert is SUPPOSED to
        rewind that growth. A current hash that appears nowhere in the range is
        the only evidence that a person wrote it, and that is what gets refused.
        """
        accounted: Dict[str, set] = {}
        for snapshot_id in snapshot_ids:
            try:
                record = self.load_snapshot(snapshot_id)
            except CheckpointError:
                continue
            for name, entry in (record.get("conversation") or {}).items():
                digest = entry.get("hash")
                accounted.setdefault(str(name), set()).add(
                    digest if digest else _ABSENT
                )
        return accounted

    def _restore_conversation(
        self,
        target: Mapping[str, Any],
        snapshot_ids: Sequence[str],
        *,
        force: bool,
        dry_run: bool,
    ) -> Dict[str, Any]:
        """Restore conversation artifacts, or say exactly why not."""
        if not target:
            return {
                "action": "not_captured",
                "reason": "no conversation snapshot was captured for these turns",
            }
        root = self.log_root / "_conversations"
        if _has_symlink_component(root, self.log_root):
            return {
                "action": "refused",
                "reason": "conversation root is not a real directory",
            }
        accounted = self._accounted_conversation(snapshot_ids)
        results: List[Dict[str, Any]] = []
        for name, entry in sorted(target.items()):
            candidate = root / name
            before = _digest_file(candidate) if candidate.is_file() else None
            expected = str(entry.get("hash") or "")
            if before == expected:
                results.append(
                    {
                        "path": name,
                        "action": "unchanged",
                        "before_hash": before,
                        "after_hash": before,
                    }
                )
                continue
            known = accounted.get(name, set())
            if (_ABSENT if before is None else before) not in known and not force:
                results.append(
                    {
                        "path": name,
                        "action": "refused",
                        "reason": "changed_since_capture",
                        "detail": "the conversation changed and the change is not one this run recorded",
                        "before_hash": before,
                        "expected_hash": expected,
                    }
                )
                continue
            payload = self._read_object(str(entry.get("object") or ""))
            if payload is None:
                results.append(
                    {"path": name, "action": "refused", "reason": "pre_image_missing"}
                )
                continue
            if not dry_run:
                try:
                    _atomic_bytes(candidate, payload)
                except (OSError, RuntimeError, ValueError) as exc:
                    results.append(
                        {
                            "path": name,
                            "action": "refused",
                            "reason": f"write_failed: {type(exc).__name__}",
                        }
                    )
                    continue
            results.append(
                {
                    "path": name,
                    "action": "restored",
                    "before_hash": before,
                    "after_hash": expected,
                }
            )
        actions = {str(item.get("action")) for item in results}
        if "refused" in actions:
            return {"action": "partial", "files": results}
        if actions == {"unchanged"}:
            return {"action": "already_current", "files": results}
        return {"action": "restored", "files": results}

    def receipt(self, receipt_id: str) -> Dict[str, Any]:
        """Load one stored revert receipt."""
        segment = _staged_segment(receipt_id, fallback="revert")
        path = self.receipts / f"{segment}.json"
        if not path.is_file() or path.is_symlink():
            raise CheckpointNotFoundError(f"receipt {receipt_id!r} was not found")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise CheckpointCorruptError("revert receipt is corrupt") from exc
        if not isinstance(value, dict):
            raise CheckpointCorruptError("revert receipt is corrupt")
        return value


def _staged_undo_store(
    repo_path: Any, log_root: Any, session_id: str, **kwargs: Any
) -> StagedSnapshotStore:
    return StagedSnapshotStore(repo_path, log_root, session_id, **kwargs)


@contextmanager
def checkpoint_guard(
    repo_path: Any,
    log_root: Any,
    **kwargs: Any,
) -> Iterator[CheckpointResult]:
    """Yield a persisted pre-mutation checkpoint for a mutation scope."""
    record = create_checkpoint(repo_path, log_root, **kwargs)
    yield record


def create_checkpoint(
    repo_path: Any,
    log_root: Any,
    *,
    session_id: str = "",
    session: Any = None,
    files: Optional[Sequence[str]] = None,
    mode: str = "shadow_git",
    label: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> CheckpointResult:
    """Create a checkpoint using the default durable manager."""
    return CheckpointManager(repo_path, log_root, session_id).create_checkpoint(
        session=session,
        files=files,
        mode=mode,
        label=label,
        metadata=metadata,
    )


def checkpoint_before_mutation(
    repo_path: Any,
    log_root: Any,
    *,
    session_id: str = "",
    session: Any = None,
    files: Optional[Sequence[str]] = None,
    mode: str = "shadow_git",
    label: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> CheckpointResult:
    """Create a named checkpoint before a mutation is dispatched."""
    return create_checkpoint(
        repo_path,
        log_root,
        session_id=session_id,
        session=session,
        files=files,
        mode=mode,
        label=label,
        metadata=metadata,
    )


def list_checkpoints(
    repo_path: Any, log_root: Any, session_id: str = ""
) -> List[CheckpointResult]:
    """List checkpoints for a workspace and optional conversation."""
    return CheckpointManager(repo_path, log_root, session_id).list_checkpoints()


def load_checkpoint(
    repo_path: Any, log_root: Any, checkpoint_id: str, session_id: str = ""
) -> CheckpointResult:
    """Load one checkpoint record."""
    return CheckpointManager(repo_path, log_root, session_id).load_checkpoint(
        checkpoint_id
    )


def review_checkpoint(
    repo_path: Any, log_root: Any, checkpoint_id: str, session_id: str = ""
) -> CheckpointResult:
    """Review drift between a workspace and a checkpoint."""
    return CheckpointManager(repo_path, log_root, session_id).review_checkpoint(
        checkpoint_id
    )


def diff_checkpoint(
    repo_path: Any, log_root: Any, checkpoint_id: str, session_id: str = ""
) -> str:
    """Return a checkpoint's bounded unified diff."""
    return CheckpointManager(repo_path, log_root, session_id).diff_checkpoint(
        checkpoint_id
    )


def restore_checkpoint(
    repo_path: Any,
    log_root: Any,
    checkpoint_id: str,
    *,
    session_id: str = "",
    restore_files: bool = True,
    restore_conversation: bool = False,
    force: bool = False,
    strict: bool = False,
) -> RestoreResult:
    """Restore a checkpoint through conflict-safe file and conversation paths."""
    return CheckpointManager(repo_path, log_root, session_id).restore_checkpoint(
        checkpoint_id,
        restore_files=restore_files,
        restore_conversation=restore_conversation,
        force=force,
        strict=strict,
    )


# --- AGT-09 module surface --------------------------------------------------
#
# These mirror the checkpoint helpers above so a caller that already speaks
# this module's functional style can drive the staged store without importing
# the class. They are thin: there is no logic here that the class does not have.


def capture_staged_snapshot(
    repo_path: Any,
    log_root: Any,
    *,
    turn_id: str,
    session_id: str = "",
    event: str = "step",
    label: str = "",
    paths: Optional[Sequence[str]] = None,
    scope_prefixes: Optional[Sequence[str]] = None,
    conversation: bool = True,
    changed_paths: Optional[Sequence[str]] = None,
    max_file_bytes: int = _UNDO_DEFAULT_MAX_FILE_BYTES,
    trace: Any = None,
) -> Dict[str, Any]:
    """Capture a staged pre-image; never raises (a failure is a record)."""
    return StagedSnapshotStore(
        repo_path, log_root, session_id, max_file_bytes=max_file_bytes, trace=trace
    ).capture(
        turn_id=turn_id,
        event=event,
        label=label,
        paths=paths,
        scope_prefixes=scope_prefixes,
        conversation=conversation,
        changed_paths=changed_paths,
    )


def stage_staged_revert(
    repo_path: Any,
    log_root: Any,
    *,
    session_id: str = "",
    count: int = 1,
    turn_ids: Optional[Sequence[str]] = None,
    scope: str = DEFAULT_RESTORE_SCOPE,
) -> Dict[str, Any]:
    """Stage a revert of the newest ``count`` turns, widening any existing range."""
    return StagedSnapshotStore(repo_path, log_root, session_id).stage(
        count=count, turn_ids=turn_ids, scope=scope
    )


def widen_staged_revert(
    repo_path: Any, log_root: Any, *, session_id: str = "", count: int = 1
) -> Dict[str, Any]:
    """Extend the staged revert range by ``count`` more turns."""
    return StagedSnapshotStore(repo_path, log_root, session_id).widen(count=count)


def staged_revert_state(
    repo_path: Any, log_root: Any, *, session_id: str = ""
) -> Dict[str, Any]:
    """Return the staged revert range, or an empty mapping when nothing is staged."""
    return StagedSnapshotStore(repo_path, log_root, session_id).staged()


def staged_revert_plan(
    repo_path: Any,
    log_root: Any,
    *,
    session_id: str = "",
    scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Describe what committing the staged revert WOULD do, writing nothing."""
    return StagedSnapshotStore(repo_path, log_root, session_id).plan(scope=scope)


def commit_staged_revert(
    repo_path: Any,
    log_root: Any,
    *,
    session_id: str = "",
    scope: Optional[str] = None,
    force: bool = False,
    live_run: bool = False,
) -> Dict[str, Any]:
    """Apply the staged revert and return the hash-verified receipt."""
    return StagedSnapshotStore(repo_path, log_root, session_id).commit(
        scope=scope, force=force, live_run=live_run
    )


def discard_staged_revert(
    repo_path: Any, log_root: Any, *, session_id: str = ""
) -> Dict[str, Any]:
    """Drop the staged range without reverting anything."""
    return StagedSnapshotStore(repo_path, log_root, session_id).discard()
