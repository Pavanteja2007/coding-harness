"""Git worktree isolation and dependency-patch transport for workflows."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from runtime.fsutil import atomic_write_json, now_iso
from runtime.paths import validate_path_segment
from shared.security import SecurityViolation, require_contained

INTEGRATION_NODE_ID = "integration"


class WorktreeError(RuntimeError):
    """Raised when a workflow worktree cannot be safely created or used."""


class WorktreeConflict(WorktreeError):
    """Raised when a dependency patch does not apply cleanly."""


class WorktreeBusy(WorktreeError):
    """Raised when cleanup is requested for a dirty or active worktree."""


@dataclass
class WorktreeRecord:
    """Durable metadata for one isolated child workspace."""

    node_id: str
    path: str
    source_repo: str
    base_commit: str
    state: str = "ready"
    dependency_patches: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible worktree record."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorktreeRecord":
        """Build a record from persisted JSON."""
        data = dict(value or {})
        return cls(
            node_id=str(data.get("node_id", "")),
            path=str(data.get("path", "")),
            source_repo=str(data.get("source_repo", "")),
            base_commit=str(data.get("base_commit", "")),
            state=str(data.get("state", "ready")),
            dependency_patches=[
                str(item) for item in data.get("dependency_patches", [])
            ],
            created_at=str(data.get("created_at", now_iso())),
            last_error=str(data.get("last_error", "")),
        )


class WorktreeManager:
    """Create detached Git worktrees and apply dependency patches safely."""

    def __init__(
        self,
        source_repo: str | os.PathLike[str],
        root: str | os.PathLike[str],
        *,
        git_timeout_s: float = 30.0,
        max_patch_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.source_repo = Path(source_repo).expanduser().resolve()
        self.root = Path(root).expanduser().resolve()
        self.git_timeout_s = max(1.0, float(git_timeout_s))
        self.max_patch_bytes = max(1024, int(max_patch_bytes))
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "worktrees.json"
        self.records: Dict[str, WorktreeRecord] = {}
        self._load()

    def ensure_clean(self) -> str:
        """Require a clean source checkout and return its pinned base commit."""
        self._require_git_repo(self.source_repo)
        status = self._git(
            self.source_repo, "status", "--porcelain", "--untracked-files=all"
        )
        if status.strip():
            raise WorktreeError("source repository has uncommitted changes")
        return self.base_commit()

    def base_commit(self) -> str:
        """Return the current source commit used for detached worktrees."""
        self._require_git_repo(self.source_repo)
        return self._git(self.source_repo, "rev-parse", "HEAD").strip()

    def create(
        self,
        node_id: str,
        *,
        dependency_patches: Sequence[Tuple[str, str | os.PathLike[str]]] = (),
        base_commit: Optional[str] = None,
    ) -> WorktreeRecord:
        """Create one detached worktree and apply ordered dependency patches."""
        safe_node = validate_path_segment(str(node_id), "workflow node id")
        existing = self.records.get(safe_node)
        if existing is not None:
            self._refresh_record(existing)
            if existing.state == "ready":
                return existing
            raise WorktreeError(
                f"worktree for {safe_node} is not reusable: {existing.state}"
            )
        self._require_git_repo(self.source_repo)
        commit = str(base_commit or self.base_commit()).strip()
        if not commit:
            raise WorktreeError("source repository has no base commit")
        destination = self.root / safe_node
        try:
            destination = require_contained(self.root, destination)
        except SecurityViolation as exc:
            raise WorktreeError(str(exc)) from exc
        if destination.exists():
            raise WorktreeError(f"worktree destination already exists: {destination}")
        record = WorktreeRecord(
            node_id=safe_node,
            path=str(destination),
            source_repo=str(self.source_repo),
            base_commit=commit,
            state="creating",
        )
        self.records[safe_node] = record
        self._save()
        try:
            self._git(
                self.source_repo,
                "worktree",
                "add",
                "--detach",
                str(destination),
                commit,
            )
            record.state = "ready"
            self._save()
            for dependency_id, patch_path in dependency_patches:
                self.apply_patch(record, patch_path, dependency_id=dependency_id)
            self._save()
            self._refresh_record(record)
            return record
        except WorktreeConflict as exc:
            record.state = "conflict"
            record.last_error = str(exc)
            self._save()
            raise
        except Exception as exc:
            record.state = "error"
            record.last_error = str(exc)
            self._save()
            raise

    def capture_patch(
        self,
        record: WorktreeRecord | str,
        *,
        patch_path: str | os.PathLike[str] | None = None,
    ) -> str:
        """Capture a binary-safe patch representing the worktree changes."""
        selected = self._record(record)
        self._git(selected.path, "add", "-N", "--", ".")
        output = self._git(
            selected.path,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--full-index",
            "--",
            ".",
        )
        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) > self.max_patch_bytes:
            raise WorktreeError("worktree patch exceeds configured size limit")
        if patch_path is None:
            patch_path = self.root / "patches" / f"{selected.node_id}.patch"
        target = Path(patch_path)
        try:
            target = require_contained(self.root, target)
        except SecurityViolation as exc:
            raise WorktreeError(str(exc)) from exc
        self._atomic_write_text(target, output)
        selected.state = "ready"
        self._save()
        return str(target)

    def apply_patch(
        self,
        record: WorktreeRecord | str,
        patch_path: str | os.PathLike[str],
        *,
        dependency_id: str = "",
    ) -> None:
        """Apply a dependency patch, refusing conflicts and partial states."""
        selected = self._record(record)
        source = Path(patch_path)
        try:
            source = require_contained(self.root, source, must_exist=True)
        except SecurityViolation as exc:
            raise WorktreeConflict(str(exc)) from exc
        if source.stat().st_size == 0:
            selected.dependency_patches.append(str(source))
            selected.state = "ready"
            self._save()
            return
        try:
            self._git(
                selected.path,
                "apply",
                "--check",
                "--binary",
                "--whitespace=nowarn",
                str(source),
            )
            self._git(
                selected.path,
                "apply",
                "--binary",
                "--whitespace=nowarn",
                str(source),
            )
        except WorktreeError as exc:
            message = f"dependency {dependency_id or 'unknown'} patch conflict: {exc}"
            selected.state = "conflict"
            selected.last_error = message
            self._save()
            raise WorktreeConflict(message) from exc
        selected.dependency_patches.append(str(source))
        selected.state = "ready"
        self._save()

    def remove(self, record: WorktreeRecord | str, *, force: bool = False) -> None:
        """Remove a worktree, refusing dirty removal unless explicitly forced."""
        selected = self._record(record)
        path = Path(selected.path)
        if not path.exists():
            self.records.pop(selected.node_id, None)
            self._save()
            return
        try:
            self._git(
                self.source_repo,
                "worktree",
                "remove",
                *(["--force"] if force else []),
                str(path),
            )
        except WorktreeError as exc:
            if not force:
                raise WorktreeBusy(str(exc)) from exc
            raise
        self.records.pop(selected.node_id, None)
        self._save()
        self.prune()

    def recover(self) -> Dict[str, WorktreeRecord]:
        """Reconcile records after an orchestrator restart."""
        for record in self.records.values():
            path = Path(record.path)
            if not path.exists():
                record.state = "missing"
                record.last_error = "worktree path is missing"
                continue
            try:
                inside = self._git(path, "rev-parse", "--is-inside-work-tree").strip()
                if inside == "true" and record.state in {"creating", "error"}:
                    record.state = "ready"
                    record.last_error = ""
            except WorktreeError as exc:
                record.state = "error"
                record.last_error = str(exc)
        self._save()
        return dict(self.records)

    def ensure_integration(self) -> WorktreeRecord:
        """Return the single shared integration worktree, creating it once.

        Every child patch is merged into this one tree, so merges are
        serialized against a single destination rather than against each
        child's own worktree.
        """
        existing = self.records.get(INTEGRATION_NODE_ID)
        if existing is not None:
            self._refresh_record(existing)
            if existing.state == "ready" and Path(existing.path).exists():
                return existing
            raise WorktreeError(
                f"integration worktree is not reusable: {existing.state}"
            )
        return self.create(
            INTEGRATION_NODE_ID,
            base_commit=self.base_commit(),
        )

    def git_check(self, record: WorktreeRecord | str) -> str:
        """Run Git's whitespace/error check inside one worktree."""
        selected = self._record(record)
        return self._git(selected.path, "diff", "--check", "--no-ext-diff")

    def changed_files(self, record: WorktreeRecord | str) -> List[str]:
        """Return the repo-relative files a worktree currently changes."""
        selected = self._record(record)
        output = self._git(
            selected.path,
            "status",
            "--porcelain",
            "--untracked-files=all",
        )
        files: List[str] = []
        for line in output.splitlines():
            entry = line[3:].strip() if len(line) > 3 else ""
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            if entry:
                files.append(entry.replace("\\", "/"))
        return files

    def prune(self) -> None:
        """Ask Git to remove stale administrative entries."""
        try:
            self._git(self.source_repo, "worktree", "prune")
        except WorktreeError:
            return

    def list(self) -> Dict[str, WorktreeRecord]:
        """Return a copy of all managed worktree records."""
        return dict(self.records)

    def _record(self, record: WorktreeRecord | str) -> WorktreeRecord:
        key = str(record) if isinstance(record, str) else record.node_id
        try:
            selected = self.records[key]
        except KeyError as exc:
            raise WorktreeError(f"unknown worktree record: {key}") from exc
        if not Path(selected.path).exists():
            raise WorktreeError(f"worktree path is missing: {selected.path}")
        return selected

    def _refresh_record(self, record: WorktreeRecord) -> None:
        if not Path(record.path).exists():
            record.state = "missing"
            record.last_error = "worktree path is missing"
        self._save()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            import json

            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for item in data.get("worktrees", []):
            if isinstance(item, Mapping):
                record = WorktreeRecord.from_dict(item)
                if record.node_id:
                    self.records[record.node_id] = record

    def _save(self) -> None:
        atomic_write_json(
            self.state_path,
            {
                "schema_version": 1,
                "updated_at": now_iso(),
                "worktrees": [record.to_dict() for record in self.records.values()],
            },
        )

    def _require_git_repo(self, path: Path) -> None:
        if not path.is_dir():
            raise WorktreeError(f"source repository is missing: {path}")
        try:
            top = self._git(path, "rev-parse", "--show-toplevel").strip()
        except WorktreeError as exc:
            raise WorktreeError(f"not a Git repository: {path}") from exc
        if Path(top).resolve() != path:
            raise WorktreeError("source repository must be the Git top level")

    def _git(self, cwd: str | os.PathLike[str], *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.git_timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorktreeError(f"git command failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise WorktreeError(f"git {' '.join(args)} failed: {detail[:1000]}")
        return completed.stdout

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


__all__ = [
    "INTEGRATION_NODE_ID",
    "WorktreeBusy",
    "WorktreeConflict",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeRecord",
]
