"""Workspace journal and safe mutation helpers for daily coding runs."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

_IGNORED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "logs",
    ".pypirc",
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "id_rsa",
    "id_ed25519",
    "credentials*",
    "secrets*",
}


class WorkspaceJournal:
    """Track agent-owned changes and produce a reversible diff."""

    def __init__(
        self,
        repo_path: Path | str,
        snapshot_path: Path | str | None = None,
        protected_paths: Sequence[str] = (),
    ) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.snapshot_path = (
            Path(snapshot_path).expanduser().resolve() if snapshot_path else None
        )
        self.protected_paths = tuple(
            str(item).replace("\\", "/") for item in protected_paths or ()
        )
        self._recorded: Dict[str, str] = {}
        self._originals: Dict[str, Optional[bytes]] = {}
        self._created: set[str] = set()

    def ensure_snapshot(self) -> Optional[Path]:
        """Create a read-only comparison snapshot outside the live source tree."""
        if self.snapshot_path is None or self.snapshot_path.is_dir():
            return self.snapshot_path
        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            ignored = set(_IGNORED_PARTS)
            try:
                relative_destination = self.snapshot_path.relative_to(self.repo_path)
                if relative_destination.parts:
                    ignored.add(relative_destination.parts[0])
            except ValueError:
                pass
            shutil.copytree(
                self.repo_path,
                self.snapshot_path,
                ignore=shutil.ignore_patterns(*ignored),
                symlinks=True,
            )
            return self.snapshot_path
        except OSError:
            return None

    def safe_path(self, relative: str) -> Optional[Path]:
        """Resolve a repository-relative path without allowing traversal."""
        text = str(relative or "").replace("\\", "/").strip().strip("`\"'")
        if not text or "\x00" in text:
            return None
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
        try:
            unresolved = self.repo_path / candidate
            resolved = unresolved.resolve()
            resolved.relative_to(self.repo_path)
            if unresolved.is_symlink():
                raise PermissionError(f"symbolic-link path refused: {text}")
            relative = resolved.relative_to(self.repo_path)
            if any(
                self._path_match(part, pattern)
                for part in relative.parts
                for pattern in _IGNORED_PARTS
            ):
                raise PermissionError(f"secret or harness path refused: {text}")
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved

    def relative(self, path: Path) -> str:
        """Return a normalized repository-relative path."""
        try:
            return path.resolve().relative_to(self.repo_path).as_posix()
        except (OSError, ValueError):
            return str(path).replace("\\", "/")

    def is_protected(self, relative: str) -> bool:
        """Return whether a path matches configured protected patterns."""
        value = self.relative_path(relative)
        return any(self._path_match(value, pattern) for pattern in self.protected_paths)

    def read(self, relative: str, max_bytes: int = 200_000) -> str:
        """Read a safe text file with a hard size cap."""
        path = self.safe_path(relative)
        if path is None or not path.is_file():
            raise FileNotFoundError(relative)
        if path.stat().st_size > int(max_bytes):
            raise ValueError(f"file exceeds {int(max_bytes)} bytes: {relative}")
        return path.read_text(encoding="utf-8", errors="replace")

    def write(self, relative: str, content: str) -> str:
        """Write a safe workspace file after recording its original bytes."""
        path = self.safe_path(relative)
        if path is None:
            raise ValueError("path escapes repository")
        norm = self.relative_path(relative)
        if self.is_protected(norm):
            raise PermissionError(f"protected path: {norm}")
        self._remember(norm, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        self._recorded[norm] = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        return norm

    def edit(self, relative: str, old_string: str, new_string: str) -> str:
        """Replace the first exact block in a safe workspace file."""
        if not old_string:
            raise ValueError("old_string must not be empty")
        path = self.safe_path(relative)
        if path is None or not path.is_file():
            raise FileNotFoundError(relative)
        norm = self.relative_path(relative)
        if self.is_protected(norm):
            raise PermissionError(f"protected path: {norm}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if old_string not in text:
            raise ValueError("old_string not found verbatim")
        self._remember(norm, path)
        updated = text.replace(old_string, str(new_string), 1)
        path.write_text(updated, encoding="utf-8")
        self._recorded[norm] = hashlib.sha256(updated.encode("utf-8")).hexdigest()
        return norm

    def record_existing_change(self, relative: str) -> str:
        """Record a change made by an external shell/process tool."""
        norm = self.relative_path(relative)
        self._remember(norm, self.safe_path(norm))
        self._recorded[norm] = "external"
        return norm

    def changed_files(self) -> List[str]:
        """Return files differing from the snapshot or recorded by a tool."""
        found = set(self._recorded)
        snapshot = self.snapshot_path
        if snapshot is not None and snapshot.is_dir():
            for current in self._walk(self.repo_path):
                rel = self.relative(current)
                old = snapshot / Path(rel)
                if not old.is_file() or _digest(current) != _digest(old):
                    found.add(rel)
            for old in self._walk(snapshot):
                rel = self.relative(old)
                if not (self.repo_path / Path(rel)).is_file():
                    found.add(rel)
        return sorted(found)

    def diff(self, max_chars: int = 100_000) -> str:
        """Return a unified diff against the snapshot."""
        snapshot = self.snapshot_path
        if snapshot is None or not snapshot.is_dir():
            return ""
        pieces: List[str] = []
        for rel in self.changed_files():
            current = self.repo_path / Path(rel)
            original = snapshot / Path(rel)
            try:
                before = (
                    original.read_text(encoding="utf-8", errors="replace")
                    if original.is_file()
                    else ""
                )
                after = (
                    current.read_text(encoding="utf-8", errors="replace")
                    if current.is_file()
                    else ""
                )
            except OSError:
                continue
            pieces.extend(
                difflib.unified_diff(
                    before.splitlines(True),
                    after.splitlines(True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            if sum(len(piece) for piece in pieces) >= int(max_chars):
                break
        return "".join(pieces)[: int(max_chars)]

    def git_status(self) -> str:
        """Return read-only git status for the workspace."""
        return self._git("status", "--short")

    def git_diff(self, max_chars: int = 100_000) -> str:
        """Return read-only git diff for the workspace."""
        return self._git("diff", "--no-ext-diff")[: int(max_chars)]

    def blocked_external_changes(self) -> List[str]:
        """Return changed protected or symbolic-link paths."""
        blocked = []
        for rel in self.changed_files():
            if self.is_protected(rel) or self._builtin_protected(rel):
                blocked.append(rel)
                continue
            path = self.safe_path(rel)
            if path is not None and path.is_symlink():
                blocked.append(rel)
        return sorted(set(blocked))

    def restore_snapshot_files(self, paths: Sequence[str]) -> List[str]:
        """Restore selected files from the comparison snapshot."""
        snapshot = self.snapshot_path
        if snapshot is None or not snapshot.is_dir():
            return []
        restored: List[str] = []
        for rel in paths:
            destination = self.safe_path(rel)
            source = snapshot / Path(rel)
            if destination is None:
                continue
            try:
                if source.is_file() and not source.is_symlink():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read_bytes())
                    restored.append(rel)
                elif destination.is_file() or destination.is_symlink():
                    destination.unlink()
                    restored.append(rel)
            except OSError:
                continue
        return restored

    def restore_path(self, relative: str) -> bool:
        """Restore one path from its first recorded original."""
        rel = self.relative_path(relative)
        if rel not in self._originals:
            return False
        path = self.safe_path(rel)
        if path is None:
            return False
        try:
            original = self._originals[rel]
            if original is None:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
            return True
        except OSError:
            return False

    def restore_originals(self) -> List[str]:
        """Restore files recorded before their first mutation."""
        restored: List[str] = []
        for rel, original in list(self._originals.items()):
            path = self.safe_path(rel)
            if path is None:
                continue
            try:
                if original is None:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                        restored.append(rel)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(original)
                    restored.append(rel)
            except OSError:
                continue
        return sorted(restored)

    def _remember(self, rel: str, path: Optional[Path]) -> None:
        if rel in self._originals:
            return
        try:
            self._originals[rel] = (
                path.read_bytes() if path is not None and path.is_file() else None
            )
        except OSError:
            self._originals[rel] = None
        if path is not None and not path.exists():
            self._created.add(rel)

    def _git(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"git unavailable: {exc}"
        output = result.stdout or ""
        if result.returncode:
            output += result.stderr or ""
        return output.strip()

    def _builtin_protected(self, relative: str) -> bool:
        parts = self.relative_path(relative).replace("\\", "/").lower().split("/")
        if any(
            fnmatch.fnmatch(part, pattern)
            for part in parts
            for pattern in _IGNORED_PARTS
        ):
            return True
        return any(part in {".git", ".hg", ".svn", ".bzr", "_darcs"} for part in parts)

    def _walk(self, root: Path) -> Iterable[Path]:
        if not root.is_dir():
            return []
        result: List[Path] = []
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if self._builtin_protected(relative.as_posix()):
                continue
            result.append(path)
        return result

    def relative_path(self, value: str) -> str:
        """Normalize a path string without resolving it against disk."""
        text = str(value or "").replace("\\", "/").strip()
        path = Path(text)
        if path.is_absolute():
            try:
                return self.relative(path)
            except (OSError, ValueError):
                return text
        return path.as_posix()

    @staticmethod
    def _path_match(value: str, pattern: str) -> bool:
        from fnmatch import fnmatch

        return fnmatch(value, pattern)


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
