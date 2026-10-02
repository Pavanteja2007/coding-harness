"""Configurable, fail-closed retention and deletion for shared run artifacts."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from .security import SecurityViolation, redact_text, require_contained, safe_segment

__all__ = [
    "RetentionError",
    "RetentionPolicy",
    "RetentionReport",
    "apply_retention",
    "delete_task_data",
    "enforce_retention",
    "prune_logs",
    "retention_from_env",
]

PathLike = Union[str, os.PathLike[str]]


class RetentionError(RuntimeError):
    """Raised when a retention operation cannot complete safely."""


@dataclass(frozen=True)
class RetentionPolicy:
    """Validated age, byte, and keep-latest retention limits."""

    max_age_s: Optional[float] = None
    max_bytes: Optional[int] = None
    keep_latest: int = 0
    patterns: tuple[str, ...] = ("*",)

    def __post_init__(self) -> None:
        if self.max_age_s is not None and float(self.max_age_s) < 0:
            raise ValueError("max_age_s must be non-negative")
        if self.max_bytes is not None and int(self.max_bytes) < 0:
            raise ValueError("max_bytes must be non-negative")
        if int(self.keep_latest) < 0:
            raise ValueError("keep_latest must be non-negative")
        if not self.patterns:
            raise ValueError("at least one retention pattern is required")


@dataclass(frozen=True)
class RetentionReport:
    """Machine-readable result of a retention pass."""

    status: str
    root: str
    candidates: int = 0
    deleted: tuple[str, ...] = ()
    bytes_deleted: int = 0
    errors: tuple[str, ...] = ()
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible report with no raw filesystem secrets."""
        return {
            "status": self.status,
            "root": redact_text(self.root),
            "candidates": self.candidates,
            "deleted": [redact_text(item) for item in self.deleted],
            "bytes_deleted": self.bytes_deleted,
            "errors": [redact_text(item) for item in self.errors],
            "dry_run": self.dry_run,
        }


def retention_from_env(environ: Optional[dict[str, str]] = None) -> RetentionPolicy:
    """Build a policy from NEO retention environment variables."""
    source = dict(os.environ if environ is None else environ)
    days = source.get("NEO_TRACE_RETENTION_DAYS")
    byte_limit = source.get("NEO_TRACE_RETENTION_BYTES")
    keep = source.get("NEO_TRACE_RETENTION_KEEP", "0")
    try:
        max_age = float(days) * 86400.0 if days not in (None, "") else None
        max_bytes = int(byte_limit) if byte_limit not in (None, "") else None
        keep_latest = int(keep)
    except (TypeError, ValueError) as exc:
        raise RetentionError("retention environment values are invalid") from exc
    return RetentionPolicy(
        max_age_s=max_age, max_bytes=max_bytes, keep_latest=keep_latest
    )


def _matches(path: Path, root: Path, patterns: tuple[str, ...]) -> bool:
    relative = path.relative_to(root).as_posix()
    name = path.name
    return any(
        fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(name, pattern)
        for pattern in patterns
    )


def _receipt_path(root: Path) -> Path:
    return require_contained(root, "_retention_receipts.jsonl")


def _append_receipt(root: Path, report: RetentionReport) -> None:
    path = _receipt_path(root)
    if path.is_symlink():
        raise SecurityViolation("retention receipt must not be a symbolic link")
    payload = {"ts": round(time.time(), 3), **report.as_dict()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RetentionError("retention receipt could not be written") from exc


def _walk_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise SecurityViolation("retention refused a symlinked directory")
            safe_directories.append(directory)
        directories[:] = safe_directories
        for name in names:
            candidate = current_path / name
            if candidate.is_symlink():
                raise SecurityViolation("retention refused a symlinked file")
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise SecurityViolation("retention candidate escaped its root") from exc
            if _matches(candidate, root, patterns) and candidate != _receipt_path(root):
                files.append(candidate)
    return files


def apply_retention(
    root: PathLike,
    policy: Optional[RetentionPolicy] = None,
    *,
    max_age_s: Optional[float] = None,
    max_bytes: Optional[int] = None,
    keep_latest: Optional[int] = None,
    patterns: Optional[Iterable[str]] = None,
    dry_run: bool = False,
    now: Optional[float] = None,
    write_receipt: bool = True,
) -> RetentionReport:
    """Delete expired artifacts below ``root`` after a complete preflight.

    All symlinks and paths outside the configured root fail the operation
    before any deletion occurs.  A dry run never mutates the filesystem.
    """
    if policy is None:
        policy = RetentionPolicy(
            max_age_s=max_age_s,
            max_bytes=max_bytes,
            keep_latest=0 if keep_latest is None else int(keep_latest),
            patterns=tuple(patterns or ("*",)),
        )
    elif (
        any(value is not None for value in (max_age_s, max_bytes, keep_latest))
        or patterns is not None
    ):
        raise ValueError("pass either a RetentionPolicy or inline limits, not both")
    root_path = require_contained(root, ".")
    if not root_path.is_dir():
        raise RetentionError("retention root is not a directory")
    timestamp = time.time() if now is None else float(now)
    candidates = _walk_files(root_path, policy.patterns)
    candidates.sort(key=lambda path: (path.stat().st_mtime, str(path)))
    keep = max(0, int(policy.keep_latest))
    protected = set(candidates[-keep:]) if keep else set()
    selected: set[Path] = set()
    if policy.max_age_s is not None:
        cutoff = timestamp - float(policy.max_age_s)
        selected.update(path for path in candidates if path.stat().st_mtime < cutoff)
    if policy.max_bytes is not None:
        total = sum(path.stat().st_size for path in candidates)
        for path in candidates:
            if total <= int(policy.max_bytes):
                break
            if path in protected:
                continue
            selected.add(path)
            total -= path.stat().st_size
    selected -= protected
    selected_paths = sorted(
        selected, key=lambda path: (path.stat().st_mtime, str(path))
    )
    if dry_run:
        return RetentionReport(
            "dry_run",
            str(root_path),
            len(selected_paths),
            tuple(path.relative_to(root_path).as_posix() for path in selected_paths),
            sum(path.stat().st_size for path in selected_paths),
            dry_run=True,
        )
    deleted: list[str] = []
    deleted_bytes = 0
    errors: list[str] = []
    for path in selected_paths:
        try:
            if path.is_symlink():
                raise SecurityViolation("retention refused a symlinked file")
            size = path.stat().st_size
            path.unlink()
            deleted.append(path.relative_to(root_path).as_posix())
            deleted_bytes += size
        except (OSError, SecurityViolation) as exc:
            errors.append(
                f"{path.relative_to(root_path).as_posix()}: {type(exc).__name__}"
            )
    report = RetentionReport(
        "failed" if errors else "ok",
        str(root_path),
        len(selected_paths),
        tuple(deleted),
        deleted_bytes,
        tuple(errors),
    )
    if write_receipt:
        _append_receipt(root_path, report)
    if errors:
        raise RetentionError("retention deletion failed for one or more files")
    return report


enforce_retention = apply_retention
prune_logs = apply_retention


def delete_task_data(root: PathLike, task_id: str) -> dict[str, Any]:
    """Delete one safe task directory without following symlinks."""
    if not safe_segment(task_id):
        raise SecurityViolation("task id is not a safe segment")
    root_path = require_contained(root, ".")
    target = require_contained(root_path, task_id, must_exist=True, directory=True)
    if target.is_symlink():
        raise SecurityViolation("task directory must not be a symbolic link")
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise RetentionError("task data could not be deleted") from exc
    return {"task_id": redact_text(task_id), "deleted": True}
