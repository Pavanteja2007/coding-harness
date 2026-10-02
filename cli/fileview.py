"""Read-only terminal projections for files, diffs, context, and checkpoints."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from shared.security import redact_text

__all__ = [
    "ATTRIBUTION_FIELDS",
    "DIAGNOSTIC_PROVENANCE",
    "UNDO_SCOPE_ALIASES",
    "UNDO_VERBS",
    "DiffHunk",
    "FileChange",
    "atomic_write_bytes",
    "build_file_projection",
    "build_file_tree",
    "capture_undo_receipt",
    "checkpoint_records",
    "commit_staged_undo_for_prompt",
    "commit_undo",
    "compare_checkpoint",
    "content_hash",
    "context_snapshot",
    "current_diff_text",
    "diagnostic_link",
    "diagnostic_provenance",
    "diagnostic_rows",
    "diff_summary",
    "discard_undo",
    "file_change_verified",
    "file_picker_rows",
    "lsp_diagnostics",
    "lsp_state_report",
    "normalize_diagnostic",
    "open_diff_file",
    "open_diff_line",
    "parse_diff_target",
    "path_exists",
    "read_git_status",
    "read_repository_files",
    "redo_apply",
    "relevant_file_rows",
    "relevant_projection",
    "render_undo_receipt",
    "repo_root",
    "restore_checkpoint",
    "restore_selection_preflight",
    "safe_repo_path",
    "stage_undo",
    "staged_undo_state",
    "symbol_candidates",
    "undo_command",
    "undo_live_refusal",
    "undo_plan",
    "undo_preflight",
    "undo_scope_for",
    "undo_scope_words",
    "undo_scopes",
    "undo_turn_rows",
]

#: The five attributions the product promises for EVERY file change. This
#: table is the machine-checkable form of that promise: a renderer that shows
#: a path without resolving every key here has not identified the change.
ATTRIBUTION_FIELDS: Tuple[str, ...] = (
    "actor",
    "reason",
    "verified",
    "undoable",
    "checkpoint_ids",
)

#: Where a diagnostic came from. A journal row (something a run recorded) and
#: a live language-server row (something a tool is saying right now) are
#: DIFFERENT claims, and the panel must not present them as one list of
#: interchangeable strings.
DIAGNOSTIC_PROVENANCE: Tuple[str, ...] = ("journal", "lsp")

#: The middle restore granularity, and the product default (AGT-09). Spelled
#: here rather than read from the memory layer so a missing import degrades to a
#: real behaviour instead of an ``ImportError`` inside a command handler.
DEFAULT_UNDO_SCOPE = "files"

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        "site-packages",
        "target",
        "logs",
        ".harness",
    }
)
_DIFF_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_MUTATION_TOOLS = frozenset(
    {"edit", "write", "apply_patch", "patch", "rename", "delete", "rm", "remove"}
)

# Editor handoff: detached launches are bounded so a crashed editor cannot
# hold the session thread open.
_DETACHED_TIMEOUT = 20.0


def _platform_default() -> List[str]:
    """Platform-specific editor program names."""
    if os.name == "nt" or platform.system() == "Windows":
        return ["code"]
    return ["vi"]


@dataclass(frozen=True)
class DiffHunk:
    """One parsed unified-diff hunk with its display lines and line numbers."""

    index: int
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: Tuple[str, ...] = ()
    line_numbers: Tuple[Optional[int], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly hunk record."""
        return {
            "index": self.index,
            "header": self.header,
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "lines": list(self.lines),
            "line_numbers": list(self.line_numbers),
        }


@dataclass
class FileChange:
    """Journal- and workspace-derived facts for one changed file."""

    path: str
    status: str = "unstaged"
    summary: str = ""
    kind: str = "modified"
    actor: str = "unknown"
    reason: str = ""
    verified: bool = False
    verification_state: str = "not_run"
    undoable: bool = False
    checkpoint_ids: Tuple[str, ...] = ()
    staged: bool = False
    unstaged: bool = False
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    large: bool = False
    truncated: bool = False
    hunks: Tuple[DiffHunk, ...] = ()
    git_status: str = ""
    source: str = "journal"
    #: True when the RUN's own record — its journal `changed_files`, its
    #: per-file metadata, or its `pristine`/`work` artifacts — names this
    #: file. This, and not `source`, decides the ACTOR: `source` says where
    #: the patch TEXT came from (and a run-evidenced file's patch can come
    #: from `git`), while this says whether the run is the cause.
    run_evidenced: bool = False

    def as_dict(self) -> Dict[str, Any]:
        """Return a serializable per-file change record."""
        return {
            "path": self.path,
            "status": self.status,
            "summary": self.summary,
            "kind": self.kind,
            "actor": self.actor,
            "reason": self.reason,
            "verified": self.verified,
            "verification_state": self.verification_state,
            "undoable": self.undoable,
            "undo": self.undoable,
            "checkpoint_ids": list(self.checkpoint_ids),
            "staged": self.staged,
            "unstaged": self.unstaged,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
            "large": self.large,
            "truncated": self.truncated,
            "hunks": [hunk.as_dict() for hunk in self.hunks],
            "git_status": self.git_status,
            "source": self.source,
            "run_evidenced": self.run_evidenced,
        }


def _bounded_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def file_change_verified(
    run_state: str,
    claimed: Any = None,
    *,
    truncated: bool = False,
) -> bool:
    """Return whether a file change may be presented as verified.

    This is the ONE authority, and it fails CLOSED in both directions.

    A per-file ``verified`` claim in the journal can only ever CONFIRM a file
    the run already proved; it can never promote one. The defect this replaces
    is the R2-G45 class in a new place: the projection used

    ``bool(meta.get("verified", run_state == "verified" and not truncated))``

    so a journal row claiming ``verified: true`` rendered a file as verified
    even while the run's own state was ``failed`` — and the guard below it
    (``if not verified and run_state in {...}: verified = False``) could only
    ever assign ``False`` to something already ``False``. A gate that cannot
    fail is worse than no gate, because it reads as one.

    Assumes ``run_state`` is a ``cli.runview.verification_state`` value
    (``verified`` / ``unverified`` / ``failed`` / ``flaky`` / ``error`` /
    ``not_run`` / ``unknown``). Anything that is not the exact string
    ``"verified"`` denies.
    """
    if str(run_state or "").strip().lower() != "verified":
        return False
    if truncated:
        # A bounded view cannot vouch for the lines it dropped.
        return False
    if claimed is None:
        return True
    if isinstance(claimed, str):
        return claimed.strip().lower() in {"verified", "true", "yes", "1"}
    return bool(claimed)


def _relative(value: Any) -> str:
    """Return a safe POSIX repository-relative path or an empty string."""
    text = str(value or "").replace("\\", "/").strip()
    if not text or "\x00" in text or text.startswith(("/", "\\")):
        return ""
    if re.match(r"^[A-Za-z]:", text):
        return ""
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def _root(value: Any) -> Optional[Path]:
    try:
        root = Path(str(value or "")).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return root if root.is_dir() else None


def _safe_path(root: Optional[Path], value: Any) -> Optional[Path]:
    relative = _relative(value)
    if root is None or not relative:
        return None
    candidate = root.joinpath(*relative.split("/"))
    current = root
    try:
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                return None
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _read_bytes(path: Optional[Path], limit: int = 4 * 1024 * 1024) -> Optional[bytes]:
    if path is None or not path.is_file() or path.is_symlink():
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(max(0, int(limit)) + 1)
    except (OSError, ValueError):
        return None
    if len(data) > max(0, int(limit)):
        return data[: max(0, int(limit))]
    return data


def _safe_git_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _git(root: Path, *args: str, timeout: float = 4.0) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=max(0.2, float(timeout)),
            check=False,
            env=_safe_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _walk_repository_files(root: Path, cap: int) -> List[str]:
    values: List[str] = []
    try:
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in _SKIP_DIRS and not directory.startswith(".")
            )
            for name in sorted(files):
                relative = _relative(
                    (Path(current) / name).relative_to(root).as_posix()
                )
                if relative and _safe_path(root, relative) is not None:
                    values.append(relative)
                    if len(values) >= max(1, int(cap)):
                        return sorted(values)
    except OSError:
        return []
    return sorted(set(values))[: max(1, int(cap))]


def _repository_file_count(root: Path, threshold: int = 300) -> int:
    count = 0
    try:
        for _current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [
                directory
                for directory in directories
                if directory not in _SKIP_DIRS and not directory.startswith(".")
            ]
            count += len(files)
            if count >= threshold:
                return count
    except OSError:
        return count
    return count


def read_repository_files(
    repo: Any, cap: int = 4000, *, prefer_git: bool = True
) -> List[str]:
    """Return bounded repository-relative files without following links."""
    root = _root(repo)
    if root is None:
        return []
    if not prefer_git or _bounded_int(cap, 4000) <= 64:
        return _walk_repository_files(root, _bounded_int(cap, 4000))
    values: List[str] = []
    if not (root / ".git").exists():
        try:
            for current, directories, files in os.walk(root, followlinks=False):
                directories[:] = sorted(
                    d
                    for d in directories
                    if d not in _SKIP_DIRS and not d.startswith(".")
                )
                for name in sorted(files):
                    relative = _relative(
                        (Path(current) / name).relative_to(root).as_posix()
                    )
                    if relative and _safe_path(root, relative) is not None:
                        values.append(relative)
        except OSError:
            return []
        return sorted(set(values))[: max(1, _bounded_int(cap, 4000))]
    try:
        listing = _git(
            root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
        )
    except Exception:
        listing = None
    if listing:
        for value in listing.split("\0"):
            relative = _relative(value)
            if relative and _safe_path(root, relative) is not None:
                values.append(relative)
    if not values:
        try:
            for current, directories, files in os.walk(root, followlinks=False):
                directories[:] = sorted(
                    d
                    for d in directories
                    if d not in _SKIP_DIRS and not d.startswith(".")
                )
                for name in sorted(files):
                    relative = _relative(
                        (Path(current) / name).relative_to(root).as_posix()
                    )
                    if relative and _safe_path(root, relative) is not None:
                        values.append(relative)
        except OSError:
            return []
    return sorted(set(values))[: max(1, _bounded_int(cap, 4000))]


def read_git_status(repo: Any) -> Dict[str, Dict[str, Any]]:
    """Read porcelain v1 status and return safe per-file staging facts."""
    root = _root(repo)
    if root is None or not (root / ".git").exists():
        return {}
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if raw is None:
        return {}
    tokens = raw.split("\0")
    result: Dict[str, Dict[str, Any]] = {}
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record or len(record) < 3:
            continue
        code = record[:2]
        path = _relative(record[3:])
        if code[0] in {"R", "C"} and index < len(tokens):
            index += 1
        if not path or (_safe_path(root, path) is None and not (root / path).exists()):
            continue
        staged = code[0] not in {" ", "?"}
        unstaged = code[1] not in {" ", "?"}
        if code == "??":
            staged = False
            unstaged = True
        result[path] = {
            "code": code,
            "staged": staged,
            "unstaged": unstaged,
            "status": code,
        }
    return result


def _git_changed_paths(root: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if root is None:
        return {}
    status = read_git_status(root)
    if not status and not (root / ".git").exists():
        return status
    for args in (
        ("diff", "--name-only", "-z", "HEAD"),
        ("diff", "--name-only", "-z", "--cached"),
    ):
        output = _git(root, *args)
        if output is None:
            continue
        for value in output.split("\0"):
            path = _relative(value)
            if path and path not in status:
                status[path] = {
                    "code": " M",
                    "staged": args[-1] == "--cached",
                    "unstaged": args[-1] != "--cached",
                    "status": " M",
                }
    return status


def _text_lines(path: Optional[Path]) -> Tuple[Optional[List[str]], bool, int]:
    data = _read_bytes(path)
    if data is None:
        return None, False, 0
    if b"\x00" in data:
        return None, True, len(data)
    try:
        text = data.decode("utf-8", errors="replace")
    except (UnicodeError, ValueError):
        return None, True, len(data)
    return text.splitlines(keepends=True), False, len(data)


def _diff_text(
    old_path: Optional[Path], new_path: Optional[Path], relative: str
) -> str:
    old_lines, old_binary, _old_size = _text_lines(old_path)
    new_lines, new_binary, _new_size = _text_lines(new_path)
    if old_binary or new_binary:
        return f"Binary files a/{relative} and b/{relative} differ\n"
    if old_lines is None:
        old_lines = []
    if new_lines is None:
        new_lines = []
    if old_lines == new_lines:
        return ""
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            n=3,
        )
    )


def _sanitize_diff(text: str) -> str:
    """Return diff text stripped of escapes and secrets, before it is parsed.

    This is the parse boundary every diff line crosses, which is why it is the
    right place to sanitise: `DiffHunk.lines` is the ONE list the diff modal,
    the per-file cursor, the review lines and the changed-file rows all render,
    so a line sanitised here cannot reach any of them raw. Sanitising before
    parsing (rather than after) is deliberate — it means the `+`/`-`/`@@` a
    line is CLASSIFIED by is the one the user SEES, instead of a prefix hiding
    behind an escape the classifier never saw.

    ``markup=False`` on a RichLog protects rich markup and nothing else; it is
    not a sanitiser. Never raises.
    """
    from cli import ui as _ui

    return _ui.sanitize_text(text)


def _parse_hunks(diff: str) -> Tuple[List[DiffHunk], int, int, bool]:
    lines = _sanitize_diff(str(diff or "")).splitlines()
    hunks: List[DiffHunk] = []
    additions = 0
    deletions = 0
    current: Optional[Dict[str, Any]] = None
    for line in lines:
        match = _DIFF_HEADER.match(line)
        if match:
            if current is not None:
                hunks.append(
                    DiffHunk(
                        index=current["index"],
                        header=current["header"],
                        old_start=current["old_start"],
                        old_count=current["old_count"],
                        new_start=current["new_start"],
                        new_count=current["new_count"],
                        lines=tuple(current["lines"]),
                        line_numbers=tuple(current["line_numbers"]),
                    )
                )
            current = {
                "index": len(hunks) + 1,
                "header": line,
                "old_start": int(match.group("old_start") or 0),
                "old_count": int(match.group("old_count") or 1),
                "new_start": int(match.group("new_start") or 0),
                "new_count": int(match.group("new_count") or 1),
                "lines": [],
                "line_numbers": [],
                "old_line": int(match.group("old_start") or 0),
                "new_line": int(match.group("new_start") or 0),
            }
            continue
        if line.startswith("+++") or line.startswith("---") or line.startswith("diff "):
            continue
        if current is None:
            continue
        current["lines"].append(line)
        if line.startswith("+"):
            current["line_numbers"].append(current["new_line"])
            current["new_line"] += 1
            additions += 1
        elif line.startswith("-"):
            current["line_numbers"].append(current["old_line"])
            current["old_line"] += 1
            deletions += 1
        else:
            current["line_numbers"].append(current["new_line"])
            current["old_line"] += 1
            current["new_line"] += 1
    if current is not None:
        hunks.append(
            DiffHunk(
                index=current["index"],
                header=current["header"],
                old_start=current["old_start"],
                old_count=current["old_count"],
                new_start=current["new_start"],
                new_count=current["new_count"],
                lines=tuple(current["lines"]),
                line_numbers=tuple(current["line_numbers"]),
            )
        )
    return hunks, additions, deletions, False


def _diff_for_path(
    root: Optional[Path],
    task_dir: Optional[Path],
    relative: str,
    git_status: Mapping[str, Any],
) -> Tuple[str, str]:
    if root is not None:
        pristine = task_dir / "pristine" if task_dir is not None else None
        work = task_dir / "work" if task_dir is not None else None
        if (
            work is not None
            and work.is_dir()
            and pristine is not None
            and pristine.is_dir()
        ):
            old = _safe_path(pristine, relative)
            new = _safe_path(work, relative)
            if (
                old is not None
                or new is not None
                or (pristine / relative).exists()
                or (work / relative).exists()
            ):
                return _diff_text(old, new, relative), "workspace"
        if task_dir is not None and pristine is not None and pristine.is_dir():
            old = _safe_path(pristine, relative)
            new = _safe_path(root, relative)
            return _diff_text(old, new, relative), "workspace"
    if root is not None and relative in git_status:
        output = _git(
            root, "diff", "--no-ext-diff", "--unified=3", "HEAD", "--", relative
        )
        if output is None:
            output = _git(
                root, "diff", "--no-ext-diff", "--unified=3", "--cached", "--", relative
            )
        return output or "", "git"
    return "", "journal"


def _file_kind(old_path: Optional[Path], new_path: Optional[Path], status: str) -> str:
    old_exists = bool(old_path and old_path.exists())
    new_exists = bool(new_path and new_path.exists())
    if not old_exists and new_exists:
        return "added"
    if old_exists and not new_exists:
        return "deleted"
    if "A" in status or "??" in status:
        return "added"
    if "D" in status:
        return "deleted"
    return "modified"


def _metadata_map(snapshot: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    values = snapshot.get("file_changes")
    if not isinstance(values, list):
        values = []
    for value in values:
        if isinstance(value, str):
            result[_relative(value)] = {}
        elif isinstance(value, Mapping):
            path = _relative(
                value.get("path") or value.get("file") or value.get("target")
            )
            if path:
                result[path] = dict(value)
    return result


def _paths_from_context(value: Any) -> List[str]:
    paths: List[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"path", "file", "relative_path"}:
                normalized = _relative(item)
                if normalized:
                    paths.append(normalized)
            elif key in {
                "files",
                "relevant_files",
                "selected_files",
                "citations",
                "sources",
                "source_references",
            }:
                paths.extend(_paths_from_context(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, (str, Mapping)):
                normalized = _relative(item)
                if normalized:
                    paths.append(normalized)
                else:
                    paths.extend(_paths_from_context(item))
    elif value:
        normalized = _relative(value)
        if normalized:
            paths.append(normalized)
    return paths


def _checkpoint_ids(records: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for index, record in enumerate(records):
        identifier = str(
            record.get("checkpoint_id")
            or record.get("resume_token")
            or record.get("last_event_sequence")
            or index
        )
        values = record.get("agent_owned_changes") or record.get("captured_paths") or []
        if isinstance(values, Mapping):
            values = list(values.values())
        for value in values if isinstance(values, (list, tuple, set)) else []:
            path = _relative(
                value
                if not isinstance(value, Mapping)
                else value.get("path") or value.get("file")
            )
            if path and identifier not in result.setdefault(path, []):
                result[path].append(identifier)
    return result


def build_file_projection(
    task_dir: Any = None,
    repo: Any = None,
    snapshot: Optional[Mapping[str, Any]] = None,
    *,
    include_git: bool = True,
    max_files: int = 200,
    max_diff_lines: int = 80,
) -> Dict[str, Any]:
    """Build one bounded file and diff projection without mutating the repository."""
    root = _root(repo)
    directory = None
    try:
        directory = Path(str(task_dir)).expanduser().resolve() if task_dir else None
    except (OSError, RuntimeError, TypeError, ValueError):
        directory = None
    data = dict(snapshot or {})
    if not data and directory is not None:
        try:
            from cli.runview import read_live_projection

            data = read_live_projection(directory)
        except Exception:
            data = {}
    task_id = str(data.get("task_id") or (directory.name if directory else ""))
    metadata = _metadata_map(data)
    checkpoints = []
    if directory is not None:
        try:
            from cli.runview import read_checkpoints

            checkpoints = read_checkpoints(directory)
        except Exception:
            checkpoints = []
    checkpoint_files = _checkpoint_ids(checkpoints)
    git_status = _git_changed_paths(root) if include_git and root is not None else {}
    context = data.get("context") or {}
    relevant = []
    for value in _paths_from_context(context):
        if value not in relevant:
            relevant.append(value)
    if not relevant and root is not None:
        for path in _walk_repository_files(root, 12):
            if path not in relevant:
                relevant.append(path)
    changed = []
    for value in data.get("changed_files") or data.get("files") or []:
        normalized = _relative(value)
        if normalized and normalized not in changed:
            changed.append(normalized)
    # The run's OWN record of what it touched, kept separate from the git
    # view. These are two different questions and conflating them loses a
    # true fact: a file the run changed is ALSO uncommitted, and saying only
    # "uncommitted in the workspace" under-claims it. Equally, a file that
    # appears ONLY because `git status` reports it was not changed by this
    # run, and on a shared working tree that set includes every teammate's
    # edit. So: `run_evidenced` decides the ACTOR, and the diff `source`
    # decides only where the patch text came from.
    run_evidenced = set(changed) | set(metadata)
    changed.extend(path for path in metadata if path not in changed)
    changed.extend(path for path in git_status if path not in changed)
    changed = changed[: max(1, _bounded_int(max_files, 200))]
    verification_state = str(data.get("verification_state") or "not_run")
    files: List[FileChange] = []
    for path in changed:
        meta = metadata.get(path, {})
        status = git_status.get(path, {})
        code = str(status.get("code") or meta.get("git_status") or "")
        old_path = _safe_path(directory / "pristine", path) if directory else None
        new_path = (
            _safe_path(directory / "work", path)
            if directory is not None and (directory / "work").is_dir()
            else _safe_path(root, path)
        )
        diff, source = _diff_for_path(root, directory, path, git_status)
        hunks, additions, deletions, _binary_marker = _parse_hunks(diff)
        total_lines = sum(len(hunk.lines) + 1 for hunk in hunks)
        if max_diff_lines > 0 and total_lines > max_diff_lines:
            kept: List[DiffHunk] = []
            remaining = max_diff_lines
            for hunk in hunks:
                if remaining <= 0:
                    break
                values = list(hunk.lines[:remaining])
                kept.append(
                    DiffHunk(
                        index=hunk.index,
                        header=hunk.header,
                        old_start=hunk.old_start,
                        old_count=hunk.old_count,
                        new_start=hunk.new_start,
                        new_count=hunk.new_count,
                        lines=tuple(values),
                        line_numbers=hunk.line_numbers[: len(values)],
                    )
                )
                remaining -= len(values)
            hunks = kept
            truncated = True
        else:
            truncated = False
        binary = diff.startswith("Binary files")
        large = bool(total_lines > 80 or binary)
        status_name = str(meta.get("status") or code or "unstaged")
        if code == "??":
            status_name = "unstaged"
        elif code:
            status_name = code
        verified = file_change_verified(
            verification_state,
            meta.get("verified"),
            truncated=truncated,
        )
        # The state a file REPORTS is the run's own state unless the run is
        # verified. A file never carries a more confident word than the run
        # that produced it.
        file_verification_state = "verified" if verified else verification_state
        checkpoint_ids = tuple(checkpoint_files.get(path, []))
        undoable = bool(meta.get("undoable", meta.get("undo", False)))
        if not undoable and directory is not None and task_id.startswith("agent-"):
            undoable = (directory / "orig").is_dir()
        kind = str(meta.get("kind") or _file_kind(old_path, new_path, code))
        reason = str(meta.get("reason") or meta.get("why") or "")
        if not reason and source == "workspace":
            reason = "workspace comparison"
        elif not reason and source == "git":
            reason = "git status"
        elif not reason:
            reason = "journal mutation"
        actor = str(meta.get("actor") or "")
        if not actor:
            if task_id.startswith("agent-"):
                actor = "agent"
            elif path in run_evidenced:
                actor = "run"
            else:
                # Present only because `git status` reports it. On a shared
                # working tree that is every uncommitted file belonging to
                # someone else, and attributing those to the run is the same
                # class of lie as calling an unverified result verified: it
                # names a cause that did not happen.
                actor = "workspace"
        file_summary = str(meta.get("summary") or "")
        if not file_summary:
            file_summary = f"{kind} +{_bounded_int(meta.get('additions', additions))} -{_bounded_int(meta.get('deletions', deletions))}"
        file_status = meta.get("status")
        if not isinstance(file_status, str) or not file_status:
            file_status = status_name
        file_change = FileChange(
            path=path,
            status=file_status,
            summary=file_summary,
            kind=kind,
            actor=actor,
            reason=reason,
            verified=verified,
            verification_state=file_verification_state,
            undoable=undoable,
            checkpoint_ids=checkpoint_ids,
            staged=bool(status.get("staged") or meta.get("staged")),
            unstaged=bool(status.get("unstaged") or meta.get("unstaged") or not status),
            additions=_bounded_int(meta.get("additions", additions)),
            deletions=_bounded_int(meta.get("deletions", deletions)),
            binary=binary,
            large=large or bool(meta.get("large")),
            truncated=truncated or bool(meta.get("truncated")),
            hunks=tuple(hunks),
            git_status=code,
            source=str(meta.get("source") or source),
            run_evidenced=path in run_evidenced,
        )
        files.append(file_change)
    totals = {
        "files": len(files),
        "additions": sum(item.additions for item in files),
        "deletions": sum(item.deletions for item in files),
        "staged": sum(1 for item in files if item.staged),
        "unstaged": sum(1 for item in files if item.unstaged),
        "verified": sum(1 for item in files if item.verified),
        "large": sum(1 for item in files if item.large),
        "truncated": sum(1 for item in files if item.truncated),
    }
    diff_text_parts: List[str] = []
    for item in files:
        diff_text_parts.append(f"--- a/{item.path}\n+++ b/{item.path}")
        for hunk in item.hunks:
            diff_text_parts.append(hunk.header)
            diff_text_parts.extend(hunk.lines)
    diff_text = "\n".join(diff_text_parts)
    return {
        "task_id": task_id,
        "repo": str(root) if root is not None else str(repo or ""),
        "repo_name": root.name if root is not None else Path(str(repo or "")).name,
        "mode": str(data.get("mode") or data.get("projection_mode") or ""),
        "status": str(data.get("status") or ""),
        "verification_state": verification_state,
        "issue": str(data.get("issue") or data.get("request") or ""),
        "changed_files": [item.path for item in files],
        "file_changes": [item.as_dict() for item in files],
        "diff": {
            "files": [item.as_dict() for item in files],
            "summary": diff_summary([item.as_dict() for item in files]),
            "text": diff_text,
            "large": any(item.large for item in files),
            "truncated": any(item.truncated for item in files),
        },
        "relevant_files": relevant[: max(1, _bounded_int(max_files, 200))],
        "repository": {
            "path": str(root) if root is not None else str(repo or ""),
            "name": root.name if root is not None else Path(str(repo or "")).name,
            "git": read_git_status(root) if include_git and root is not None else {},
        },
        "checkpoints": [dict(item) for item in checkpoints],
        "diagnostics": normalize_diagnostics(data.get("diagnostics") or []),
        "sources": _paths_from_context(context),
        "totals": totals,
        "available": bool(files or checkpoints or data),
    }


def build_file_tree(
    paths: Iterable[Any],
    changed: Iterable[Any] = (),
    relevant: Iterable[Any] = (),
    *,
    max_rows: int = 500,
) -> List[Dict[str, Any]]:
    """Build a deterministic directory tree from repository-relative paths."""
    changed_set = {_relative(item) for item in changed if _relative(item)}
    relevant_set = {_relative(item) for item in relevant if _relative(item)}
    values = sorted({_relative(item) for item in paths if _relative(item)})
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        parts = value.split("/")
        for index, part in enumerate(parts):
            is_file = index == len(parts) - 1
            current = "/".join(parts[: index + 1])
            if current in seen:
                continue
            seen.add(current)
            rows.append(
                {
                    "path": current,
                    "name": part,
                    "kind": "file" if is_file else "directory",
                    "is_file": is_file,
                    "depth": index,
                    "changed": current in changed_set,
                    "relevant": current in relevant_set,
                    "label": f"{'  ' * index}{part}",
                }
            )
    return rows[: max(1, _bounded_int(max_rows, 500))]


def symbol_candidates(
    repo: Any,
    query: str = "",
    *,
    changed_files: Iterable[Any] = (),
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """Return ranked repository symbols for the file and symbol picker."""
    root = _root(repo)
    if root is None:
        return []
    try:
        from harness.retrieval import rank_symbols

        values = rank_symbols(
            str(root),
            terms=[part for part in re.split(r"[\s.:/-]+", str(query or "")) if part],
            changed_files=[
                _relative(item) for item in changed_files if _relative(item)
            ],
            limit=max(1, _bounded_int(limit, 30)),
        )
    except Exception:
        return []
    result: List[Dict[str, Any]] = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, Mapping):
            continue
        record = dict(value)
        record["label"] = str(
            record.get("qualified") or record.get("name") or record.get("path") or "?"
        )
        record["path"] = _relative(record.get("file") or record.get("path"))
        record["qualified"] = str(record.get("qualified") or record.get("name") or "")
        record["kind"] = str(record.get("kind") or "symbol")
        record["line"] = _bounded_int(record.get("line"), 0)
        result.append(record)
    return result


def file_picker_rows(
    repo: Any,
    query: str = "",
    *,
    changed_files: Iterable[Any] = (),
    include_symbols: bool = True,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return searchable file-tree and symbol rows for a picker."""
    root = _root(repo)
    if root is None:
        return []
    files = read_repository_files(
        root,
        cap=max(1, _bounded_int(limit, 100) * 4),
        prefer_git=False,
    )
    needle = str(query or "").strip().lower()
    if needle:
        files = [path for path in files if needle in path.lower()]
    rows = build_file_tree(
        files, changed=changed_files, max_rows=max(1, _bounded_int(limit, 100))
    )
    if include_symbols:
        rows.extend(
            symbol_candidates(root, query, changed_files=changed_files, limit=limit)
        )
    needle = str(query or "").strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle
            in str(
                row.get("path") or row.get("label") or row.get("qualified") or ""
            ).lower()
            or needle in str(row.get("kind") or "").lower()
        ]
    return rows[: max(1, _bounded_int(limit, 100))]


def relevant_file_rows(
    projection: Mapping[str, Any],
    query: str = "",
    *,
    limit: int = 120,
) -> List[Dict[str, Any]]:
    """Return a RANKED, REASONED list of the files that matter for this run.

    The reason this is a projection and not ``projection["relevant_files"]``:
    the projection's own list is a FALLBACK. ``build_file_projection`` fills
    it with ``_walk_repository_files(root, 12)`` whenever the run cited no
    context sources, so on a run with no context receipt it is twelve
    arbitrary files in directory order. That is why the round-1 tree had no
    relevant-files SURFACE: there was nothing ranked to show, and a list
    with no ranking and no reason per row is a worse ``/files``.

    Each row is ``{path, reason, roles, rank, changed, cited, staged,
    diagnostic, linked}`` where ``reason`` is a sentence a person can check
    ("changed in this run; staged for commit") and ``roles`` is the machine
    list it was derived from. **A file that earns its row by no rule is not
    included, and an empty result is the honest answer** — a list padded with
    twelve arbitrary repository files in directory order is a worse answer
    than a short one, and it is not a relevance claim.

    Ordering is deterministic: role strength, then the number of roles, then
    path, so two runs over the same state produce byte-identical output and a
    reader can trust that a file moving is a change in the STATE, not in the
    ranking's tie-breaking.
    """
    if not isinstance(projection, Mapping):
        return []
    changed = {str(item) for item in (projection.get("changed_files") or []) if item}
    file_records = [
        item
        for item in (projection.get("file_changes") or [])
        if isinstance(item, Mapping)
    ]
    # `changed_files` and `file_changes` are the same set in the projection the
    # product builds, but a caller-supplied snapshot can carry one without the
    # other. Union them rather than trusting either alone: a file present in
    # the richer record but missing from the flat list would otherwise vanish
    # from the relevant-files view while the diff browser still shows it.
    for item in file_records:
        path = _relative(item.get("path") or item.get("file"))
        if path:
            changed.add(path)
    staged = {
        _relative(item.get("path")) for item in file_records if item.get("staged")
    }
    staged.discard("")
    cited = {str(item) for item in (projection.get("sources") or []) if item}
    records = {
        _relative(item.get("path") or item.get("file")): dict(item)
        for item in file_records
        if _relative(item.get("path") or item.get("file"))
    }
    diagnostic_paths = {
        str(item.get("path"))
        for item in (projection.get("diagnostics") or [])
        if isinstance(item, Mapping) and item.get("path")
    }
    task_id = str(projection.get("task_id") or "")
    # `relevant_files` carries a relevance claim ONLY when it IS the cited
    # set. `build_file_projection` fills that key with a
    # `_walk_repository_files(root, 12)` FALLBACK whenever the run cited no
    # context sources, so outside a run with a context receipt it is a list
    # of arbitrary files in directory order. Reading it as "selected as
    # relevant context" phrases a claim nobody made — the same lie as
    # attributing a teammate's uncommitted file to the run.
    cited_by_run = {
        str(item) for item in (projection.get("relevant_files") or []) if item
    }
    carries_claim = bool(cited)

    candidates: List[str] = []
    for path in list(changed) + sorted(cited) + sorted(cited_by_run):
        normalized = _relative(path)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    rows: List[Dict[str, Any]] = []
    for path in candidates:
        roles: List[str] = []
        reasons: List[str] = []
        record = records.get(path, {})
        # "changed" splits into two claims, because they are two claims.
        # A file the RUN's own record names is one thing; a file that differs
        # only because `git status` reports it is another, and on a shared
        # working tree the second set includes every uncommitted file
        # belonging to someone else. `run_evidenced` is the authority, NOT
        # the diff `source` — a run-changed file's patch can legitimately
        # come from git, and reading `source` there would under-claim it.
        if path in changed:
            roles.append("changed")
            reasons.append(
                "changed in this run"
                if record.get("run_evidenced", str(record.get("source") or "") != "git")
                else "uncommitted in the workspace (not this run's change)"
            )
        if path in cited:
            roles.append("cited")
            reasons.append("cited by the context compiler")
        elif path in cited_by_run and carries_claim:
            roles.append("relevant")
            reasons.append("selected as relevant context")
        if path in staged:
            roles.append("staged")
            reasons.append("staged for commit")
        if path in diagnostic_paths:
            roles.append("diagnostic")
            reasons.append("carries a diagnostic")
        if not roles:
            # No rule earned this row. There is no filler: a list padded with
            # arbitrary repository files is a worse answer than a short one,
            # and the empty result is the honest "nothing is relevant yet".
            continue
        rows.append(
            {
                "path": path,
                "reason": "; ".join(reasons),
                "roles": roles,
                "changed": path in changed,
                "cited": path in cited or (path in cited_by_run and carries_claim),
                "staged": path in staged,
                "diagnostic": path in diagnostic_paths,
                "linked": len(roles) > 1,
                "task_id": task_id,
            }
        )
    strength = {"diagnostic": 5, "changed": 4, "staged": 3, "cited": 2, "relevant": 2}
    rows.sort(
        key=lambda row: (
            -max(strength.get(role, 1) for role in row["roles"]),
            -len(row["roles"]),
            row["path"],
        )
    )
    needle = str(query or "").strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle in row["path"].lower() or needle in row["reason"].lower()
        ]
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows[: max(1, _bounded_int(limit, 120))]


def relevant_projection(
    log_root: Any = None,
    repo: Any = None,
    task_id: Optional[str] = None,
    *,
    include_git: bool = True,
) -> Dict[str, Any]:
    """The projection `/relevant` should read, with an honest fallback.

    `NeoApp._file_projection` and `interactive.file_projection` both return
    `{}` when there is no active task, because a task-scoped projection
    needs a task directory. That made `/relevant` answer "no relevant
    files" in a FRESH session on a working tree with forty modified files —
    which is not a statement about relevance, it is a statement that the
    surface was never asked the question.

    So: with a task, read the task's own journal (the authoritative source,
    and the only one that can attribute a change to a run). Without one, read
    the WORKSPACE, which can still answer the question honestly — every row
    it produces is sourced from `git status` and therefore says "uncommitted
    in the workspace (not this run's change)". The two cases never blur.

    `source` is recorded on the result so a caller can tell which question
    was answered.
    """
    if task_id:
        from cli.interactive import file_projection

        scoped = file_projection(log_root, repo, task_id, include_git=include_git)
        if scoped:
            scoped.setdefault("projection_source", "run")
            return scoped
    root = _root(repo)
    if root is None:
        return {}
    scoped = build_file_projection(None, root, include_git=include_git)
    scoped["projection_source"] = "workspace"
    return scoped


def current_diff_text(task_dir: Any, repo: Any = None, max_lines: int = 2000) -> str:
    """Return the current task diff from journal-owned workspace artifacts."""
    directory = None
    try:
        directory = Path(str(task_dir)).expanduser().resolve() if task_dir else None
    except (OSError, RuntimeError, TypeError, ValueError):
        directory = None
    if directory is None:
        return ""
    if (directory / "work").is_dir():
        try:
            from cli.tracelog import live_diff

            values = live_diff(
                directory / "pristine", directory / "work", max_lines=max_lines
            )
            return "\n".join(str(value) for value, _kind in values)
        except Exception:
            return ""
    if (directory / "pristine").is_dir() and repo:
        try:
            from harness.agent_loop import agent_diff

            return str(agent_diff(directory.name, directory.parent, str(repo)) or "")
        except Exception:
            return ""
    if repo:
        root = _root(repo)
        status = read_git_status(root) if root is not None else {}
        chunks: List[str] = []
        for path in sorted(status):
            value = _diff_for_path(root, None, path, status)[0]
            if value:
                chunks.append(value)
        return "\n".join(chunks)[: max(0, _bounded_int(max_lines, 2000) * 240)]
    return ""


def diff_summary(value: Any) -> Dict[str, Any]:
    """Summarize a diff string or file projection for summary-first rendering."""
    files: List[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        raw_files = value.get("files") or value.get("file_changes") or []
        files = [item for item in raw_files if isinstance(item, Mapping)]
    elif isinstance(value, (list, tuple)):
        files = [item for item in value if isinstance(item, Mapping)]
    elif isinstance(value, str):
        current: List[str] = []
        for line in value.splitlines():
            if line.startswith("diff --git "):
                if current:
                    files.append({"path": "", "hunks": current})
                current = [line]
            elif current:
                current.append(line)
        if current:
            files.append({"path": "", "hunks": current})
    additions = sum(_bounded_int(item.get("additions")) for item in files)
    deletions = sum(_bounded_int(item.get("deletions")) for item in files)
    line_count = 0
    truncated = False
    for item in files:
        for hunk in (
            item.get("hunks", []) if isinstance(item.get("hunks"), list) else []
        ):
            if isinstance(hunk, Mapping):
                line_count += len(hunk.get("lines") or [])
            elif isinstance(hunk, (list, tuple, str)):
                line_count += len(hunk) if isinstance(hunk, (list, tuple)) else 1
        if item.get("truncated"):
            truncated = True
    if isinstance(value, str):
        line_count = len(value.splitlines())
    names = [str(item.get("path") or "") for item in files if item.get("path")]
    return {
        "files": len(names) or len(files),
        "file_names": names,
        "additions": additions,
        "deletions": deletions,
        "lines": line_count,
        "large": len(files) > 6 or line_count > 120,
        "truncated": truncated,
        "headline": f"{len(names) or len(files)} file(s) · +{additions} -{deletions}",
    }


def open_diff_file(
    projection: Mapping[str, Any], path: str, hunk: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Select one file and optional hunk from a file projection."""
    normalized = _relative(path)
    files = (
        projection.get("diff", {}).get("files", [])
        if isinstance(projection, Mapping)
        else []
    )
    for value in files if isinstance(files, list) else []:
        if not isinstance(value, Mapping) or _relative(value.get("path")) != normalized:
            continue
        if hunk is None:
            return dict(value)
        for candidate in value.get("hunks") or []:
            if isinstance(candidate, Mapping) and int(candidate.get("index", 0)) == int(
                hunk
            ):
                result = dict(value)
                result["selected_hunk"] = dict(candidate)
                return result
    return None


def parse_diff_target(text: Any) -> Dict[str, Any]:
    r"""Parse one diff target into ``{path, hunk, line, column}``.

    Accepts the four forms a person actually types, and NOTHING else:

    ============================  =======================================
    ``cli/tui.py``                the file
    ``cli/tui.py#2``              the file, hunk 2
    ``cli/tui.py:340``            the file, new-file line 340
    ``cli/tui.py:340:9``          the file, line 340, column 9
    ============================  =======================================

    Assumes nothing about the path beyond the ``_relative`` contract. A
    Windows drive letter is never mistaken for a line suffix (the same
    lookbehind ``split_open_target`` uses), and a ``#``/``:`` that is not
    followed by digits is left in the path rather than swallowed, because a
    silently-mangled path is worse than a rejected one.
    """
    raw = str(text or "").strip()
    result: Dict[str, Any] = {"path": raw, "hunk": None, "line": None, "column": None}
    if not raw:
        result["path"] = ""
        return result
    # The line form is consumed FIRST so the two forms compose:
    # ``f.py#2:5`` is hunk 2, line 5 rather than a path literally named
    # ``f.py#2``. Numeric suffixes are peeled right-to-left, and the
    # rightmost one is the COLUMN (``f.py:340:9`` is line 340, column 9) —
    # assigning them left-to-right silently transposes the pair and would
    # send a reader to line 9 of a 4000-line file.
    numbers: List[int] = []
    while ":" in raw and re.search(r":[0-9]+$", raw) is not None:
        head, _, tail = raw.rpartition(":")
        if not head or not tail.isdigit() or len(numbers) >= 2:
            break
        numbers.insert(0, int(tail))
        raw = head
    if len(numbers) == 1:
        result["line"] = numbers[0]
    elif len(numbers) == 2:
        result["line"] = numbers[0]
        result["column"] = numbers[1]
    # hunk form: a trailing #<digits>
    if "#" in raw:
        head, _, tail = raw.rpartition("#")
        if head and tail.isdigit():
            raw = head
            result["hunk"] = int(tail)
    result["path"] = _relative(raw)
    return result


def _diff_line_kind(line_text: Any) -> str:
    """Classify one unified-diff body line as added, deleted, or context."""
    text = str(line_text or "")
    if text.startswith("+"):
        return "added"
    if text.startswith("-"):
        return "deleted"
    return "context"


def open_diff_line(
    projection: Mapping[str, Any], path: str, line: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Select one file and, when a line is named, the hunk and row holding it.

    This is what makes the diff navigable at LINE granularity rather than
    hunk granularity. ``DiffHunk.line_numbers`` already carried real
    per-line numbers and ``_DiffFileScreen`` already rendered them, but the
    only way to *address* one was ``#<hunk>``: a 400-line hunk gave a reader
    no way to reach its 39th line from a diagnostic, a trace entry, or a
    teammate.

    Returns the file record with ``selected_line`` (the new-file line) and
    ``selected_offset`` (its index within ``selected_hunk``) added, or
    ``None`` when the file has no recorded diff.

    For a DELETED line there is no new-file line, so the search falls back to
    the old-file start and the result says so via
    ``selected_line_kind="old"`` rather than silently landing on a neighbour.
    """
    selected = open_diff_file(projection, path)
    if selected is None:
        return None
    target = _bounded_int(line, 0)
    if target <= 0:
        return selected
    for hunk in selected.get("hunks") or []:
        if not isinstance(hunk, Mapping):
            continue
        numbers = list(hunk.get("line_numbers") or [])
        for offset, number in enumerate(numbers):
            if number is None or int(number) != target:
                continue
            line_text = ""
            lines = list(hunk.get("lines") or [])
            if offset < len(lines):
                line_text = str(lines[offset])
            result = dict(selected)
            result["selected_hunk"] = dict(hunk)
            result["selected_line"] = target
            result["selected_offset"] = offset
            # A new-file line number is shared by a context line and the
            # removal that replaced it, so the KIND has to come from the
            # line's own prefix. Calling a context line "added" would tell a
            # reader the run wrote something it did not.
            result["selected_line_kind"] = _diff_line_kind(line_text)
            result["selected_line_text"] = line_text
            return result
    # A deleted line carries no new-file number; address it through the hunk
    # whose old range contains the target.
    for hunk in selected.get("hunks") or []:
        if not isinstance(hunk, Mapping):
            continue
        old_start = _bounded_int(hunk.get("old_start"), 0)
        old_count = _bounded_int(hunk.get("old_count"), 0)
        if not (old_start <= target <= old_start + max(0, old_count - 1)):
            continue
        result = dict(selected)
        result["selected_hunk"] = dict(hunk)
        result["selected_line"] = target
        result["selected_offset"] = 0
        result["selected_line_kind"] = "old"
        result["selected_line_text"] = ""
        return result
    result = dict(selected)
    result["selected_line"] = target
    result["selected_offset"] = None
    result["selected_line_kind"] = "outside_diff"
    result["selected_line_text"] = ""
    return result


def changed_line_index(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every added/removed line in one file record, in display order.

    The line-level navigator's ``next``/``previous`` steps. Each entry is
    ``{line, hunk, offset, side}`` so a surface can jump without re-deriving
    the hunk it belongs to.
    """
    result: List[Dict[str, Any]] = []
    for hunk in record.get("hunks") or []:
        if not isinstance(hunk, Mapping):
            continue
        numbers = list(hunk.get("line_numbers") or [])
        lines = list(hunk.get("lines") or [])
        for offset, text in enumerate(lines):
            if not str(text).startswith(("+", "-")):
                continue
            number = numbers[offset] if offset < len(numbers) else None
            result.append(
                {
                    "line": int(number) if number else 0,
                    "hunk": int(hunk.get("index", 0) or 0),
                    "offset": offset,
                    "side": "deleted" if str(text).startswith("-") else "added",
                }
            )
    return result


def normalize_diagnostic(value: Any, repo: Any = None) -> Dict[str, Any]:
    """Normalize journal and LSP diagnostics to path, line, column, and link fields."""
    if not isinstance(value, Mapping):
        return {
            "path": "",
            "file": "",
            "line": 1,
            "column": 1,
            "end_line": 1,
            "end_column": 1,
            "severity": "error",
            "message": redact_text(value),
            "source": "",
            "code": "",
            "data": {},
            "link": "",
            "provenance": "journal",
        }
    raw_path = value.get("path") or value.get("file") or value.get("uri") or ""
    path = str(raw_path or "").replace("\\", "/")
    root = _root(repo)
    if root is not None and path:
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                path = candidate.resolve().relative_to(root).as_posix()
            except (OSError, RuntimeError, ValueError):
                path = ""
    path = _relative(path)
    raw_line = value.get("line", value.get("row", 1))
    lsp_shape = "file" in value and "path" not in value and "column" in value
    line = _bounded_int(int(raw_line) + 1 if lsp_shape else raw_line, 1) or 1
    column = (
        _bounded_int(
            int(value.get("column", value.get("char", 0))) + 1
            if lsp_shape
            else value.get("column", 1),
            1,
        )
        or 1
    )
    end_line = (
        _bounded_int(
            int(value.get("end_line", raw_line)) + 1
            if lsp_shape
            else value.get("end_line", line),
            line,
        )
        or line
    )
    end_column = (
        _bounded_int(
            int(value.get("end_column", 0)) + 1
            if lsp_shape
            else value.get("end_column", column),
            column,
        )
        or column
    )
    result = {
        "path": path,
        "file": path,
        "line": line,
        "column": column,
        "end_line": end_line,
        "end_column": end_column,
        "severity": str(value.get("severity") or "error").lower(),
        "message": redact_text(
            value.get("message") or value.get("detail") or ""
        ).strip(),
        "source": str(value.get("source") or ""),
        "code": value.get("code"),
        "data": dict(value.get("data") or {})
        if isinstance(value.get("data"), Mapping)
        else {},
    }
    result["link"] = diagnostic_link(result)
    result["provenance"] = diagnostic_provenance(value)
    return result


def diagnostic_provenance(value: Any) -> str:
    """Return where a diagnostic came from: ``journal`` or ``lsp``.

    A row a run recorded and a row a language server is emitting right now
    are different claims with different lifetimes, and a panel that renders
    them as one undifferentiated list invites a reader to trust a stale
    journal row as if it were a live check. The classification is derived
    from the SHAPE, never from a caller-supplied label, so a journal row
    cannot relabel itself as live and vice versa.

    The LSP shape is the ``harness.lsp.Diagnostic`` dataclass: a ``file``
    key with no ``path`` and a ``column``. Anything else is a journal row.
    """
    if not isinstance(value, Mapping):
        return "journal"
    if "file" in value and "path" not in value and "column" in value:
        return "lsp"
    return "journal"


def normalize_diagnostics(values: Any, repo: Any = None) -> List[Dict[str, Any]]:
    """Normalize a diagnostic collection and remove duplicate links."""
    source = values if isinstance(values, (list, tuple)) else [values]
    result: List[Dict[str, Any]] = []
    seen: set[Tuple[str, int, int, str]] = set()
    for value in source:
        normalized = normalize_diagnostic(value, repo=repo)
        key = (
            normalized["path"],
            normalized["line"],
            normalized["column"],
            normalized["message"],
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def diagnostic_link(value: Mapping[str, Any]) -> str:
    """Return a stable editor-friendly path/line/column link."""
    path = _relative(value.get("path") or value.get("file"))
    if not path:
        return ""
    line = _bounded_int(value.get("line"), 1) or 1
    column = _bounded_int(value.get("column"), 1) or 1
    return f"{path}:{line}:{column}"


def lsp_state_report(
    repo: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    *,
    timeout_s: float = 8.0,
) -> Dict[str, Any]:
    """Return an honest receipt for the live language-server path.

    Every field here exists because the previous implementation was
    ``except Exception: pass``. A language server that is not configured,
    one that cannot start, and one that was never attempted are three
    different facts, and collapsing them into "no diagnostics available"
    makes an absent check indistinguishable from a clean workspace. The
    panel renders this receipt, so a reader can see which of the three it is
    looking at.

    Assumes ``repo`` is a repository path and ``config`` is a parsed
    ``.neo/lsp.json``. Never raises: an unusable language server is a
    reported state, not an exception on a UI path.
    """
    root = _root(repo)
    report: Dict[str, Any] = {
        "available": False,
        "state": "not_configured",
        "reason": "no .neo/lsp.json in this repository",
        "config_path": str(Path(str(root)) / ".neo" / "lsp.json")
        if root is not None
        else "",
        "server": "",
        "count": 0,
        "error": "",
    }
    if config is None and root is not None:
        candidate = Path(str(root)) / ".neo" / "lsp.json"
        if not candidate.is_file():
            return report
        report["config_path"] = str(candidate)
        try:
            config = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            report["state"] = "unreadable_config"
            report["reason"] = f"{type(exc).__name__}: {exc}"
            return report
    if not isinstance(config, Mapping) or not config:
        report["state"] = "unreadable_config"
        report["reason"] = "configuration is empty or not an object"
        return report
    report["server"] = str(config.get("command") or "")
    if not report["server"]:
        report["state"] = "unreadable_config"
        report["reason"] = "configuration names no server command"
        return report
    try:
        from harness.lsp import LspManager, get_diagnostics

        manager = LspManager.from_config(config, repo_path=root)
        started = bool(manager.start())
    except Exception as exc:
        report["state"] = "unavailable"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report
    if not started:
        report["state"] = "unavailable"
        report["reason"] = "the language server did not start"
        return report
    try:
        values = [
            item.to_dict()
            for item in get_diagnostics(manager)
            if hasattr(item, "to_dict")
        ]
    except Exception as exc:
        report["state"] = "unavailable"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        try:
            manager.close()
        except Exception:
            pass
    report["available"] = True
    report["state"] = "live"
    report["reason"] = ""
    report["count"] = len(values)
    return report


def lsp_diagnostics(
    repo: Any = None,
    config: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return live language-server diagnostics, or an honest empty list.

    The receipt from :func:`lsp_state_report` is the reason this is a
    separate function rather than a branch inside ``_diagnostic_lines``: a
    caller needs BOTH the rows and the reason there are none, and a function
    returning only rows makes "the server refused to start" look identical to
    "your code is clean".
    """
    report = lsp_state_report(repo, config)
    if not report.get("available"):
        return []
    root = _root(repo)
    candidate = Path(str(root)) / ".neo" / "lsp.json" if root is not None else None
    if config is None and candidate is not None and candidate.is_file():
        try:
            config = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            return []
    try:
        from harness.lsp import LspManager, get_diagnostics

        manager = LspManager.from_config(config, repo_path=root)
        if not manager.start():
            return []
        try:
            raw = [
                dict(item.to_dict())
                for item in get_diagnostics(manager)
                if hasattr(item, "to_dict")
            ]
        finally:
            try:
                manager.close()
            except Exception:
                pass
        # Normalized here, not left to the caller: a row without `provenance`
        # and without a `path:line:column` link is a different shape from the
        # journal rows, and one producer is the whole point.
        return normalize_diagnostics(raw, repo=root)
    except Exception:
        return []


def diagnostic_rows(
    repo: Any = None,
    task_dir: Any = None,
    snapshot: Optional[Mapping[str, Any]] = None,
    *,
    log_root: Any = None,
    include_git: bool = True,
) -> List[Dict[str, Any]]:
    """Return journal diagnostics plus optional configured live LSP diagnostics.

    ``log_root`` is the artifact root and is accepted so a caller that has
    only a root can ask without inventing a task id. It cannot be reduced
    to one run's journal rows on its own, so it is used for nothing here
    and that is stated rather than papered over with a swapped argument: the
    previous version called ``_diagnostic_lines(Path(str(repo)), repo, None)``,
    handing the REPOSITORY where the log root belongs and the log root where
    the repository belonged. It was harmless only because the third argument
    (the task id) was ``None`` so the journal read was skipped, and the two
    live-LSP calls then double-counted whatever a server did report.
    """
    values: List[Any] = []
    if snapshot is not None:
        values.extend(snapshot.get("diagnostics") or [])
    if task_dir:
        try:
            from cli.runview import read_diagnostics

            values.extend(read_diagnostics(Path(str(task_dir))))
        except Exception:
            pass
    # The journal read needs a TASK directory; a bare log root cannot be
    # reduced to one run's diagnostics without a task id, so there is
    # nothing more to read here. The previous version called
    # `_diagnostic_lines(log_root, repo, None)` in that branch, and then
    # ALSO called `lsp_diagnostics(repo)` below — and `_diagnostic_lines`
    # already includes live LSP rows, so a configured language server was
    # queried twice and its findings appended twice. One collection point
    # per source is the whole point of the provenance tag.
    if include_git and _root(repo) is not None:
        values.extend(lsp_diagnostics(repo))
    return normalize_diagnostics(values, repo=repo)


def _fast_context_snapshot(
    root: Path,
    issue_text: str,
    token_budget: int,
) -> Dict[str, Any]:
    files = _walk_repository_files(root, 24)
    sources: List[Dict[str, Any]] = []
    for path in files:
        if path.endswith("SKILL.md") or "/skills/" in f"/{path}":
            kind = "skill"
        elif Path(path).name in {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "QWEN.md"}:
            kind = "project_instructions"
        else:
            kind = "repository_map"
        sources.append(
            {
                "source": kind,
                "path": path,
                "line": 0,
                "end_line": 0,
                "digest": "",
                "citation_id": f"bounded:{kind}:{path}",
                "reason": "bounded repository index",
            }
        )
    memory: List[Dict[str, Any]] = []
    if os.environ.get("NEO_CONTEXT_MEMORY") == "1":
        try:
            from memory.decision_store import open_default_store

            rows = open_default_store().search(issue_text, limit=6, repo_path=str(root))
        except Exception:
            rows = []
        for value in rows if isinstance(rows, list) else []:
            if not isinstance(value, Mapping):
                continue
            memory.append(
                {
                    "source": "decision_memory",
                    "path": "memory",
                    "line": 0,
                    "end_line": 0,
                    "digest": "",
                    "citation_id": f"memory:{value.get('id', '')}",
                    "reason": "repo-scoped decision memory",
                }
            )
        sources.extend(memory)
    skills = [item for item in sources if item["source"] == "skill"]
    return {
        "repo": str(root),
        "issue": str(issue_text or ""),
        "sections": [
            {
                "name": "repository_map",
                "text": "",
                "reason": "bounded repository index for a large workspace",
                "included": True,
                "source_refs": [
                    {"path": item["path"], "source": item["source"], "line": 0}
                    for item in sources
                ],
            }
        ],
        "sources": sources,
        "skills": skills,
        "memory": memory,
        "citations": [],
        "token_budget": max(1, _bounded_int(token_budget, 4000)),
        "estimated_tokens": 0,
        "omitted": [
            {"role": "full_context", "reason": "large workspace bounded index"}
        ],
        "warnings": ["large workspace: showing a bounded repository index"],
        "source_digest": "",
        "index_digest": "",
    }


def context_snapshot(
    repo: Any,
    issue_text: str = "",
    *,
    task_id: Optional[str] = None,
    changed_files: Optional[Sequence[Any]] = None,
    selected_files: Optional[Sequence[Any]] = None,
    token_budget: int = 4000,
    config: Optional[Mapping[str, Any]] = None,
    use_cache: bool = False,
) -> Dict[str, Any]:
    """Compile a bounded cited context bundle and expose source metadata only."""
    root = _root(repo)
    if root is None:
        return {
            "repo": "",
            "issue": str(issue_text or ""),
            "sections": [],
            "sources": [],
            "skills": [],
            "memory": [],
            "citations": [],
            "token_budget": max(1, _bounded_int(token_budget, 4000)),
            "estimated_tokens": 0,
            "omitted": [],
            "warnings": ["repository is unavailable"],
        }
    if _repository_file_count(root, 300) >= 300:
        return _fast_context_snapshot(root, issue_text, token_budget)
    settings = dict(config or {})
    settings.setdefault(
        "context_token_budget", max(1, _bounded_int(token_budget, 4000))
    )
    settings.setdefault("context_cache_entries", 16)
    settings.setdefault("include_decision_memory", True)
    settings.setdefault("skills_enabled", True)
    try:
        from harness.context_compiler import ContextCompiler

        bundle = ContextCompiler(root, config=settings).compile(
            issue_text=issue_text,
            task={"issue_text": issue_text, "task_id": task_id or ""},
            repo_path=root,
            selected_files=list(selected_files or []),
            changed_files=list(changed_files or []),
            token_budget=max(1, _bounded_int(token_budget, 4000)),
            task_id=task_id,
            use_cache=bool(use_cache),
        )
        result = bundle.as_dict(include_source=False)
    except Exception as exc:
        result = {
            "sections": [],
            "citations": [],
            "source_references": [],
            "token_budget": max(1, _bounded_int(token_budget, 4000)),
            "estimated_tokens": 0,
            "omitted": [],
            "warnings": [f"{type(exc).__name__}: {exc}"],
            "text": "",
        }
    references = result.get("source_references") or []
    sources: List[Dict[str, Any]] = []
    for value in references:
        if not isinstance(value, Mapping):
            continue
        citation = (
            value.get("citation") if isinstance(value.get("citation"), Mapping) else {}
        )
        path = _relative(value.get("path") or citation.get("path"))
        source_name = str(value.get("source") or citation.get("source") or "")
        item = {
            "source": source_name,
            "path": path,
            "line": _bounded_int(value.get("line") or citation.get("line"), 0),
            "end_line": _bounded_int(
                value.get("end_line") or citation.get("end_line"), 0
            ),
            "digest": str(value.get("digest") or citation.get("digest") or ""),
            "citation_id": str(citation.get("id") or ""),
            "reason": str(
                value.get("reason") or citation.get("role") or "cited source"
            ),
        }
        sources.append(item)
    if not any("repository" in str(item.get("source", "")).lower() for item in sources):
        mapped_files = _walk_repository_files(root, 20)
        for value in mapped_files:
            path = _relative(value)
            if path:
                sources.append(
                    {
                        "source": "repository_map",
                        "path": path,
                        "line": 0,
                        "end_line": 0,
                        "digest": "",
                        "citation_id": "",
                        "reason": "structural repository map",
                    }
                )
    skills = [
        item
        for item in sources
        if "skill" in str(item.get("source", "")).lower()
        or str(item.get("reason", "")).lower() == "skills"
    ]
    memory = [
        item
        for item in sources
        if "decision" in str(item.get("source", "")).lower()
        or "memory" in str(item.get("source", "")).lower()
    ]
    return {
        "repo": str(root),
        "issue": str(issue_text or ""),
        "sections": [
            {
                "name": str(section.get("name") or ""),
                "text": str(section.get("text") or ""),
                "reason": str(section.get("reason") or ""),
                "included": bool(section.get("included", True)),
                "source_refs": [
                    {
                        "path": _relative(ref.get("path") or ref.get("file")),
                        "source": str(ref.get("source") or ""),
                        "line": _bounded_int(ref.get("line"), 0),
                    }
                    for ref in section.get("source_refs", [])
                    if isinstance(ref, Mapping)
                ],
            }
            for section in result.get("sections", [])
            if isinstance(section, Mapping)
        ],
        "sources": sources,
        "skills": skills,
        "memory": memory,
        "citations": list(result.get("citations") or []),
        "token_budget": result.get(
            "token_budget", max(1, _bounded_int(token_budget, 4000))
        ),
        "estimated_tokens": result.get("estimated_tokens", 0),
        "omitted": list(result.get("omitted") or []),
        "warnings": list(result.get("warnings") or []),
        "source_digest": str(result.get("source_digest") or ""),
        "index_digest": str(result.get("index_digest") or ""),
    }


def _manager(repo: Any, log_root: Any, session_id: str = "") -> Any:
    try:
        from memory.checkpoints import CheckpointManager

        return CheckpointManager(repo, log_root, session_id=session_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AGT-09: staged undo
# ---------------------------------------------------------------------------
#
# The shells must not talk to the store directly, for the same reason
# ``_manager`` exists: the memory layer owns the policy, this layer owns the
# projection, and the renderer owns the words. Every function here returns plain
# data and NEVER raises - a `/undo` that raises into a Textual handler takes the
# whole TUI down, which is the failure mode this repo keeps having to repair.


def _staged_store(repo: Any, log_root: Any, session_id: str = "") -> Any:
    """Return a ``StagedSnapshotStore`` or ``None`` when one cannot be built."""
    try:
        from memory.checkpoints import StagedSnapshotStore

        return StagedSnapshotStore(repo, log_root, session_id)
    except Exception:
        return None


def undo_scopes() -> Tuple[str, ...]:
    """The three restore granularities, in the memory layer's vocabulary."""
    try:
        from memory.checkpoints import RESTORE_SCOPES

        return tuple(RESTORE_SCOPES)
    except Exception:
        return ("files", "conversation", "both")


def undo_scope_words(scope: str) -> str:
    """One honest sentence per granularity; the middle one is the point."""
    return {
        "files": "code only - the conversation stays exactly as it is",
        "conversation": "the conversation only - your files are untouched",
        "both": "code AND the conversation",
    }.get(str(scope or ""), str(scope or ""))


def undo_live_refusal(in_flight: bool) -> Dict[str, Any]:
    """The ONE live-run gate for a revert. Shared by every surface.

    Reverting a tree a run is still editing is how an agent's work and a
    person's edit destroy each other, so the refusal lives in one place rather
    than being re-implemented in three renderers that can drift.
    """
    if not in_flight:
        return {"ok": True, "status": "ok", "reason": ""}
    return {
        "ok": False,
        "status": "refused",
        "reason": "run_in_flight",
        "detail": "a run is editing this repository; wait for it or /cancel it before undoing",
    }


def staged_undo_state(
    repo: Any, log_root: Any, *, session_id: str = ""
) -> Dict[str, Any]:
    """Read-only view of the staged revert range and the captured turns."""
    store = _staged_store(repo, log_root, session_id)
    if store is None:
        return {
            "available": False,
            "staged": False,
            "turns": [],
            "staged_turn_ids": [],
            "scopes": list(undo_scopes()),
            "reason": "no staged-undo store is available for this workspace",
        }
    try:
        staged = store.staged()
        turns = store.turns(limit=16)
    except Exception as exc:
        return {
            "available": False,
            "staged": False,
            "turns": [],
            "staged_turn_ids": [],
            "scopes": list(undo_scopes()),
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {
        "available": True,
        "staged": bool(staged),
        "turns": turns,
        "staged_turn_ids": [str(item) for item in (staged.get("turn_ids") or [])],
        "scope": str(staged.get("scope") or ""),
        "widened": int(staged.get("widened") or 0),
        "paths": [str(item) for item in (staged.get("paths") or [])],
        "scopes": list(undo_scopes()),
        "reason": "",
    }


def stage_undo(
    repo: Any,
    log_root: Any,
    *,
    session_id: str = "",
    count: int = 1,
    scope: str = "",
    in_flight: bool = False,
    widen: bool = False,
) -> Dict[str, Any]:
    """Stage a revert, widening an existing range rather than popping it."""
    gate = undo_live_refusal(bool(in_flight))
    if not gate["ok"]:
        return gate
    store = _staged_store(repo, log_root, session_id)
    if store is None:
        return {
            "ok": False,
            "status": "unavailable",
            "reason": "no staged-undo store is available for this workspace",
        }
    try:
        if widen:
            result = store.widen(count=count)
        else:
            result = store.stage(
                count=count,
                scope=scope or DEFAULT_UNDO_SCOPE,
            )
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    out = dict(result)
    out.setdefault("ok", False)
    out["scope_word"] = undo_scope_words(str(out.get("scope") or ""))
    out["scopes"] = list(undo_scopes())
    return out


def undo_plan(
    repo: Any, log_root: Any, *, session_id: str = "", scope: str = ""
) -> Dict[str, Any]:
    """Describe what committing the staged revert WOULD do; writes nothing."""
    store = _staged_store(repo, log_root, session_id)
    if store is None:
        return {
            "ok": False,
            "status": "unavailable",
            "reason": "no staged-undo store is available for this workspace",
        }
    try:
        result = dict(store.plan(scope=scope or None))
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    result.setdefault("ok", False)
    result["scope_word"] = undo_scope_words(str(result.get("scope") or ""))
    return result


def commit_undo(
    repo: Any,
    log_root: Any,
    *,
    session_id: str = "",
    scope: str = "",
    force: bool = False,
    in_flight: bool = False,
) -> Dict[str, Any]:
    """Apply the staged revert and return its hash-verified receipt."""
    gate = undo_live_refusal(bool(in_flight))
    if not gate["ok"]:
        return gate
    store = _staged_store(repo, log_root, session_id)
    if store is None:
        return {
            "ok": False,
            "status": "unavailable",
            "reason": "no staged-undo store is available for this workspace",
        }
    try:
        receipt = dict(
            store.commit(scope=scope or None, force=bool(force), live_run=False)
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "restored": [],
            "refused": [],
            "excluded": [],
            "verified": False,
        }
    receipt["scope_word"] = undo_scope_words(str(receipt.get("scope") or ""))
    receipt["lines"] = render_undo_receipt(receipt)
    return receipt


def discard_undo(repo: Any, log_root: Any, *, session_id: str = "") -> Dict[str, Any]:
    """Drop the staged range without reverting anything."""
    store = _staged_store(repo, log_root, session_id)
    if store is None:
        return {
            "ok": False,
            "status": "unavailable",
            "reason": "no staged-undo store is available for this workspace",
        }
    try:
        return dict(store.discard())
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def undo_turn_rows(
    repo: Any, log_root: Any, *, session_id: str = "", limit: int = 8
) -> List[Dict[str, Any]]:
    """Return each captured turn with the paths its assistant message changed.

    This is the "record changed paths on the assistant message" surface: the
    row is written by ``harness.editor.StageCapture`` at the point the
    mutation happened, and read back here so a user can see what a turn
    touched without diffing the repository themselves.
    """
    store = _staged_store(repo, log_root, session_id)
    if store is None:
        return []
    try:
        recorded = {
            str(row.get("turn_id") or ""): row
            for row in store._rows("assistant_message")
        }
        turns = store.turns()
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    for turn in reversed(turns):
        turn_id = str(turn.get("turn_id") or "")
        message = recorded.get(turn_id) or {}
        rows.append(
            {
                "turn_id": turn_id,
                "event": str(turn.get("event") or ""),
                "snapshots": [str(item) for item in (turn.get("snapshots") or [])],
                "changed_paths": [
                    str(item) for item in (message.get("changed_paths") or [])
                ],
                "summary": str(message.get("summary") or ""),
                "created_at": float(turn.get("created_at") or 0.0),
            }
        )
        if limit and len(rows) >= limit:
            break
    return rows


#: Words `/undo` understands as a verb rather than as a file path.
UNDO_VERBS: Tuple[str, ...] = ("commit", "apply", "discard", "cancel", "force", "plan")

#: The words a PERSON types for a granularity, mapped onto the one canonical
#: vocabulary. ``/undo code`` is a sentence; ``/undo files`` is a file mode.
#: Accepting both here is why the help text can promise ``code|task|all``
#: without the dispatcher silently treating ``code`` as a filename.
UNDO_SCOPE_ALIASES: Dict[str, str] = {
    "code": "files",
    "file": "files",
    "files": "files",
    "task": "conversation",
    "conversation": "conversation",
    "all": "both",
    "both": "both",
}


def undo_scope_for(word: Any) -> str:
    """Resolve a typed granularity word to its canonical scope, or ``""``."""
    return UNDO_SCOPE_ALIASES.get(str(word or "").strip().lower(), "")


def undo_command(
    repo: Any,
    log_root: Any,
    arg: str = "",
    *,
    session_id: str = "",
    in_flight: bool = False,
) -> Dict[str, Any]:
    """The ONE dispatcher for the staged-undo surface, shared by both shells.

    Returns plain data and never raises:

    ``handled``   False means "not mine" - the caller keeps its historical
                  per-file revert, which is why ``/undo <file>`` still behaves
                  exactly as it did.
    ``kind``      which sub-verb ran, so a renderer can word it.
    ``lines``     PLAIN, unstyled lines. Both shells render these and escape
                  them; nothing here emits markup, because a repository path
                  may contain ``[`` and a markup parser would eat the message
                  rather than print it.
    ``ok``        the honest outcome word, never a success claim the payload
                  does not support.
    """
    text = str(arg or "").strip()
    low = text.lower()
    verb = low if low in UNDO_VERBS else ""

    # Verbs first: `/undo commit` on a workspace with no store must still be
    # OWNED here, so the user gets "unavailable" and not a silent fall-through
    # into the historical engine.
    if verb in ("discard", "cancel"):
        result = discard_undo(repo, log_root, session_id=session_id)
        if result.get("ok") or str(result.get("status") or "") == "nothing_staged":
            return {
                "handled": True,
                "kind": "discard",
                "ok": True,
                "lines": [
                    "staged revert discarded - nothing was reverted"
                    if result.get("ok")
                    else "nothing was staged"
                ],
                "payload": result,
            }
        return {
            "handled": True,
            "kind": "discard",
            "ok": False,
            "lines": [f"discard failed: {result.get('reason') or 'unknown error'!s}"],
            "payload": result,
        }
    if verb == "plan":
        plan = undo_plan(repo, log_root, session_id=session_id)
        return {
            "handled": True,
            "kind": "plan",
            "ok": bool(plan.get("ok")),
            "lines": render_undo_receipt(plan),
            "payload": plan,
        }
    if verb in ("commit", "apply", "force"):
        receipt = commit_undo(
            repo,
            log_root,
            session_id=session_id,
            force=verb == "force",
            in_flight=bool(in_flight),
        )
        return {
            "handled": True,
            "kind": "commit",
            "ok": bool(receipt.get("ok")),
            "lines": render_undo_receipt(receipt),
            "payload": receipt,
        }

    state = staged_undo_state(repo, log_root, session_id=session_id)
    if not state.get("available"):
        return {"handled": False, "kind": "", "ok": False, "lines": [], "payload": {}}

    scope = undo_scope_for(low)
    if scope:
        return _undo_scope_command(repo, log_root, state, scope, session_id=session_id)
    if text:
        # A path, not a verb: the historical per-file revert owns it.
        return {"handled": False, "kind": "", "ok": False, "lines": [], "payload": {}}
    if not state.get("turns"):
        # No captured turn at all. A bare `/undo` here must NOT become a dead
        # end: a session that edited files through a path this feature does
        # not cover (an older run, a hand edit) still has to be revertible
        # through the historical per-run engine. Hand the line back.
        return {"handled": False, "kind": "", "ok": False, "lines": [], "payload": {}}
    gate = undo_live_refusal(bool(in_flight))
    if not gate["ok"]:
        return {
            "handled": True,
            "kind": "refused",
            "ok": False,
            "lines": [
                "undo refused: a run is in flight",
                str(gate.get("detail") or ""),
            ],
            "payload": gate,
        }
    result = stage_undo(
        repo,
        log_root,
        session_id=session_id,
        widen=bool(state.get("staged")),
        in_flight=False,
    )
    return _staged_lines("widen" if result.get("widened") else "stage", result)


def _undo_scope_command(
    repo: Any,
    log_root: Any,
    state: Mapping[str, Any],
    scope: str,
    *,
    session_id: str,
) -> Dict[str, Any]:
    """Change the granularity of the staged range, or say why it could not."""
    if not state.get("staged"):
        if not state.get("turns"):
            return {
                "handled": False,
                "kind": "",
                "ok": False,
                "lines": [],
                "payload": {},
            }
        return {
            "handled": True,
            "kind": "scope",
            "ok": False,
            "lines": [
                "nothing is staged yet - run /undo first",
                "granularities: " + ", ".join(undo_scopes()),
            ],
            "payload": state,
        }
    store = _staged_store(repo, log_root, session_id)
    if store is None:
        return {
            "handled": True,
            "kind": "scope",
            "ok": False,
            "lines": ["undo unavailable: no staged-undo store for this workspace"],
            "payload": state,
        }
    try:
        result = dict(store.set_scope(scope))
    except Exception as exc:
        return {
            "handled": True,
            "kind": "scope",
            "ok": False,
            "lines": [f"could not set the granularity: {type(exc).__name__}: {exc}"],
            "payload": {},
        }
    return _staged_lines("scope", result)


def _staged_lines(kind: str, result: Mapping[str, Any]) -> Dict[str, Any]:
    """Word a staging result, always ending with when the revert actually runs."""
    record = dict(result)
    if not record.get("ok"):
        reason = str(record.get("reason") or record.get("status") or "unknown")
        return {
            "handled": True,
            "kind": kind,
            "ok": False,
            "lines": [
                f"nothing staged: {reason}",
                "a captured turn is what makes a revert possible - /undo <file> "
                "still reverts one file of the last agent run",
            ],
            "payload": record,
        }
    turns = ", ".join(str(item) for item in (record.get("turn_ids") or []))
    paths = [str(item) for item in (record.get("paths") or [])]
    scope = str(record.get("scope") or "")
    lines = [f"staged revert of {turns or 'nothing'} across {len(paths)} path(s)"]
    lines.append(f"granularity: {scope} - {undo_scope_words(scope)}")
    for path in paths[:8]:
        lines.append(f"  {path}")
    if len(paths) > 8:
        lines.append(f"  ... and {len(paths) - 8} more")
    if int(record.get("widened") or 0):
        lines.append(f"range widened by {int(record.get('widened') or 0)} turn(s)")
    lines.append(
        "nothing is reverted yet - your next prompt commits it, or use /undo commit"
    )
    record["scope_word"] = undo_scope_words(scope)
    return {
        "handled": True,
        "kind": kind,
        "ok": True,
        "lines": lines,
        "payload": record,
    }


def commit_staged_undo_for_prompt(
    repo: Any,
    log_root: Any,
    *,
    session_id: str = "",
    in_flight: bool = False,
) -> Dict[str, Any]:
    """Commit a staged CODE-ONLY revert because the user asked for more work.

    This is the second half of the staged model: ``/undo`` stages, and the NEXT
    PROMPT is what commits it, so a user can stage, change their mind about the
    granularity, and then keep talking.

    Only the ``files`` scope auto-commits. A ``conversation`` or ``both`` range
    deliberately throws history away, and that has to be typed, not inferred
    from the next thing somebody typed.
    """
    if bool(in_flight):
        return {"committed": False, "reason": "run_in_flight", "lines": []}
    state = staged_undo_state(repo, log_root, session_id=session_id)
    if not state.get("staged"):
        return {"committed": False, "reason": "nothing_staged", "lines": []}
    scope = str(state.get("scope") or "")
    if undo_scope_for(scope) not in ("", DEFAULT_UNDO_SCOPE):
        return {
            "committed": False,
            "reason": "scope_requires_explicit_commit",
            "lines": [
                f"a {scope} revert is staged and needs /undo commit - "
                "it rewrites the conversation, so it is not applied on a prompt"
            ],
        }
    receipt = commit_undo(repo, log_root, session_id=session_id, in_flight=False)
    return {
        "committed": bool(receipt.get("ok")),
        "reason": str(receipt.get("status") or ""),
        "scope": scope,
        "lines": ["committing the staged revert:", *render_undo_receipt(receipt)],
        "payload": receipt,
    }


def render_undo_receipt(receipt: Mapping[str, Any]) -> List[str]:
    """Render a revert receipt as plain, unstyled lines.

    Plain text on purpose: the TUI transcript, the rich REPL, and
    ``--json``/scripted callers all read these same lines, and a line that
    crossed into Textual's markup parser carrying a repository path (which may
    contain ``[``) would delete a message rather than print one.
    """
    if not isinstance(receipt, Mapping):
        return ["undo failed: no receipt"]
    status = str(receipt.get("status") or "failed")
    lines: List[str] = []
    if status == "nothing_staged":
        return ["nothing is staged; run undo first"]
    if status == "refused":
        return [
            f"undo refused: {receipt.get('reason') or 'unknown'!s}",
            str(receipt.get("detail") or ""),
        ]
    if status == "unavailable":
        return [f"undo unavailable: {receipt.get('reason') or 'unknown'!s}"]
    if status == "error":
        return [f"undo failed: {receipt.get('reason') or 'unknown'!s}"]
    scope = str(receipt.get("scope") or "")
    turn_ids = [str(item) for item in (receipt.get("turn_ids") or [])]
    turns = ", ".join(turn_ids)
    restored = list(receipt.get("restored") or [])
    refused = list(receipt.get("refused") or [])
    excluded = list(receipt.get("excluded") or [])
    lines.append(
        f"reverted {len(restored)} path(s) across {len(turn_ids)} turn(s) "
        f"({scope}: {undo_scope_words(scope)})"
    )
    if turns:
        lines.append(f"  turns: {turns}")
    for item in restored[:12]:
        lines.append(
            f"  restored {item.get('path')!s} "
            f"({item.get('action')!s}, hash {str(item.get('after_hash'))[:12]})"
        )
    if len(restored) > 12:
        lines.append(f"  ... and {len(restored) - 12} more restored path(s)")
    for item in refused[:8]:
        lines.append(f"  NOT restored {item.get('path')!s}: {item.get('reason')!s}")
    if len(refused) > 8:
        lines.append(f"  ... and {len(refused) - 8} more refusal(s)")
    for item in excluded[:8]:
        lines.append(f"  excluded {item.get('path')!s}: {item.get('reason')!s}")
    if len(excluded) > 8:
        lines.append(f"  ... and {len(excluded) - 8} more exclusion(s)")
    conversation = receipt.get("conversation")
    if isinstance(conversation, Mapping):
        lines.append(
            f"  conversation: {conversation.get('action') or 'unknown'!s}"
            + (
                f" ({conversation.get('reason')!s})"
                if conversation.get("reason")
                else ""
            )
        )
    lines.append(
        "  verified: "
        + (
            "yes, every restored path hashes to its recorded pre-image"
            if receipt.get("verified")
            else "NO - see the refusals above"
            if refused
            else "NO - nothing was checked, so nothing was verified"
        )
    )
    receipt_id = str(receipt.get("receipt_id") or "")
    if receipt_id:
        lines.append(f"  receipt: {receipt_id}")
    if receipt.get("forced"):
        lines.append(
            "  forced: yes - "
            + str(len(receipt.get("overwritten_user_edits") or []))
            + " user edit(s) were overwritten on purpose"
            + (
                ": "
                + ", ".join(
                    str(item.get("path") or "")
                    for item in (receipt.get("overwritten_user_edits") or [])
                    if isinstance(item, Mapping)
                )
                if receipt.get("overwritten_user_edits")
                else ""
            )
        )
    return lines


def _file_digest(path: Optional[Path]) -> Optional[str]:
    data = _read_bytes(path, limit=16 * 1024 * 1024)
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# VEX-PF-05: the four public primitives the review surface builds on.
#
# They are WRAPPERS, not re-implementations. The path guard, the byte reader
# and the digest are the ones every other surface in this module already used,
# so a second traversal check or a second hashing rule would be a second way
# to be wrong about the same file. ``cli/review.py`` reaches the safety
# properties through these names rather than importing the underscore forms.
# ---------------------------------------------------------------------------


def repo_root(value: Any) -> Optional[Path]:
    """Return the resolved repository root for ``value``, or ``None``.

    Total: an unusable value is ``None``, never an exception. A caller that
    gets ``None`` must not treat the workspace as real.
    """
    return _root(value)


def safe_repo_path(root: Any, value: Any) -> Optional[Path]:
    """Resolve a repository-relative path, refusing traversal and symlinks.

    ``None`` means the path is unsafe, absolute, escaping the root, or
    carries a symlinked component. A caller that ignores the ``None`` and
    writes anyway is the one bug this function exists to make impossible.
    """
    return _safe_path(_root(root), value)


def content_hash(path: Any) -> Optional[str]:
    """SHA-256 of a file's bytes, or ``None`` when it cannot be read.

    ``None`` is deliberately ambiguous between "absent" and "unreadable": a
    restore that cannot hash its target must refuse either way, and a
    receipt that separated them would invite a caller to treat one as the
    other. Callers that must distinguish use :func:`path_exists`.
    """
    try:
        candidate = Path(str(path)).expanduser()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return _file_digest(candidate)


def path_exists(path: Any) -> bool:
    """Whether ``path`` is a regular file that is not a symlink."""
    try:
        candidate = Path(str(path)).expanduser()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    try:
        return candidate.is_file() and not candidate.is_symlink()
    except OSError:
        return False


def atomic_write_bytes(path: Any, data: bytes) -> bool:
    """Write ``data`` to ``path`` atomically, refusing a symlinked target.

    THE repository's one atomic byte-write primitive: a unique temp name in
    the destination directory, ``fsync`` before the rename, then
    ``os.replace``. Two inline copies of this shape existed in this module
    (``_atomic_json`` and ``redo_apply``); the review surface needed a third
    and forked copies are how they drift, so all three now call this.

    Returns ``True`` only when the rename landed. A ``False`` leaves the
    destination BYTE-IDENTICAL, which is the property a revert receipt
    depends on.
    """
    try:
        target = Path(str(path)).expanduser()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    if target.is_symlink():
        return False
    temporary_name: str = ""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(bytes(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        return True
    except (OSError, TypeError, ValueError):
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
        return False


def _atomic_json(path: Path, value: Mapping[str, Any]) -> bool:
    if path.is_symlink():
        return False
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return atomic_write_bytes(path, payload)


def capture_undo_receipt(
    task_dir: Any,
    repo: Any,
    paths: Sequence[Any],
    *,
    post_root: Any = None,
) -> bool:
    """Persist pre-undo bytes and hashes so redo can detect later conflicts."""
    root = _root(repo)
    try:
        directory = Path(str(task_dir)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    receipt = directory / "redo.json"
    if root is None or (receipt.is_file() and not receipt.is_symlink()):
        return True
    original_root = directory / "orig"
    post_undo_root = _root(post_root) if post_root is not None else original_root
    records: List[Dict[str, Any]] = []
    for value in paths:
        relative = _relative(
            value
            if not isinstance(value, Mapping)
            else value.get("path") or value.get("file")
        )
        if not relative:
            continue
        target = _safe_path(root, relative)
        original = (
            _safe_path(post_undo_root, relative) if post_undo_root is not None else None
        )
        if target is None and not (root / relative).exists():
            continue
        if original is None and post_undo_root is None:
            continue
        current_bytes = _read_bytes(target)
        records.append(
            {
                "path": relative,
                "existed": current_bytes is not None,
                "content": base64.b64encode(current_bytes or b"").decode("ascii"),
                "pre_undo_hash": _file_digest(target),
                "post_undo_hash": _file_digest(original),
                "pre_undo_exists": target is not None and target.is_file(),
                "post_undo_exists": original is not None and original.is_file(),
            }
        )
    if not records:
        return False
    return _atomic_json(
        receipt,
        {
            "version": 2,
            "task_id": directory.name,
            "repo": str(root),
            "created_at": time.time(),
            "files": records,
        },
    )


def _read_redo_receipt(task_dir: Any) -> Tuple[Optional[Path], Dict[str, Any]]:
    try:
        directory = Path(str(task_dir)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, {}
    receipt = directory / "redo.json"
    if not receipt.is_file() or receipt.is_symlink():
        return None, {}
    try:
        value = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return receipt, {}
    return receipt, dict(value) if isinstance(value, Mapping) else {}


def _receipt_preflight(
    task_dir: Any, repo: Any, phase: str
) -> Tuple[Optional[Path], Dict[str, Any], List[Dict[str, Any]]]:
    root = _root(repo)
    receipt, payload = _read_redo_receipt(task_dir)
    conflicts: List[Dict[str, Any]] = []
    if root is None or receipt is None:
        return receipt, payload, conflicts
    records = payload.get("files") if isinstance(payload.get("files"), list) else []
    for value in records:
        if not isinstance(value, Mapping):
            continue
        relative = _relative(value.get("path"))
        target = _safe_path(root, relative)
        if not relative or (target is None and not (root / relative).exists()):
            conflicts.append(
                {
                    "path": relative or str(value.get("path") or ""),
                    "reason": "unsafe redo path",
                }
            )
            continue
        expected_key = "pre_undo_hash" if phase == "undo" else "post_undo_hash"
        actual = _file_digest(target)
        expected = value.get(expected_key)
        if actual != expected:
            conflicts.append(
                {
                    "path": relative,
                    "reason": "workspace changed after the recorded operation",
                    "expected_hash": expected,
                    "actual_hash": actual,
                }
            )
    return receipt, payload, conflicts


def undo_preflight(
    task_dir: Any,
    repo: Any,
    paths: Sequence[Any],
    *,
    post_root: Any = None,
) -> Dict[str, Any]:
    """Check that an undo receipt still describes the current workspace."""
    receipt, _payload, conflicts = _receipt_preflight(task_dir, repo, "undo")
    if receipt is None:
        created = capture_undo_receipt(task_dir, repo, paths, post_root=post_root)
        return {"ok": bool(created), "status": "ready" if created else "unavailable"}
    if conflicts:
        return {"ok": False, "status": "conflict", "conflicts": conflicts}
    return {"ok": True, "status": "ready"}


def redo_apply(task_dir: Any, repo: Any) -> Dict[str, Any]:
    """Reapply a task-local undo receipt only when every target hash matches."""
    root = _root(repo)
    receipt, payload, conflicts = _receipt_preflight(task_dir, repo, "redo")
    if root is None or receipt is None or not payload:
        return {"outcome": "nothing", "files": [], "diff": None}
    if conflicts:
        return {
            "outcome": "conflict",
            "files": [],
            "diff": None,
            "conflicts": conflicts,
        }
    records = payload.get("files") if isinstance(payload.get("files"), list) else []
    changed: List[str] = []
    for value in records:
        if not isinstance(value, Mapping):
            continue
        relative = _relative(value.get("path"))
        target = _safe_path(root, relative)
        if not relative or (target is None and not (root / relative).exists()):
            return {
                "outcome": "conflict",
                "files": [],
                "diff": None,
                "conflicts": [{"path": relative, "reason": "unsafe redo path"}],
            }
        try:
            if bool(value.get("existed")):
                content = base64.b64decode(
                    str(value.get("content") or ""), validate=True
                )
                if not atomic_write_bytes(target, content):
                    return {
                        "outcome": "error",
                        "files": changed,
                        "diff": None,
                        "error": f"could not write {relative}",
                    }
            elif target.is_file() or target.is_symlink():
                target.unlink()
            changed.append(relative)
        except (OSError, ValueError, TypeError) as exc:
            return {
                "outcome": "error",
                "files": changed,
                "diff": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
    try:
        receipt.unlink()
    except OSError:
        pass
    diff = ""
    try:
        diff = current_diff_text(task_dir, root)
    except Exception:
        pass
    return {"outcome": "done", "files": changed, "diff": diff or None}


def checkpoint_records(
    repo: Any,
    log_root: Any,
    *,
    session_id: str = "",
    task_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return durable checkpoint metadata and journal receipts without mutation."""
    records: Dict[str, Dict[str, Any]] = {}
    if task_id:
        try:
            from cli.runview import read_checkpoints

            for record in read_checkpoints(Path(str(log_root)) / str(task_id)):
                value = dict(record)
                value.setdefault("source", "journal")
                records[
                    str(
                        value.get("checkpoint_id")
                        or value.get("resume_token")
                        or len(records)
                    )
                ] = value
        except Exception:
            pass
    manager = _manager(repo, log_root, session_id)
    if manager is not None:
        try:
            for value in manager.list_checkpoints():
                value = dict(value)
                value.setdefault("source", "durable")
                records[
                    str(
                        value.get("checkpoint_id")
                        or value.get("resume_token")
                        or len(records)
                    )
                ] = value
        except Exception:
            pass
    result = list(records.values())
    result.sort(key=lambda value: float(value.get("created_at") or 0.0), reverse=True)
    return result


def compare_checkpoint(
    repo: Any,
    log_root: Any,
    checkpoint_id: str,
    *,
    session_id: str = "",
) -> Dict[str, Any]:
    """Review one durable checkpoint and return its safe comparison record."""
    manager = _manager(repo, log_root, session_id)
    if manager is None:
        return {
            "checkpoint_id": checkpoint_id,
            "status": "unavailable",
            "changed": False,
            "files": [],
            "diff": "",
        }
    try:
        result = dict(manager.review_checkpoint(str(checkpoint_id)))
    except Exception as exc:
        return {
            "checkpoint_id": checkpoint_id,
            "status": "error",
            "changed": False,
            "files": [],
            "diff": "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    result.setdefault("checkpoint_id", checkpoint_id)
    result.setdefault("status", "conflict" if result.get("changed") else "unchanged")
    return result


def restore_selection_preflight(
    repo: Any,
    log_root: Any,
    checkpoint_id: str,
    files: Optional[Sequence[str]] = None,
    *,
    session_id: str = "",
) -> Dict[str, Any]:
    """Answer "could I restore just these files?" BEFORE the refusal.

    A whole-checkpoint restore is the only restore the underlying manager
    performs, and narrowing it to a subset would mean changing another
    module's contract. What the CLI CAN do — read-only, safely — is tell the
    user what a subset restore would have done, so ``restore_checkpoint``'s
    ``selection_not_supported`` refusal stops being a shrug.

    Each requested path resolves to one of four measured states:

    ``restorable``   the file is in the checkpoint and has not changed since
    ``conflicts``    the file changed AFTER the checkpoint; restoring would
                     discard an edit made since, so the whole-checkpoint
                     restore would refuse it too
    ``absent``       the file is not in the checkpoint at all
    ``unknown``      the checkpoint could not be reviewed; stated, not guessed

    Nothing here writes. ``force=True`` would bypass the conflict above, and
    that door belongs to the restore call, not to a preflight.
    """
    wanted = [_relative(item) for item in (files or []) if _relative(item)]
    review = compare_checkpoint(repo, log_root, checkpoint_id)
    rows: List[Dict[str, Any]] = []
    if review.get("status") in {"unavailable", "error", "not_found"}:
        return {
            "checkpoint_id": checkpoint_id,
            "ok": False,
            "status": str(review.get("status") or "unavailable"),
            "reason": str(
                review.get("error") or "the checkpoint could not be reviewed"
            ),
            "files": [],
            "restorable": [],
            "conflicts": [],
        }
    known = {
        _relative(item.get("path")): dict(item)
        for item in (review.get("files") or [])
        if isinstance(item, Mapping) and _relative(item.get("path"))
    }
    for path in wanted:
        record = known.get(path)
        if record is None:
            rows.append(
                {"path": path, "state": "absent", "detail": "not in this checkpoint"}
            )
            continue
        state = str(record.get("status") or "unknown")
        if state == "unchanged":
            rows.append(
                {
                    "path": path,
                    "state": "restorable",
                    "detail": "no edits since the checkpoint",
                }
            )
        else:
            rows.append(
                {
                    "path": path,
                    "state": "conflicts",
                    "detail": f"{state} since the checkpoint; restoring discards that edit",
                }
            )
    return {
        "checkpoint_id": checkpoint_id,
        "ok": True,
        "status": "ready",
        "reason": "",
        "files": rows,
        "restorable": [row["path"] for row in rows if row["state"] == "restorable"],
        "conflicts": [row["path"] for row in rows if row["state"] == "conflicts"],
    }


def restore_checkpoint(
    repo: Any,
    log_root: Any,
    checkpoint_id: str,
    *,
    files: Optional[Sequence[str]] = None,
    include_conversation: bool = False,
    force: bool = False,
    session_id: str = "",
) -> Dict[str, Any]:
    """Restore files, optionally with conversation, only through the safe manager API."""
    manager = _manager(repo, log_root, session_id)
    if manager is None:
        return {
            "status": "unavailable",
            "ok": False,
            "restored_files": [],
            "conflicts": [],
        }
    if files:
        preflight = restore_selection_preflight(
            repo, log_root, checkpoint_id, files, session_id=session_id
        )
        return {
            "status": "selection_not_supported",
            "ok": False,
            "restored_files": [],
            "conflicts": [
                {
                    "reason": "restore applies to the whole checkpoint; "
                    "narrowing it to a subset is a contract change in another "
                    "module and is not performed here",
                }
            ],
            "preflight": preflight,
        }
    try:
        result = manager.restore_checkpoint(
            str(checkpoint_id),
            restore_files=files is None or bool(files),
            restore_conversation=bool(include_conversation),
            force=bool(force),
        )
        return dict(result)
    except Exception as exc:
        return {
            "status": "error",
            "ok": False,
            "restored_files": [],
            "conflicts": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


# ---------------------------------------------------------------------------
# Editor handoff
# ---------------------------------------------------------------------------


def split_open_target(rest: str) -> Tuple[str, Optional[int]]:
    r"""Split a ``path[:line]`` string into ``(path, line)``.

    The trailing ``:line`` is only honored when the colon is not part of a
    Windows drive letter (``C:\``) and the remainder is a non-empty path.
    Returns ``(rest, None)`` when no line suffix is present.
    """
    text = str(rest or "").strip()
    if not text:
        return text, None
    match = re.search(r"(?<![:\\])(?::)(\d+)$", text)
    if match:
        return text[: match.start()], int(match.group(1))
    return text, None


def launch_editor(
    file_path: str | Path, line: int | None = None, editor: str | None = None
) -> List[str]:
    """Resolve the exact editor command line, platform by platform.

    Priority: ``$VISUAL`` > ``$EDITOR`` > platform default
    (``code`` + ``-g`` on Windows, ``vi`` on POSIX).  An environment
    value may carry its own arguments (``code --wait``); they are split
    with shlex and kept.  When the resolved program is VS Code (``code``,
    ``code.cmd`` or ``code.exe``), ``-g`` is inserted before the file
    path and ``:line`` is appended for the editor to jump to.  This
    function only returns the argv so tests can pin the exact invocation
    per platform; the caller detaches the process via
    ``launch_editor_detached``.
    """
    program = (
        str(editor)
        if editor is not None
        else os.environ.get("VISUAL") or os.environ.get("EDITOR")
    )
    argv = shlex.split(program) if program else list(_platform_default())
    if not argv:
        argv = list(_platform_default())
    base = str(argv[0])
    is_code = base.lower() in {"code", "code.cmd", "code.exe"}
    args: List[str] = []
    if is_code:
        args.append("-g")
    args.extend(argv[1:])
    args.append(str(file_path))
    if line is not None:
        args.append(f":{line}" if is_code else f"+{line}")
    return [base, *args]


def launch_editor_detached(
    file_path: str | Path,
    line: int | None = None,
    editor: str | None = None,
) -> subprocess.CompletedProcess:
    """Launch the editor in a detached process so the terminal keeps
    ownership.  Never raises: on failure it returns the failed result and
    the caller reports the honest fallback."""
    argv = launch_editor(file_path, line, editor)
    try:
        if os.name == "nt":
            return subprocess.run(
                argv,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                capture_output=True,
                text=True,
                timeout=_DETACHED_TIMEOUT,
            )
        return subprocess.run(
            argv,
            start_new_session=True,
            capture_output=True,
            text=True,
            timeout=_DETACHED_TIMEOUT,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(
            argv, returncode=-1, stdout="", stderr=f"{type(exc).__name__}: {exc}"
        )


# ---------------------------------------------------------------------------
# Read-only review + working-tree identity
# ---------------------------------------------------------------------------


def review_worktree(repo: Path, changed_only: bool = True) -> Dict[str, Any]:
    """Return a SHA-256 over the *read-only* working tree plus its diff
    summary.  The function never writes or applies anything: it hashes
    the working tree and, when the repository is a git worktree, also
    hashes the pristine/index state so a reviewer can confirm that no
    file was mutated during review.

    ``changed_only=True`` (the daily default) hashes only paths listed
    in ``git status --porcelain``; ``False`` hashes the whole tree, so a
    reviewer that runs in pristine/outside the repo gets a stable
    fingerprint.
    """
    root = Path(repo).resolve()
    status = read_git_status(root)
    changed = list(status.keys()) if changed_only else sorted(p for p in status)
    hasher = hashlib.sha256()
    for path in changed:
        candidate = root / path
        if not candidate.is_file():
            continue
        try:
            hasher.update(candidate.read_bytes())
        except OSError:
            continue
    return {
        "repo": str(root),
        "changed_files": changed,
        "working_tree_sha256": hasher.hexdigest(),
        "baseline_sha256": _baseline_sha256(root),
        "changed_count": len(changed),
    }


def _baseline_sha256(root: Path) -> str:
    """SHA-256 over the pristine reference tree (the review baseline)."""
    hasher = hashlib.sha256()
    pristine = root / "pristine"
    if pristine.is_dir():
        for dirpath, dirnames, filenames in os.walk(str(pristine)):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in sorted(filenames):
                p = Path(dirpath, name)
                try:
                    hasher.update(p.read_bytes())
                except OSError:
                    continue
    return hasher.hexdigest()


def review_scope(repo: Path, scope: str, ref: str = "") -> Dict[str, Any]:
    """Read-only review presets for uncommitted, branch, SHA, and PR.

    Each scope returns a diff summary plus the ref it derived from —
    nothing below writes to the working tree or the index.  ``sha``
    requires ``ref`` (a commit or range) and says so honestly when it is
    missing; ``pr`` resolves ``git merge-base`` against the configured
    upstream branch and reports honestly when there is no upstream.
    """
    root = Path(repo).resolve()
    status = read_git_status(root)
    changed = sorted(
        p for p, info in status.items() if str(info.get("status", " ")).strip()
    )
    if not changed:
        changed = sorted(status)
    if scope == "uncommitted":
        diff = _git_diff(root, changed=changed)
        record = review_worktree(root)
        return {
            "scope": "uncommitted",
            "changed_files": changed,
            "additions": _count(diff, "+"),
            "deletions": _count(diff, "-"),
            "working_tree_sha256": record["working_tree_sha256"],
            "baseline_sha256": record["baseline_sha256"],
        }
    if scope == "branch":
        head = _git_commit(root, "HEAD")
        diff = _git_diff(root, commit="HEAD", changed=changed)
        return {
            "scope": "branch",
            "changed_files": changed,
            "additions": _count(diff, "+"),
            "deletions": _count(diff, "-"),
            "head": head,
        }
    if scope == "sha":
        target = str(ref or "").strip()
        if not target:
            return {
                "scope": "sha",
                "status": "needs-ref",
                "reason": "the sha scope needs a commit or range",
                "usage": "/review sha <ref>",
            }
        diff = _git_show(root, target, changed=changed)
        if not diff:
            return {
                "scope": "sha",
                "status": "unknown-ref",
                "reason": f"git show produced no output for {target!r}",
                "ref": target,
            }
        return {
            "scope": "sha",
            "status": "ok",
            "changed_files": changed,
            "additions": _count(diff, "+"),
            "deletions": _count(diff, "-"),
            "ref": target,
        }
    if scope == "pr":
        upstream = _upstream_ref(root)
        if not upstream:
            return {
                "scope": "pr",
                "status": "no-upstream",
                "reason": "this branch has no upstream tracking ref",
                "usage": "git push -u origin <branch>, or use /review branch",
            }
        merge_base = _git_merge_base(root, upstream)
        diff = _git_diff(root, commit_a=merge_base, commit_b=upstream, changed=changed)
        return {
            "scope": "pr",
            "status": "ok",
            "changed_files": changed,
            "additions": _count(diff, "+"),
            "deletions": _count(diff, "-"),
            "base": merge_base,
            "head": upstream,
        }
    return {
        "scope": scope,
        "status": "unknown-scope",
        "reason": f"unknown review scope: {scope}",
    }


def _git_diff(
    root: Path,
    commit: str = "",
    commit_a: str = "",
    commit_b: str = "",
    changed: Sequence[str] = (),
) -> str:
    cmd = ["git", "-C", str(root), "diff"]
    if commit_a and commit_b:
        cmd += ["--no-color", commit_a, commit_b]
    elif commit:
        cmd += ["--no-color", commit]
    elif commit_a:
        cmd += ["--no-color", commit_a]
    if changed:
        cmd += ["--", *list(changed)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0)
    return proc.stdout or ""


def _git_show(root: Path, sha: str, changed: Sequence[str] = ()) -> str:
    cmd = ["git", "-C", str(root), "show", "--no-color", sha]
    if changed:
        cmd += ["--", *list(changed)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0)
    return proc.stdout or ""


def _count(diff: str, prefix: str) -> int:
    return sum(1 for line in diff.splitlines() if line.startswith(prefix))


def _git_commit(root: Path, commit: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", commit],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    return proc.stdout.strip() or ""


def _upstream_ref(root: Path) -> str:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{u}",
        ],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _git_merge_base(root: Path, ref: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "merge-base", "HEAD", ref],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    return proc.stdout.strip() or ""


# ---------------------------------------------------------------------------
# Failure classification + recovery vocabulary
# ---------------------------------------------------------------------------


#: One recovery policy per authoritative error kind. The kind vocabulary is
#: owned by ``harness.tool_errors`` (tool/command failures) and
#: ``KIND_MODEL_*`` (provider failures) — this table only says what the user
#: can DO next, so a refusal and a failure speak the same language instead of
#: each inventing its own.
_RECOVERY_ACTIONS_BY_KIND: Mapping[str, Tuple[str, ...]] = {
    "timeout": (
        "narrow the command scope, then re-run with a larger bound",
        "inspect /trace for the last completed step",
    ),
    "command_not_found": (
        "confirm the binary is on PATH in this environment",
        "re-run with an available tool",
    ),
    "file_not_found": (
        "list the repository with /files",
        "re-run from the correct working directory",
    ),
    "malformed_patch": (
        "re-read the file and regenerate the patch against current content",
        "reject the hunk with /review, then re-apply",
    ),
    "syntax_error": (
        "read the exact current file with line numbers (/open path:line)",
        "re-run the formatter before retrying",
    ),
    "import_error": (
        "confirm the module path and that the package is installed",
        "inspect /doctor for the dependency lane",
    ),
    "undefined_name": (
        "read the symbol's real definition (/files, then /open)",
        "re-run the same step after the import lands",
    ),
    "permission_denied": (
        "do not retry this path",
        "re-check the policy scope with /settings",
    ),
    "argument_error": (
        "re-read the command's documented flags (/help <command>)",
        "re-run with corrected arguments",
    ),
    "command_rejected": (
        "the deny guard refused this command — rephrase it",
        "inspect /trace for the rejected command body",
    ),
    "model_rate_limited": (
        "wait for the rate-limit window, then retry",
        "fall back to the configured provider",
    ),
    "model_unavailable": (
        "fall back to the configured provider",
        "check /doctor for provider reachability",
    ),
    "model_timeout": (
        "retry once with a reduced scope",
        "fall back to the configured provider",
    ),
    "model_auth": (
        "re-authenticate with /login",
        "verify the key with /doctor",
    ),
    "model_bad_request": (
        "verify the model name and endpoint with /model",
        "inspect /doctor for the effective provider settings",
    ),
    "model_internal": (
        "inspect /trace for the failing request",
        "retry the step once",
    ),
    "verification_failed": (
        "preserve the evidence in /trace",
        "re-plan around the failing test",
    ),
    "internal_error": (
        "inspect /trace for the full command output",
        "re-run the command once; if it repeats, read the raw output",
    ),
    "unknown": (
        "inspect /trace for the full command output",
        "resume with the recorded evidence",
    ),
}

#: Provider-shaped failures are routed to the MODEL classifier rather than the
#: command classifier. The anchor is deliberately a provider exception CLASS
#: name or an explicit HTTP status, not loose prose: "not found" alone is the
#: single most ambiguous phrase in a terminal (a missing FILE and a missing
#: MODEL both say it), and guessing wrong would send a file error to the
#: provider recovery policy.
_MODEL_FAILURE_RE = re.compile(
    r"(?:^|\W)(?:RateLimit|Authentication|PermissionDenied|BadRequest|"
    r"ServiceUnavailable|InternalServer|APIConnection|APIError|"
    r"ContextWindowExceeded|NotFound|Overloaded|Timeout)\w*(?:Error|Exception)?"
    r"\s*:|"
    r"\b429\b|\b5(?:00|02|03|04)\b|\b401\b|\b403\b|\b400\b|\b404\b|"
    r"litellm\.",
    re.I,
)

#: Canonical exception class names the model classifier recognizes. The
#: model classifier keys off the exception CLASS (a harness TypeError must
#: never look like a provider outage), so re-entering captured provider TEXT
#: has to re-enter it as an exception carrying a provider-shaped name rather
#: than a bare RuntimeError, which would classify as `model_internal`.
_MODEL_EXCEPTION_NAMES: Tuple[Tuple[str, str], ...] = (
    (r"rate[ _-]?limit|too many requests|\b429\b", "RateLimitError"),
    (
        r"invalid api key|unauthorized|authentication|\b401\b|\b403\b",
        "AuthenticationError",
    ),
    (r"context[ _-]?window|too many tokens", "ContextWindowExceededError"),
    (
        r"serviceunavailable|service unavailable|overloaded|no available channel",
        "ServiceUnavailableError",
    ),
    (r"bad gateway|gateway timeout|\b502\b|\b503\b|\b504\b", "InternalServerError"),
    (r"connectionerror|apierror|connection", "APIConnectionError"),
    (r"internal server error|\b500\b", "InternalServerError"),
    (r"timed?\s?out|timeout", "Timeout"),
    (r"bad request|invalid_request_error|\b400\b", "BadRequestError"),
    (r"not found|unknown model|\b404\b", "NotFoundError"),
)


def _model_exception_for(text: str) -> BaseException:
    """Wrap provider text in an exception whose CLASS the model classifier
    recognizes, so its own precedence and retryability rules apply."""
    for pattern, name in _MODEL_EXCEPTION_NAMES:
        if re.search(pattern, text, re.I):
            return type(name, (Exception,), {})(text)
    return type("APIConnectionError", (Exception,), {})(text)


def classify_failure(
    command: str, stdout: str, stderr: str, output: str = ""
) -> Dict[str, str]:
    """Classify one failure into the shared recovery vocabulary.

    The kind is NOT invented here: tool/command failures are classified by
    ``harness.tool_errors.classify`` and provider failures by
    ``harness.tool_errors.classify_model_failure``, so the recovery card, a
    refusal, and the harness's own retry policy all name the same thing.
    ``verification_failed`` is the one class the command classifier does not
    know, so it is applied ONLY after the authoritative classifier falls
    through to its own catch-all.
    """
    combined = f"{stdout}\n{stderr}\n{output}".strip()
    kind = "unknown"
    detail = ""
    hint: Optional[str] = None
    try:
        from harness import tool_errors

        if _MODEL_FAILURE_RE.search(combined):
            model_failure = tool_errors.classify_model_failure(
                _model_exception_for(combined)
            )
            kind = str(model_failure.kind)
            detail = str(getattr(model_failure, "detail", "") or "")
            hint = getattr(model_failure, "hint", None)
        else:
            error = tool_errors.classify(
                124 if _TIMEOUT_RE.search(combined) else 1,
                stdout or "",
                stderr or output or "",
                bool(_TIMEOUT_RE.search(combined)),
                command or "",
            )
            kind = str(error.kind)
            detail = str(error.detail or "")
            hint = error.hint
            if kind in {"internal_error", "ok"} and _VERIFICATION_FAILURE_RE.search(
                combined
            ):
                kind = "verification_failed"
    except Exception:
        kind = "unknown"
    if kind in {"ok", ""}:
        kind = "unknown"
    return {
        "kind": kind,
        "detail": detail,
        "hint": hint or "",
        "evidence_path": _evidence_path(command, stdout, stderr, output),
        "actions": list(
            _RECOVERY_ACTIONS_BY_KIND.get(kind, _RECOVERY_ACTIONS_BY_KIND["unknown"])
        ),
    }


#: The one class the command classifier does not know. Deliberately
#: pytest-shaped: a bare "failed" also appears in "patch failed", "build
#: failed", and "0 failed", and claiming those are VERIFICATION failures
#: would send a patch error to the wrong recovery policy.
_VERIFICATION_FAILURE_RE = re.compile(
    r"verification failed"
    r"|^\s*(?:FAILED|ERROR)\s+\S+::"
    r"|assertionerror"
    r"|\b\d+ failed\b"
    r"|\b(?:1|2|3)?\d* tests? failed\b",
    re.I | re.M,
)
_TIMEOUT_RE = re.compile(r"timed?\s?out|timeout|deadline exceeded", re.I)


def _evidence_path(command: str, stdout: str, stderr: str, output: str) -> str:
    candidate = Path(str(command).strip())
    if candidate.exists():
        return os.path.realpath(candidate)
    for text in (stdout, stderr, output):
        match = re.search(r"(/[^\s]+\.(?:py|js|ts|md|toml|txt))", str(text))
        if match:
            return str(Path(match.group(1)))
    return os.path.realpath(Path.cwd() / "logs" / "diagnostics.txt")


# ---------------------------------------------------------------------------
# Discoverability: searchable help + grouped command registry
# ---------------------------------------------------------------------------


def help_search(command_registry: Any = None, query: str = "") -> List[Dict[str, Any]]:
    """Search the built-in command registry the way a daily user searches
    ``/help``: substring (case-insensitive) over command name and summary,
    returning the matching spec names plus their one-line summary.

    The canonical implementation is over the COMMAND_SPECS registry, so a
    branded help browser renders identical results to the REPL and TUI.
    """
    registry = command_registry or _default_registry()
    needle = str(query or "").strip().casefold()
    if not needle:
        return []
    results: List[Dict[str, Any]] = []
    for spec in registry:
        haystack = f"{spec.name} {spec.summary}".casefold()
        if needle in haystack:
            results.append(
                {
                    "name": spec.name,
                    "summary": spec.summary,
                    "argument_hint": spec.argument_hint,
                    "palette_behavior": spec.palette_behavior,
                    "match": needle,
                }
            )
    results.sort(key=lambda item: item["name"])
    return results


def _default_registry() -> Any:
    from cli import commands as _commands

    return _commands.COMMAND_SPECS


# ---------------------------------------------------------------------------
# Repo switching: effective-settings diff + file-cache reload
# ---------------------------------------------------------------------------


def settings_effective_diff(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Return the key-by-key effective-settings diff between two merged
    settings dicts, used by the repo switch path to print what changed.

    Secret-bearing keys (``api_key``, ``api_base``/``base_url``) are masked
    the same way ``neo config list`` masks them, so a repo switch can
    never print a credential.  Every other value is shown verbatim: a
    truncated model name is a lie, and this diff is how the user verifies
    the switch.
    """
    keys = set(before) | set(after)
    diff: List[Dict[str, Any]] = []
    for key in sorted(keys):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            diff.append(
                {
                    "key": key,
                    "before": _public_value(key, old),
                    "after": _public_value(key, new),
                    "source": None,
                }
            )
    return diff


#: settings keys whose values must never be printed verbatim.
_SECRET_SETTING_KEYS = frozenset({"api_key", "api_base", "base_url"})


def _public_value(key: str, value: Any) -> Any:
    """Mask secret settings values; leave every other value readable."""
    if str(key or "").lower() in _SECRET_SETTING_KEYS and value:
        try:
            from cli.neoconfig import public_value

            rendered = public_value(key, value)
        except Exception:
            rendered = "***"
        return rendered if rendered is not None else "***"
    if isinstance(value, dict):
        return {k: _public_value(k, v) for k, v in value.items()}
    return value


def reload_repo_settings(repo: Path, state: Dict[str, Any]) -> Dict[str, Any]:
    """Reload project settings, model/provider, log root, and file caches
    for a new repository, then return the effective-settings diff so the
    caller prints exactly what the switch changed.

    The BEFORE side is resolved against the *previous* repository, not
    against the process CWD: ``merged_settings()`` with no argument reads
    whatever project tier the CWD happens to be in, so using it here would
    report a spurious diff on the first switch and none at all afterwards.
    """
    from cli import neoconfig
    from memory.paths import default_logs_dir

    old_repo = state.get("repo")
    before = neoconfig.merged_settings(start=Path(old_repo)) if old_repo else {}
    if old_repo and str(old_repo) != str(repo):
        clear_file_caches()
    state["repo"] = str(repo)
    new_effective = neoconfig.merged_settings(start=Path(repo))
    state["file_config"] = new_effective
    try:
        from cli.session import resolve_artifact_root

        artifacts = resolve_artifact_root(None, Path(repo), new_effective)
        log_root = str(artifacts.get("log_root") or default_logs_dir())
    except Exception:
        log_root = str(default_logs_dir())
    diff = settings_effective_diff(before, new_effective)
    return {
        "repo": str(repo),
        "effective_diff": diff,
        "log_root": log_root,
        "model": new_effective.get("model"),
        "provider": new_effective.get("provider"),
        "changed": any(
            row["key"] in {"model", "provider", "api_key", "base_url", "log_verbosity"}
            for row in diff
        ),
    }


def clear_file_caches() -> Dict[str, Any]:
    """Invalidate any repository file caches held by this module.

    ``fileview`` is a pure read layer and holds no module-level caches by
    default; the interactive and TUI layers keep per-repo, per-app caches
    (for example the palette file list) that are cleared by those
    layers.  Callers that keep their own caches should clear them when
    ``state["repo"]`` changes.
    """
    return {"cleared": True}
