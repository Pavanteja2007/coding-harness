"""Durable managed workspaces and local/remote client handles."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from shared.security import redact_secrets, safe_segment

from .errors import (
    InvalidRequestError,
    WorkspaceActiveError,
    WorkspaceError,
    WorkspaceNotFoundError,
)
from .models import new_workspace_id

__all__ = [
    "LocalWorkspaceManager",
    "RemoteWorkspaceManager",
    "Workspace",
    "WorkspaceManager",
]


@dataclass
class Workspace(Mapping[str, Any]):
    """A client-side handle for one managed workspace directory."""

    id: str
    path: str
    name: str = ""
    state: str = "ready"
    created_at: float = 0.0
    updated_at: float = 0.0
    deleted_at: float = 0.0
    source_path: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    manager: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Normalize public workspace metadata."""
        self.id = str(self.id or "")
        self.path = str(self.path or "")
        self.name = str(self.name or Path(self.path).name if self.path else self.id)
        self.state = str(self.state or "ready")
        self.created_at = float(self.created_at or time.time())
        self.updated_at = float(self.updated_at or self.created_at)
        self.deleted_at = float(self.deleted_at or 0.0)
        self.source_path = str(self.source_path or "")
        self.metadata = dict(redact_secrets(self.metadata or {}))

    @property
    def workspace_id(self) -> str:
        """Return the workspace identifier under a long alias."""
        return self.id

    @property
    def root(self) -> Path:
        """Return the workspace path as a Path."""
        return Path(self.path)

    @property
    def deleted(self) -> bool:
        """Return whether this handle represents a deleted workspace."""
        return self.state == "deleted"

    @property
    def active(self) -> bool:
        """Return whether the server or manager marked the workspace active."""
        return self.state == "active" or bool(self.metadata.get("active_run_ids"))

    def exists(self) -> bool:
        """Return whether the workspace directory still exists."""
        try:
            return self.root.is_dir()
        except OSError:
            return False

    def open(self) -> Path:
        """Return the contained workspace directory for local callers."""
        if not self.exists():
            raise WorkspaceError(f"workspace directory is unavailable: {self.id}")
        return self.root

    def delete(self) -> "Workspace":
        """Delete this workspace through its owning manager."""
        if self.manager is None:
            raise WorkspaceError("workspace has no lifecycle manager")
        updated = self.manager.delete(self.id)
        if isinstance(updated, Workspace):
            self.__dict__.update(updated.__dict__)
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible workspace record."""
        return {
            "id": self.id,
            "workspace_id": self.id,
            "name": self.name,
            "path": self.path,
            "root": str(self.root),
            "state": self.state,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
            "source_path": self.source_path,
            "metadata": dict(redact_secrets(self.metadata)),
        }

    @classmethod
    def from_record(
        cls, value: Mapping[str, Any], *, manager: Any = None
    ) -> "Workspace":
        """Build a handle from a local or remote lifecycle record."""
        data = dict(value or {})
        return cls(
            id=str(data.get("id", data.get("workspace_id", ""))),
            path=str(data.get("path", data.get("root", ""))),
            name=str(data.get("name", "")),
            state=str(data.get("state", "ready")),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
            deleted_at=float(data.get("deleted_at", 0.0) or 0.0),
            source_path=str(data.get("source_path", "")),
            metadata=dict(data.get("metadata", {})),
            manager=manager,
        )

    def __getitem__(self, key: str) -> Any:
        """Return a lifecycle field using mapping access."""
        return self.to_dict()[str(key)]

    def __iter__(self) -> Iterator[str]:
        """Iterate over lifecycle field names."""
        return iter(self.to_dict())

    def __len__(self) -> int:
        """Return the number of lifecycle fields."""
        return len(self.to_dict())


class LocalWorkspaceManager:
    """Manage durable local workspace directories beneath one safe root."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        storage_root: str | Path | None = None,
    ) -> None:
        """Create or load a manager rooted at an explicit absolute directory."""
        selected = root if root is not None else storage_root
        if selected is None:
            selected = Path.cwd() / ".neo" / "workspaces"
        raw = Path(selected).expanduser()
        absolute = raw.absolute()
        cursor = absolute
        while True:
            if cursor.is_symlink():
                raise WorkspaceError(
                    "managed workspace root must not contain a symbolic link"
                )
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        self.root = absolute.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.root / "workspaces.json"
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self.lease_seconds = 3600.0
        self._load()

    def _load(self) -> None:
        with self._lock:
            self._records = self._read_records()

    def _read_records(self) -> dict[str, dict[str, Any]]:
        """Read and validate the durable record file without trusting its paths."""
        if not self.metadata_path.exists():
            return {}
        if self.metadata_path.is_symlink():
            raise WorkspaceError("workspace metadata must not be a symbolic link")
        try:
            raw = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise WorkspaceError(f"workspace metadata is unreadable: {exc}") from exc
        records = raw.get("workspaces") if isinstance(raw, Mapping) else raw
        if isinstance(records, Mapping):
            candidates = list(records.items())
        elif isinstance(records, list):
            candidates = [
                (item.get("id"), item) for item in records if isinstance(item, Mapping)
            ]
        else:
            raise WorkspaceError("workspace metadata has an invalid shape")
        loaded: dict[str, dict[str, Any]] = {}
        for key, value in candidates:
            if not isinstance(value, Mapping):
                raise WorkspaceError("workspace metadata contains a non-object record")
            record = dict(value)
            record_id = str(record.get("id", key) or "").strip()
            if not record_id or not safe_segment(record_id):
                raise WorkspaceError("workspace metadata contains an unsafe id")
            if str(key) not in {"", record_id}:
                raise WorkspaceError("workspace metadata key does not match its id")
            expected = str((self.root / record_id).absolute().resolve())
            actual = str(
                Path(str(record.get("path", ""))).expanduser().absolute().resolve()
            )
            if actual != expected:
                raise WorkspaceError(
                    "workspace metadata path is outside its managed root"
                )
            state = str(record.get("state", "ready"))
            if state not in {"ready", "active", "deleted"}:
                raise WorkspaceError("workspace metadata contains an invalid state")
            record["id"] = record_id
            record["path"] = expected
            record["state"] = state
            record["metadata"] = dict(record.get("metadata", {}) or {})
            active = record.get("active_run_ids", [])
            if not isinstance(active, list):
                raise WorkspaceError(
                    "workspace metadata contains invalid active leases"
                )
            record["active_run_ids"] = [str(item) for item in active]
            loaded[record_id] = record
        self._reap_locked(loaded)
        return loaded

    def _refresh_locked(self) -> None:
        """Refresh records before a lifecycle mutation to reduce lost updates."""
        self._records = self._read_records()

    def _reap_locked(self, records: dict[str, dict[str, Any]]) -> None:
        """Expire abandoned workspace leases without trusting stale process state."""
        now = time.time()
        for record in records.values():
            expiry = float(record.get("lease_expires_at", 0.0) or 0.0)
            if record.get("state") == "active" and expiry and expiry < now:
                record["active_run_ids"] = []
                record["state"] = "ready"
                record["updated_at"] = now
                record.pop("lease_expires_at", None)
            elif record.get("state") == "active" and not record.get("active_run_ids"):
                record["state"] = "ready"
                record.pop("lease_expires_at", None)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Serialize metadata replacement across manager instances."""
        lock_path = self.metadata_path.with_name(self.metadata_path.name + ".lock")
        deadline = time.monotonic() + 10.0
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    stale = time.time() - lock_path.stat().st_mtime > 30.0
                except OSError:
                    stale = False
                if stale:
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise WorkspaceError("workspace metadata lock timed out") from None
                time.sleep(0.01)
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii", errors="ignore"))
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                lock_path.unlink()
            except OSError:
                pass

    def _save(self) -> None:
        payload = {
            "schema_version": 1,
            "updated_at": time.time(),
            "workspaces": {
                key: dict(redact_secrets(value)) for key, value in self._records.items()
            },
        }
        temporary = self.metadata_path.with_name(
            f"{self.metadata_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with self._file_lock():
                self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                os.replace(temporary, self.metadata_path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise WorkspaceError(
                f"workspace metadata could not be saved: {exc}"
            ) from exc

    def _id(self, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate or not safe_segment(candidate):
            raise InvalidRequestError("workspace id must be one safe path segment")
        return candidate

    def _target(self, workspace_id: str) -> Path:
        target = (self.root / workspace_id).absolute()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError("workspace path escapes its managed root") from exc
        if target.is_symlink():
            raise WorkspaceError("managed workspace path must not be a symbolic link")
        return target

    def _source_is_safe(self, source_root: Path) -> None:
        """Reject symlinks and unbounded or secret-bearing source trees."""
        if source_root.is_symlink() or not source_root.is_dir():
            raise WorkspaceError("workspace source must be a real directory")
        file_count = 0
        byte_count = 0
        for directory, dirnames, filenames in os.walk(source_root, followlinks=False):
            directory_path = Path(directory)
            for name in [*dirnames, *filenames]:
                candidate = directory_path / name
                if candidate.is_symlink():
                    raise WorkspaceError("workspace source must not contain symlinks")
            for name in filenames:
                candidate = directory_path / name
                try:
                    size = candidate.stat().st_size
                except OSError as exc:
                    raise WorkspaceError(
                        "workspace source could not be inspected"
                    ) from exc
                file_count += 1
                byte_count += size
                if file_count > 100_000 or byte_count > 1024 * 1024 * 1024:
                    raise WorkspaceError("workspace source exceeds the copy budget")

    @staticmethod
    def _copy_ignore(directory: str, names: list[str]) -> set[str]:
        """Exclude VCS/build state and common credential files from workspace copies."""
        ignored: set[str] = set()
        sensitive_exact = {
            ".env",
            ".env.local",
            ".env.production",
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "credentials",
            "secrets",
        }
        for name in names:
            lower = name.casefold()
            if (
                name in {".git", "logs", "__pycache__", ".venv"}
                or lower in sensitive_exact
                or lower.startswith(".env.")
                or Path(lower).suffix in {".pem", ".key", ".p12", ".pfx"}
            ):
                ignored.add(name)
        return ignored

    def create(
        self,
        name: str = "",
        *,
        workspace_id: str = "",
        source_path: str | Path | None = None,
        repo_path: str | Path | None = None,
        path: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Workspace:
        """Create and persist a managed workspace, optionally copying a source tree."""
        selected_id = self._id(workspace_id or new_workspace_id())
        source = source_path if source_path is not None else repo_path
        if source is None:
            source = path
        with self._lock:
            self._refresh_locked()
            if (
                selected_id in self._records
                and self._records[selected_id].get("state") != "deleted"
            ):
                raise WorkspaceError(f"workspace already exists: {selected_id}")
            target = self._target(selected_id)
            if target.exists():
                raise WorkspaceError(
                    f"workspace directory already exists: {selected_id}"
                )
            source_text = str(Path(source).expanduser().resolve()) if source else ""
            if source:
                source_root = Path(source_text)
                if not source_root.is_dir():
                    raise WorkspaceError(
                        f"workspace source is not a directory: {source_root}"
                    )
                try:
                    source_root.relative_to(self.root)
                except ValueError:
                    pass
                else:
                    raise WorkspaceError(
                        "workspace source must be outside the managed root"
                    )
                try:
                    target.relative_to(source_root)
                except ValueError:
                    pass
                else:
                    raise WorkspaceError(
                        "workspace target must be outside the source tree"
                    )
                self._source_is_safe(source_root)
                shutil.copytree(
                    source_root,
                    target,
                    symlinks=False,
                    ignore=self._copy_ignore,
                )
            else:
                target.mkdir(parents=True, exist_ok=False)
            now = time.time()
            record = {
                "id": selected_id,
                "name": str(name or selected_id),
                "path": str(target),
                "state": "ready",
                "created_at": now,
                "updated_at": now,
                "deleted_at": 0.0,
                "source_path": source_text,
                "metadata": dict(redact_secrets(metadata or {})),
            }
            self._records[selected_id] = record
            self._save()
            return Workspace.from_record(record, manager=self)

    def list(self, *, include_deleted: bool = False) -> list[Workspace]:
        """List managed workspaces in deterministic creation order."""
        with self._lock:
            self._refresh_locked()
            values = [
                value
                for value in self._records.values()
                if include_deleted or value.get("state") != "deleted"
            ]
            values.sort(
                key=lambda item: (
                    float(item.get("created_at", 0.0)),
                    str(item.get("id", "")),
                )
            )
            return [Workspace.from_record(value, manager=self) for value in values]

    def get(self, workspace_id: str, *, include_deleted: bool = False) -> Workspace:
        """Return one managed workspace or raise a typed not-found error."""
        selected_workspace = self._id(workspace_id)
        with self._lock:
            self._refresh_locked()
            record = self._records.get(selected_workspace)
            if record is None or (
                record.get("state") == "deleted" and not include_deleted
            ):
                raise WorkspaceNotFoundError(
                    f"workspace not found: {selected_workspace}"
                )
            return Workspace.from_record(record, manager=self)

    def delete(self, workspace_id: str) -> Workspace:
        """Delete an inactive workspace while retaining a lifecycle tombstone."""
        selected_workspace = self._id(workspace_id)
        with self._lock:
            self._refresh_locked()
            record = self._records.get(selected_workspace)
            if record is None or record.get("state") == "deleted":
                raise WorkspaceNotFoundError(
                    f"workspace not found: {selected_workspace}"
                )
            active_ids = list(record.get("active_run_ids", []) or [])
            if active_ids or record.get("state") == "active":
                raise WorkspaceActiveError(
                    f"workspace is active and cannot be deleted: {workspace_id}"
                )
            target = self._target(selected_workspace)
            if target.exists():
                if target.is_symlink():
                    raise WorkspaceError("refusing to delete a symbolic-link workspace")
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    raise WorkspaceError(
                        f"workspace could not be deleted: {exc}"
                    ) from exc
            now = time.time()
            updated = dict(record)
            updated["state"] = "deleted"
            updated["updated_at"] = now
            updated["deleted_at"] = now
            self._records[selected_workspace] = updated
            self._save()
            return Workspace.from_record(updated, manager=self)

    def claim(self, workspace_id: str, run_id: str) -> Workspace:
        """Mark a workspace active for one run."""
        selected_workspace = self._id(workspace_id)
        selected_run = self._id(run_id)
        with self._lock:
            self.get(selected_workspace)
            record = self._records.get(selected_workspace)
            if record is None:
                raise WorkspaceNotFoundError(
                    f"workspace not found: {selected_workspace}"
                )
            active = set(str(item) for item in record.get("active_run_ids", []) or [])
            active.add(selected_run)
            record["active_run_ids"] = sorted(active)
            record["state"] = "active"
            record["updated_at"] = time.time()
            record["lease_expires_at"] = time.time() + self.lease_seconds
            self._save()
            return Workspace.from_record(record, manager=self)

    def release(self, workspace_id: str, run_id: str) -> Workspace:
        """Release one run's active claim and return the workspace to ready."""
        selected_workspace = self._id(workspace_id)
        selected_run = self._id(run_id)
        with self._lock:
            self._refresh_locked()
            record = self._records.get(selected_workspace)
            if record is None:
                raise WorkspaceNotFoundError(
                    f"workspace not found: {selected_workspace}"
                )
            if record.get("state") == "deleted":
                return Workspace.from_record(record, manager=self)
            active = set(str(item) for item in record.get("active_run_ids", []) or [])
            active.discard(selected_run)
            record["active_run_ids"] = sorted(active)
            record["state"] = "ready" if not active else "active"
            record["updated_at"] = time.time()
            if not active:
                record.pop("lease_expires_at", None)
            else:
                record["lease_expires_at"] = time.time() + self.lease_seconds
            self._save()
            return Workspace.from_record(record, manager=self)

    def create_workspace(self, *args: Any, **kwargs: Any) -> Workspace:
        """Create a workspace under the transport-friendly long name."""
        return self.create(*args, **kwargs)

    def list_workspaces(self, *, include_deleted: bool = False) -> list[Workspace]:
        """List workspaces under the transport-friendly long name."""
        return self.list(include_deleted=include_deleted)

    def get_workspace(
        self, workspace_id: str, *, include_deleted: bool = False
    ) -> Workspace:
        """Get a workspace under the transport-friendly long name."""
        return self.get(workspace_id, include_deleted=include_deleted)

    def delete_workspace(self, workspace_id: str) -> Workspace:
        """Delete a workspace under the transport-friendly long name."""
        return self.delete(workspace_id)

    def find(self, workspace_id: str) -> Optional[Workspace]:
        """Return a workspace when present without raising for a missing id."""
        try:
            return self.get(workspace_id)
        except WorkspaceNotFoundError:
            return None


WorkspaceManager = LocalWorkspaceManager


class RemoteWorkspaceManager:
    """Manage server-owned workspaces through the remote SDK protocol."""

    def __init__(
        self,
        base_url: str = "",
        *,
        transport: Any = None,
        token: str = "",
        timeout: float = 30.0,
    ) -> None:
        """Create a manager backed by a RemoteTransport or server base URL."""
        if transport is None:
            from .remote import RemoteTransport

            transport = RemoteTransport(base_url, token=token, timeout=timeout)
        self.transport = transport

    def create(
        self,
        name: str = "",
        *,
        workspace_id: str = "",
        source_path: str | Path | None = None,
        repo_path: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Workspace:
        """Create a server-managed workspace and return its client handle."""
        source = source_path if source_path is not None else repo_path
        record = self.transport.create_workspace(
            name=name,
            workspace_id=workspace_id,
            source_path=str(source) if source else "",
            metadata=metadata,
        )
        return Workspace.from_record(record, manager=self)

    def list(self, *, include_deleted: bool = False) -> list[Workspace]:
        """List server-managed workspace records as local handles."""
        records = self.transport.list_workspaces(include_deleted=include_deleted)
        return [Workspace.from_record(item, manager=self) for item in records]

    def get(self, workspace_id: str, *, include_deleted: bool = False) -> Workspace:
        """Fetch one server-managed workspace record."""
        record = self.transport.get_workspace(
            workspace_id, include_deleted=include_deleted
        )
        return Workspace.from_record(record, manager=self)

    def delete(self, workspace_id: str) -> Workspace:
        """Delete an inactive server-managed workspace."""
        record = self.transport.delete_workspace(workspace_id)
        return Workspace.from_record(record, manager=self)

    def find(self, workspace_id: str) -> Optional[Workspace]:
        """Return a remote workspace when it exists."""
        try:
            return self.get(workspace_id)
        except WorkspaceNotFoundError:
            return None
