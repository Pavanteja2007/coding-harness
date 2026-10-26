"""The worktree lifecycle, made visible - because sessions can run in parallel.

Phase 4 lets two sessions run at once, and T3 makes ``runtime/worktrees.py``
load-bearing. That creates a data-loss hazard this surface exists to close: **a
user editing files in tree A while the agent works in tree B.** A user who
cannot see which tree their session is in will eventually save over the wrong
one, so the current tree is ALWAYS rendered, on every receipt, with no flag to
turn it off.

The other half is deliberate acts. Creating a worktree is explained before it
happens; cleanup confirms and then REPORTS what it did (``runtime`` requires
reporting and never silent pruning, and this surface is where that requirement
becomes visible); a merge-back is a visible sequence rather than a background
operation, including the concurrent-edit refusal T2 enforces; and switching
trees mid-session warns about uncommitted work.

Every git answer is read through one helper with an explicit unavailable state,
so "we could not ask git" never renders as "there were no changes".

    python -m cli.worktree_view --repo DIR [--root DIR] list
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from cli.ui import sanitize_text

__all__ = [
    "MERGE_STEPS",
    "WORKTREE_STATES",
    "WorktreeReport",
    "WorktreeRow",
    "assert_tree_always_visible",
    "cleanup_lines",
    "cleanup_receipt",
    "creation_briefing",
    "creation_lines",
    "current_tree_banner",
    "current_tree_lines",
    "merge_back_flow",
    "merge_back_lines",
    "merge_back_preview",
    "preview_lines",
    "read_worktrees",
    "reset_git_cache",
    "tree_switch_lines",
    "tree_switch_warning",
    "worktree_detail_lines",
    "worktree_list_lines",
]


def _line(value: Any) -> str:
    """Return one display line through the ONE sanitise entry point."""
    return sanitize_text(value)


def _git(repo: Any, *args: str) -> Optional[str]:
    """Return one git answer, or ``None`` when git could not be asked.

    ``None`` and ``""`` are DIFFERENT and both matter: ``""`` is git saying
    "no", ``None`` is "we could not look". Collapsing them is how a surface
    renders an unreadable tree as a clean one.

    Answers are MEMOISED per process. Measured on this tree under four-terminal
    load, one ``git status --porcelain`` over the real checkout costs about a
    second, and a full eleven-surface render pass asks git more than eighty
    times - so an unmemoised render loop spends its whole budget re-asking the
    same question. **A long-lived process (the TUI, the REPL) must call
    :func:`reset_git_cache` before re-rendering**, or it will show a stale
    tree; the cache is a snapshot, and the caller owns its freshness.
    """
    key = (str(repo or "."), args)
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo or "."),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _GIT_CACHE[key] = None
        return None
    if completed.returncode != 0:
        _GIT_CACHE[key] = None
        return None
    # rstrip, never strip: see cli/session_briefing.py for why the leading
    # column of a porcelain row is load-bearing.
    answer = (completed.stdout or "").rstrip()
    _GIT_CACHE[key] = answer
    return answer


#: Per-process git answer memo. Not a global in any sense that matters: it is
#: reset by :func:`reset_git_cache` and never read across processes.
_GIT_CACHE: Dict[Tuple[str, Tuple[str, ...]], Optional[str]] = {}


def reset_git_cache() -> None:
    """Drop every memoised git answer.

    A long-lived surface calls this before re-rendering so the tree it shows is
    the tree that exists now rather than the one that existed when the process
    started.
    """
    _GIT_CACHE.clear()


def _branch(repo: Any) -> str:
    """Return the current branch, ``"(detached HEAD)"``, or ``"unknown"``.

    Guarded by :func:`_is_git_repo` like every other reader here. An unguarded
    ``git status`` from a directory that merely sits inside a larger repository
    makes git walk that repository - measured on this host as a multi-minute
    hang from a temp directory - so the guard is a correctness AND a cost rule.
    """
    if not _is_git_repo(repo):
        return "unknown"
    answer = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if answer is None:
        return "unknown"
    text = answer.strip()
    return text or "(detached HEAD)"


def _commit(repo: Any) -> str:
    """Return the short commit, or ``""``."""
    if not _is_git_repo(repo):
        return ""
    return (_git(repo, "rev-parse", "--short", "HEAD") or "").strip()


def _dirty_paths(repo: Any) -> Optional[List[str]]:
    """Return uncommitted paths, or ``None`` when they could not be established.

    ``None`` and ``[]`` are different and both matter: ``[]`` is "git says
    clean", ``None`` is "we could not ask, or the path is not a work tree".
    """
    if not _is_git_repo(repo):
        return None
    raw = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if raw is None:
        return None
    if not raw.strip():
        return []
    files: List[str] = []
    for line in raw.splitlines():
        entry = line[3:].strip() if len(line) > 3 else ""
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if entry:
            files.append(entry.replace("\\", "/"))
    return files


def _is_git_repo(repo: Any) -> bool:
    """Return whether a path is ITSELF the top level of a git work tree.

    The equality matters and is not a nicety. ``runtime.worktrees`` already
    requires it ("source repository must be the Git top level"), and the reason
    it is load-bearing here is measured: ``git status --porcelain`` from a
    directory that merely sits INSIDE some larger repository makes git walk that
    repository - on this host, from a temp directory, that was the whole user
    temp tree, and the call went from milliseconds to minutes. A subdirectory is
    not a tree, and reporting "this is a git work tree" for one is the same
    mistake that would then hang the render.
    """
    toplevel = _git(repo, "rev-parse", "--show-toplevel")
    if not toplevel:
        return False
    candidate = toplevel.strip()
    if not candidate:
        return False
    try:
        return os_equivalent(str(Path(repo or ".")), candidate)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

#: The states ``runtime.worktrees.WorktreeRecord.state`` can carry, plus the two
#: this surface derives. Declared so a renderer cannot silently accept a state
#: nobody defined.
WORKTREE_STATES: Tuple[str, ...] = (
    "ready",
    "creating",
    "conflict",
    "error",
    "missing",
    "clean",
    "dirty",
)

#: A merge-back is a SEQUENCE and is shown as one. Each step names what the user
#: would see, because a merge that happens in the background is a merge nobody
#: can interrupt.
MERGE_STEPS: Tuple[Tuple[str, str], ...] = (
    ("capture", "the child's patch is captured from its own worktree"),
    ("claim check", "every hunk is checked against the claims; an unclaimed edit is REFUSED"),
    ("apply", "the patch is applied to the integration worktree"),
    ("verify", "verification runs on the merged tree; a failure is not a merge"),
    ("report", "what merged, what was refused, and why - always, including when nothing merged"),
)


@dataclass
class WorktreeRow:
    """One worktree's visible state."""

    node_id: str
    path: str
    state: str
    branch: str = "unknown"
    commit: str = ""
    base_commit: str = ""
    created_at: str = ""
    changed_files: Tuple[str, ...] = ()
    dirty_unknown: bool = False
    exists: bool = True
    error: str = ""
    is_current: bool = False
    #: A worktree whose DIRECTORY is gone, or whose SESSION is gone. Neither is
    #: "still ready": P4/W2 measured a record carrying ``state: ready`` and
    #: ``exists: False`` rendering as a live worktree in a list, because nothing
    #: in the row said the thing it named no longer exists. A list of managed
    #: worktrees is a list of things a user may attach to, and offering a ghost
    #: is offering an action that cannot be taken.
    orphaned: bool = False
    orphan_reason: str = ""
    #: The session that created this worktree, when the store records one.
    session_id: str = ""

    @property
    def changed_count(self) -> int:
        """Return how many files this worktree changes, or ``-1`` if unknown."""
        return -1 if self.dirty_unknown else len(self.changed_files)

    @property
    def clean(self) -> bool:
        """Return whether this worktree has no uncommitted work.

        ``False`` when the answer is UNKNOWN **and when the worktree is
        orphaned**. A worktree whose status could not be read is not clean,
        because "clean" is a claim about bytes and the bytes were never looked
        at; and a worktree that no longer exists cannot be clean in any sense
        that could be acted on.
        """
        return (
            self.exists
            and not self.orphaned
            and not self.dirty_unknown
            and not self.changed_files
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection (additive keys only)."""
        return {
            "node_id": self.node_id,
            "path": self.path,
            "state": self.state,
            "branch": self.branch,
            "commit": self.commit,
            "base_commit": self.base_commit,
            "created_at": self.created_at,
            "changed_files": list(self.changed_files),
            "changed_count": self.changed_count,
            "clean": self.clean,
            "dirty_unknown": self.dirty_unknown,
            "exists": self.exists,
            "is_current": self.is_current,
            "orphaned": self.orphaned,
            "orphan_reason": self.orphan_reason,
            "session_id": self.session_id,
            "error": self.error,
        }


@dataclass
class WorktreeReport:
    """Every worktree this module can see, plus the current one."""

    repo: str = ""
    repo_is_git: bool = False
    current_path: str = ""
    current_branch: str = "unknown"
    current_commit: str = ""
    current_dirty: Optional[List[str]] = None
    rows: Tuple[WorktreeRow, ...] = ()
    source: str = ""
    source_available: bool = True

    @property
    def current(self) -> Optional[WorktreeRow]:
        """Return the row for the tree this session is IN, if it is managed."""
        for row in self.rows:
            if row.is_current:
                return row
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "repo": self.repo,
            "repo_is_git": bool(self.repo_is_git),
            "current_path": self.current_path,
            "current_branch": self.current_branch,
            "current_commit": self.current_commit,
            "current_dirty": (
                None if self.current_dirty is None else list(self.current_dirty)
            ),
            "source": self.source,
            "source_available": self.source_available,
            "rows": [row.to_dict() for row in self.rows],
        }


# ---------------------------------------------------------------------------
# Reading the managed worktree store
# ---------------------------------------------------------------------------


def _default_root(repo: Any) -> Path:
    """Return the managed worktree root for a repository."""
    return Path(repo or ".") / "logs" / "worktrees"


def read_worktrees(
    repo: Any, root: Any = None, *, live_sessions: Optional[Any] = None
) -> WorktreeReport:
    """Read every managed worktree and the tree this session is in.

    Reads ``<root>/worktrees.json``, which ``runtime.worktrees.WorktreeManager``
    owns. It does NOT construct a manager: construction creates the root
    directory, and a read-only surface must not create state.

    ``live_sessions`` is an optional collection of session ids that still exist.
    When supplied, a worktree whose store row names a session that is NOT in it
    is marked ``orphaned`` - the worktree's session is gone, so this is not a
    live session's checkout. A store that names no session is left alone: "no
    session was recorded" is UNKNOWN, not "the session is gone".

    **Git is asked once, and only when the path is a work tree.** Measured on
    this host: asking ``git status --porcelain --untracked-files=all`` from a
    directory that merely sits INSIDE some larger repository makes git walk
    that repository - here the whole user temp tree - and the call went from
    milliseconds to minutes. Three git calls guarded by one
    ``rev-parse --show-toplevel`` is both the honest answer and the cheap one.
    """
    repo_path = str(repo or ".")
    base = Path(root) if root is not None else _default_root(repo_path)
    store = base / "worktrees.json"
    is_git = _is_git_repo(repo_path)
    raw_toplevel = _git(repo_path, "rev-parse", "--show-toplevel") if is_git else None
    current_real = ""
    if raw_toplevel:
        try:
            current_real = str(Path(raw_toplevel.strip()).resolve())
        except OSError:
            current_real = ""
    report = WorktreeReport(
        repo=repo_path,
        repo_is_git=is_git,
        current_path=current_real,
        current_branch=_branch(repo_path) if is_git else "unknown",
        current_commit=_commit(repo_path) if is_git else "",
        current_dirty=_dirty_paths(repo_path) if is_git else None,
        source=str(store),
    )
    if not is_git:
        report.current_dirty = None
    if not store.exists():
        report.source_available = False
        return report
    try:
        data = json.loads(store.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        report.source_available = False
        return report
    entries = data.get("worktrees") if isinstance(data, Mapping) else None
    if not isinstance(entries, list):
        report.source_available = False
        return report

    rows: List[WorktreeRow] = []
    live = None if live_sessions is None else {str(item) for item in live_sessions}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        node_id = str(entry.get("node_id") or "")
        if not node_id:
            continue
        path = str(entry.get("path") or "")
        session_id = str(
            entry.get("session_id") or entry.get("parent_session_id") or ""
        )
        exists = bool(path) and Path(path).is_dir()
        orphaned = False
        reasons: List[str] = []
        if not exists:
            orphaned = True
            reasons.append(
                "the directory this worktree named is gone, so nothing can be "
                "attached to it and it is not a live checkout"
            )
        if live is not None and session_id and session_id not in live:
            # ACCUMULATED rather than replaced: a ghost can be both. The first
            # draft's single `elif` reported only the directory, so a reader
            # whose worktree was orphaned twice was told one of the two things
            # that mattered - and the session half is the one that explains WHY
            # the directory may have been cleaned up.
            orphaned = True
            reasons.append(
                f"the session that created it ({session_id}) is gone, so this is "
                "not a live session's tree; its work may still be on disk"
            )
        orphan_reason = " AND ".join(reasons)
        changed: Optional[List[str]] = None
        row_is_git = exists and _is_git_repo(path)
        if row_is_git:
            changed = _dirty_paths(path)
        rows.append(
            WorktreeRow(
                node_id=node_id,
                path=path,
                state=str(entry.get("state") or "ready"),
                branch=_branch(path) if row_is_git else ("unknown" if not exists else "not a git work tree"),
                commit=_commit(path) if row_is_git else "",
                base_commit=str(entry.get("base_commit") or ""),
                created_at=str(entry.get("created_at") or ""),
                changed_files=tuple(changed or ()) if changed is not None else (),
                dirty_unknown=changed is None,
                exists=exists,
                error=str(entry.get("last_error") or ""),
                is_current=bool(current_real and path and _same(path, current_real)),
                orphaned=orphaned,
                orphan_reason=orphan_reason,
                session_id=session_id,
            )
        )
    report.rows = tuple(rows)
    return report


def _same(left: str, right: str) -> bool:
    """Return whether two paths name the same directory."""
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return os_equivalent(left, right)


def os_equivalent(left: str, right: str) -> bool:
    """Case-insensitive path comparison, for a Windows checkout."""
    return str(left).replace("\\", "/").rstrip("/").lower() == str(
        right
    ).replace("\\", "/").rstrip("/").lower()


# ---------------------------------------------------------------------------
# The always-visible current tree
# ---------------------------------------------------------------------------


def current_tree_banner(report: Optional[WorktreeReport]) -> str:
    """Return ONE line naming the tree, for every receipt that is not the tree list.

    The brief's requirement is that the current tree is visible at ALL times, on
    every surface, in every mode, with no state in which it is hidden. Wave 1
    satisfied that on exactly one surface: ``worktree_list_lines`` called
    ``current_tree_lines``, and the detail, cleanup, merge-back and switch
    receipts did not. P4/W2 measured all four as absent, which means the
    ``/diff``-style operations - the ones that actually move bytes between trees
    - were the receipts with no tree on them.

    One line rather than the full block: a receipt about a cleanup should not
    carry a tree census, but it must never be the receipt that leaves a reader
    unsure which tree they are in.
    """
    if report is None:
        return (
            "CURRENT TREE: UNKNOWN - no report was supplied, so this receipt "
            "cannot say which tree it is acting on"
        )
    if not report.repo_is_git:
        return (
            f"CURRENT TREE: {_line(report.repo)} (not a git work tree, so there "
            "is no worktree to be in)"
        )
    dirty = report.current_dirty
    if dirty is None:
        state = "uncommitted work UNKNOWN"
    elif dirty:
        state = f"{len(dirty)} uncommitted path(s)"
    else:
        state = "clean"
    managed = report.current
    where = (
        f"managed as {managed.node_id}" if managed is not None else "the repo checkout"
    )
    return (
        f"CURRENT TREE: {_line(report.current_path)} on "
        f"{_line(report.current_branch or 'unknown')} ({where}, {state})"
    )


def assert_tree_always_visible(rendered: Mapping[str, Sequence[str]]) -> None:
    """Fail if any worktree receipt omits the current tree.

    The brief asks for an ASSERTION that there is no state in which the tree is
    hidden, and an assertion is a gate rather than a review note. It runs over
    whatever a caller rendered, keyed by surface name, so a NEW worktree surface
    is covered by construction rather than by remembering to add a line.

    A surface with NO lines is a failure too, because "it rendered nothing" is
    not "it did not need to say which tree".
    """
    missing = sorted(
        name
        for name, lines in rendered.items()
        if not any("CURRENT TREE" in str(line) for line in lines or ())
    )
    if missing:
        raise AssertionError(
            "these worktree receipts do not say which tree they act on: "
            + ", ".join(missing)
            + " - a user who cannot see the tree cannot know what they are about "
            "to change"
        )


def current_tree_lines(report: WorktreeReport) -> List[str]:
    """Render WHICH TREE this session is in. Always rendered; never optional.

    The brief calls a user editing tree A while the agent works in tree B a
    data-loss incident, so this block appears on every worktree receipt and has
    no flag to suppress it.
    """
    out: List[str] = ["CURRENT WORKTREE (this session works HERE):"]
    if not report.repo_is_git:
        out.append(
            f"  {_line(report.repo)} is NOT a git work tree, so there is no "
            "worktree to be in. Files edited here are edited directly."
        )
        return out
    managed = report.current
    out.append(f"  path:   {_line(report.current_path)}")
    out.append(
        f"  branch: {_line(report.current_branch)}   commit: "
        f"{_line(report.current_commit or 'unknown')}"
    )
    if managed is None:
        out.append(
            "  this tree is NOT a managed worktree - it is the repository's own "
            "checkout, shared with any other session running in it"
        )
    else:
        out.append(f"  managed as node: {_line(managed.node_id)}")
        out.append(f"  state: {_line(managed.state)}")
    dirty = report.current_dirty
    if dirty is None:
        out.append(
            "  uncommitted work: UNKNOWN - git could not be asked, which is NOT "
            "the same as clean"
        )
    elif dirty:
        out.append(
            f"  uncommitted work: {len(dirty)} path(s) - "
            f"{_line(', '.join(dirty[:8]))}"
        )
        if len(dirty) > 8:
            out.append(f"    ... and {len(dirty) - 8} more")
    else:
        out.append("  uncommitted work: none")
    if managed is not None and not managed.clean:
        out.append(
            "  !! this worktree is NOT clean. A second session running in it, or "
            "a tree switch now, would operate on uncommitted bytes."
        )
    return out


# ---------------------------------------------------------------------------
# Deliberate acts: create, clean up, merge back, switch
# ---------------------------------------------------------------------------


def creation_briefing(
    node_id: str, repo: Any, *, root: Any = None
) -> Dict[str, Any]:
    """Explain what creating a worktree WILL do, before it is created.

    Returns the briefing rather than rendering it so the caller that performs
    the creation can journal the same value it showed. Creating a detached
    worktree is cheap but not consequence-free: it pins a base commit, so work
    committed after that point is not in it.
    """
    base = Path(root) if root is not None else _default_root(repo)
    commit = _commit(repo)
    dirty = _dirty_paths(repo)
    return {
        "node_id": str(node_id),
        "will_create": str(base / str(node_id)),
        "pinned_base_commit": str(commit),
        "source_repo_is_dirty": bool(dirty),
        "source_repo_dirty_paths": list(dirty or ()),
        "explains": [
            "a NEW detached worktree is created at the pinned base commit; it is "
            "not a branch, so nothing is published",
            "work committed AFTER that commit is not in this worktree",
            "the SOURCE repository must be clean - runtime refuses a dirty "
            "checkout rather than silently omitting local changes",
            "removing it refuses while it has uncommitted work unless forced, and "
            "forcing is recorded",
        ],
        "refusals_you_may_hit": [
            "source repository has uncommitted changes",
            "worktree destination already exists",
        ],
    }


def creation_lines(briefing: Mapping[str, Any]) -> List[str]:
    """Render the creation briefing."""
    out: List[str] = [f"CREATING WORKTREE {_line(briefing.get('node_id'))} WILL:"]
    for item in briefing.get("explains") or ():
        out.append(f"  - {_line(item)}")
    out.append(
        f"  path: {_line(briefing.get('will_create'))}   pinned base: "
        f"{_line(briefing.get('pinned_base_commit') or 'unavailable')}"
    )
    if briefing.get("source_repo_is_dirty"):
        paths = list(briefing.get("source_repo_dirty_paths") or ())
        out.append(
            f"  !! the source repository is DIRTY ({len(paths)} path(s)); "
            "runtime will REFUSE the creation rather than omit local changes"
        )
    return out


def cleanup_receipt(
    node_id: str, report: WorktreeReport, *, force: bool = False, removed: Any = None
) -> Dict[str, Any]:
    """Describe a cleanup and CONFIRM it, then report what happened.

    ``removed`` is what actually happened, supplied by the caller that performed
    the removal. Absent it the receipt says ``not_attempted`` - never
    ``removed``. This is the surface where ``runtime``'s "report, never prune
    silently" requirement becomes visible to a person.
    """
    row = None
    for item in report.rows:
        if item.node_id == str(node_id):
            row = item
            break
    dirty = None if row is None else (None if row.dirty_unknown else list(row.changed_files))
    blocking = bool(dirty)
    # The COUNT the brief asks a cleanup to confirm with. Wave 1 computed
    # ``confirmation_required`` and then never rendered it, so the receipt said
    # "clean: False" and "uncommitted in this tree: 1 path(s)" while the field
    # that actually answers "will this ask me?" stayed invisible.
    paths_count = len(dirty) if dirty is not None else -1
    if row is None:
        question = "nothing to remove, so nothing to confirm"
    elif row.orphaned:
        question = (
            f"this worktree is ORPHANED ({row.orphan_reason}), so its directory "
            "may already be gone; confirm with the count below"
        )
    elif blocking:
        question = (
            f"CONFIRM: {paths_count} uncommitted path(s) would be lost. "
            "runtime REFUSES this without --force, and forcing overwrites them."
        )
    else:
        question = (
            f"this worktree is clean, so removing it loses no uncommitted work; "
            f"{paths_count} path(s) would be removed"
        )
    return {
        "node_id": str(node_id),
        "known": row is not None,
        "clean": (None if row is None else row.clean),
        "changed_files": dirty,
        "paths_count": paths_count,
        "confirmation_count": (0 if row is None else (1 if (blocking or row.orphaned) else 0)),
        "orphaned": bool(row.orphaned) if row is not None else False,
        "orphan_reason": (row.orphan_reason if row is not None else ""),
        "force": bool(force),
        "confirmation_required": bool(blocking or (row is not None and row.orphaned)),
        "confirmation_question": question,
        "warning": (
            "this worktree has uncommitted work; removing it without --force is "
            "REFUSED by runtime, and forcing it overwrites those bytes"
            if blocking
            else ""
        ),
        "outcome": "not_attempted",
        "removed": None if removed is None else sorted(str(item) for item in removed),
        "report": (
            "no removal was performed by this surface; the receipt reports the "
            "PLAN and states that no removal happened"
        ),
    }


def cleanup_lines(
    receipt: Mapping[str, Any], *, report: Optional[WorktreeReport] = None
) -> List[str]:
    """Render a cleanup confirmation and outcome."""
    out: List[str] = [current_tree_banner(report), f"CLEANUP {_line(receipt.get('node_id'))}:"]
    if not receipt.get("known"):
        out.append(
            "  no such managed worktree is recorded. Nothing would be removed, "
            "and nothing was."
        )
        return out
    out.append(f"  clean: {receipt.get('clean')}   force: {receipt.get('force')}")
    if receipt.get("paths_count", 0) >= 0:
        out.append(
            f"  paths at risk: {receipt.get('paths_count')} - "
            f"confirmations required: {receipt.get('confirmation_count')}"
        )
    else:
        out.append(
            "  paths at risk: UNKNOWN (the changed-file list could not be read), "
            f"so the confirmation count is UNKNOWN, not {receipt.get('confirmation_count')}"
        )
    if receipt.get("orphaned"):
        out.append(
            f"  !! ORPHANED: {_line(receipt.get('orphan_reason'))}. This is not a "
            "live session's tree."
        )
    if receipt.get("changed_files"):
        out.append(
            f"  uncommitted in this tree: {len(receipt['changed_files'])} path(s): "
            f"{_line(', '.join(list(receipt['changed_files'])[:8]))}"
        )
    out.append(f"  !! {_line(receipt.get('confirmation_question'))}")
    if receipt.get("warning"):
        out.append(f"  !! {_line(receipt['warning'])}")
    out.append(f"  outcome: {receipt.get('outcome')}")
    if receipt.get("removed") is None:
        out.append(f"  {_line(receipt.get('report'))}")
    else:
        out.append(f"  removed: {_line(', '.join(receipt['removed']) or 'nothing')}")
    return out


def merge_back_preview(
    *,
    node_id: str,
    source: Optional[WorktreeRow] = None,
    files: Sequence[Mapping[str, Any]] = (),
    refused: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Return exactly what a merge-back WILL change, before it changes anything.

    **A merge that cannot state what it will change does not get rendered as a
    merge.** Wave 1's ``merge_back_flow`` carried ``merged_files`` - a
    post-hoc list of what did land - and the three-session run's receipt showed
    ``merged files: 0`` under a heading reading MERGE-BACK, with nothing
    anywhere saying what had been proposed. That is the silent merge the brief
    forbids: a reader who cannot see the proposed change cannot consent to it.

    ``files`` is the proposed change as measured, one row per path:

    ``{"path": ..., "added": int, "removed": int, "binary": bool}``

    A row whose counts are unknown reports ``None`` rather than ``0``, because a
    zero added and zero removed reads as "no change" when it means "not
    measured". Refusals are folded in and counted separately, so the preview's
    total and its refusals are two numbers that cannot be added together by
    accident.
    """
    proposed: List[Dict[str, Any]] = []
    added = 0
    removed = 0
    unmeasured = 0
    for item in files or ():
        path = str(item.get("path") or "") if isinstance(item, Mapping) else str(item)
        if not path:
            continue
        plus = item.get("added") if isinstance(item, Mapping) else None
        minus = item.get("removed") if isinstance(item, Mapping) else None
        known = isinstance(plus, int) and isinstance(minus, int)
        if known:
            added += int(plus)
            removed += int(minus)
        else:
            unmeasured += 1
        proposed.append(
            {
                "path": path,
                "added": int(plus) if isinstance(plus, int) else None,
                "removed": int(minus) if isinstance(minus, int) else None,
                "binary": bool(item.get("binary")) if isinstance(item, Mapping) else False,
                "measured": known,
            }
        )
    return {
        "node_id": str(node_id),
        "source_path": (source.path if source is not None else ""),
        "source_clean": (source.clean if source is not None else None),
        "source_orphaned": bool(source.orphaned) if source is not None else False,
        "files": proposed,
        "file_count": len(proposed),
        "lines_added": (added if not unmeasured else None),
        "lines_removed": (removed if not unmeasured else None),
        "files_unmeasured": unmeasured,
        "refused_count": len(tuple(refused or ())),
        "stated": True,
    }


def preview_lines(preview: Mapping[str, Any]) -> List[str]:
    """Render the merge-back PREVIEW: what will change, before it changes."""
    out: List[str] = [f"WHAT THIS MERGE WILL CHANGE ({_line(preview.get('node_id'))}):"]
    files = list(preview.get("files") or ())
    if not files:
        out.append(
            "  nothing is proposed. A merge with no proposed change is not a "
            "merge, and this receipt does not claim one happened."
        )
    for item in files:
        if item.get("measured"):
            out.append(
                f"    - {_line(item.get('path'))}  +{item.get('added')}/"
                f"-{item.get('removed')} line(s)"
                + ("  (binary)" if item.get("binary") else "")
            )
        else:
            out.append(
                f"    - {_line(item.get('path'))}  line counts NOT MEASURED - "
                "this is not the same as no change"
            )
    unmeasured = int(preview.get("files_unmeasured") or 0)
    if unmeasured:
        out.append(
            f"    {unmeasured} file(s) have UNKNOWN line counts, so the total "
            "below is deliberately absent rather than zero"
        )
    else:
        out.append(
            f"    total: {preview.get('file_count')} file(s), "
            f"+{preview.get('lines_added')}/-{preview.get('lines_removed')} line(s)"
        )
    out.append(
        f"    refused before this point: {preview.get('refused_count')} - refusals "
        "are NOT part of the total above"
    )
    if preview.get("source_orphaned"):
        out.append(
            "    !! the source worktree is ORPHANED, so its patch may not be "
            "readable at all; nothing above could be measured from it"
        )
    return out


def merge_back_flow(
    *,
    node_id: str,
    source: Optional[WorktreeRow] = None,
    refused: Sequence[Mapping[str, Any]] = (),
    verified: Optional[bool] = None,
    merged_files: Sequence[str] = (),
    proposed: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return the merge-back as a visible sequence, refusals included.

    A concurrent edit that T2 refuses is the interesting case, so the refusals
    are FIRST in the receipt rather than appended: a reader who scrolls sees
    what was blocked before what went through.

    The PREVIEW is computed here rather than supplied, so a caller cannot
    render a merge receipt with no preview by forgetting an argument - the
    absence of a preview is a value this function produces, and
    :func:`merge_back_lines` refuses to present it as a merge.
    """
    rows = [dict(item) for item in (refused or ())]
    preview = merge_back_preview(
        node_id=node_id, source=source, files=(proposed or ()), refused=rows
    )
    return {
        "node_id": str(node_id),
        "steps": [{"step": step, "what": what} for step, what in MERGE_STEPS],
        "source_path": (source.path if source is not None else ""),
        "source_clean": (source.clean if source is not None else None),
        "preview": preview,
        "refusals": rows,
        "refusal_count": len(rows),
        "verified": verified,
        "merged_files": list(merged_files),
        "blocked": bool(rows) or verified is False,
        "explanation": (
            "a refused concurrent edit means the merge did NOT happen for the "
            "offending file; the refusal names the path, the line and the reason"
            if rows
            else "no refusal was recorded, which means no concurrent edit "
            "blocked this merge - not that one was checked for and none existed"
        ),
    }


def merge_back_lines(
    receipt: Mapping[str, Any], *, report: Optional[WorktreeReport] = None
) -> List[str]:
    """Render the merge-back flow: the tree, the preview, refusals, then the steps."""
    out: List[str] = [current_tree_banner(report)]
    out.append(f"MERGE-BACK {_line(receipt.get('node_id'))}:")
    preview = receipt.get("preview")
    if not isinstance(preview, Mapping):
        # Refuse rather than degrade. A merge receipt with no preview is the
        # silent merge; showing the steps without saying what they will do is
        # exactly that.
        out.append(
            "  !! THIS MERGE CANNOT BE PRESENTED: no preview of what will change "
            "was supplied, and a merge a reader cannot see is a silent merge. "
            "Nothing here should be applied."
        )
        return out
    out.extend(f"  {line}" for line in preview_lines(preview))
    if receipt.get("refusals"):
        out.append(
            f"  !! {receipt['refusal_count']} refusal(s) - the merge did NOT "
            "complete for the offending file(s):"
        )
        for item in receipt.get("refusals") or ():
            out.append(
                f"     - {_line(item.get('path'))}:{_line(item.get('line'))} "
                f"{_line(item.get('symbol') or '')} {_line(item.get('reason') or '')}".rstrip()
            )
    else:
        out.append("  no refusal was recorded")
    for step in receipt.get("steps") or ():
        out.append(f"  {step['step']}: {_line(step['what'])}")
    verified = receipt.get("verified")
    if verified is None:
        out.append(
            "  verification: NOT REPORTED. A merge with no verification result "
            "is not a verified merge."
        )
    else:
        out.append(f"  verification: {verified}")
    merged = list(receipt.get("merged_files") or ())
    out.append(
        f"  merged files: {len(merged)}"
        + (f" ({_line(', '.join(merged[:8]))})" if merged else "")
    )
    if not merged:
        out.append(
            "  nothing merged. The steps above are the FLOW; they are not a "
            "record that any of them ran."
        )
    out.append(f"  blocked: {receipt.get('blocked')}")
    return out


def tree_switch_warning(
    *,
    from_path: Any,
    to_path: Any,
    dirty: Optional[Sequence[str]] = None,
    doing: str = "",
) -> Dict[str, Any]:
    """Return what a mid-session tree switch risks, given uncommitted work.

    ``doing`` names what this session was IN THE MIDDLE OF. The brief asks a
    switch to warn "about what the agent was doing", and P4/W2 measured the
    absence: the pre-round warning knew the uncommitted path count and nothing
    about the live run, so the one sentence that mattered - *you are pulling the
    agent away from this* - had nowhere to go.
    """
    paths = None if dirty is None else [str(item) for item in dirty]
    activity = str(doing or "").strip()
    return {
        "from_path": str(from_path or ""),
        "to_path": str(to_path or ""),
        "uncommitted_known": paths is not None,
        "uncommitted_count": len(paths) if paths is not None else -1,
        "uncommitted_paths": list(paths or ()),
        "doing": activity,
        "doing_known": bool(activity),
        "warns": (
            "the work you have not committed stays in the tree you are leaving; "
            "switching does not carry it, stash it, or copy it"
            if paths
            else ""
        ),
        "activity_warns": (
            "switching trees does NOT stop, cancel or hand over this work; it "
            "leaves it running in the tree you are leaving, and nothing in the "
            "new tree knows about it"
            if activity
            else ""
        ),
        "unknown_is_not_clean": paths is None,
    }


def tree_switch_lines(
    warning: Mapping[str, Any], *, report: Optional[WorktreeReport] = None
) -> List[str]:
    """Render a tree-switch warning, and which tree the switch is FROM."""
    out: List[str] = [
        current_tree_banner(report),
        f"SWITCHING TREE: {_line(warning.get('from_path'))} -> "
        f"{_line(warning.get('to_path'))}",
    ]
    if warning.get("doing_known"):
        out.append(
            f"  !! THIS SESSION WAS DOING: {_line(warning.get('doing'))}. "
            + _line(warning.get("activity_warns"))
        )
    elif not warning.get("doing"):
        out.append(
            "  what this session was doing is NOT RECORDED, so whether the "
            "switch interrupts live work is UNKNOWN rather than 'nothing'"
        )
    if warning.get("unknown_is_not_clean"):
        out.append(
            "  !! uncommitted state is UNKNOWN (git could not be asked); this "
            "switch is UNVERIFIED, not safe"
        )
    elif warning.get("uncommitted_count"):
        out.append(
            f"  !! {warning['uncommitted_count']} uncommitted path(s) stay in the "
            f"tree you are leaving: {_line(', '.join(list(warning.get('uncommitted_paths') or ())[:8]))}"
        )
        out.append(f"  {_line(warning.get('warns'))}")
    else:
        out.append("  no uncommitted work in the tree you are leaving")
    return out


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def worktree_list_lines(
    report: WorktreeReport, *, banner: bool = True
) -> List[str]:
    """Render every worktree with its state, including the zero case."""
    out: List[str] = []
    if banner:
        out.append(current_tree_banner(report))
    out.append(f"WORKTREES (managed under {_line(report.source)}):")
    if not report.source_available:
        out.append(
            "  no managed-worktree store was found. That is UNKNOWN - it is NOT "
            "'there are no worktrees', and a parallel session could still be "
            "running in one."
        )
        return out
    if not report.rows:
        out.append(
            "  the store exists and lists ZERO worktrees. A session running now "
            "is in the repository's own checkout."
        )
        return out
    for row in report.rows:
        pointer = "->" if row.is_current else "  "
        changed = row.changed_count
        changed_text = "unknown" if changed < 0 else str(changed)
        if row.orphaned:
            # An orphan is named in the MARKER column, not appended as a note
            # below a row that reads `state: ready`. The measured pre-round output
            # was `ghost [ready] branch=unknown changed=unknown clean=False`,
            # which reads as a worktree awaiting attention rather than as a
            # directory that does not exist.
            state = "ORPHANED"
        else:
            state = _line(row.state)
        out.append(
            f"  {pointer} {_line(row.node_id)} [{state}] "
            f"branch={_line(row.branch)} changed={changed_text} "
            f"clean={row.clean} {('CURRENT SESSION' if row.is_current else '')}".rstrip()
        )
        out.append(f"       path: {_line(row.path or 'unrecorded')}")
        if row.created_at:
            out.append(f"       created: {_line(row.created_at)}")
        if row.session_id:
            out.append(f"       created by session: {_line(row.session_id)}")
        if row.orphaned:
            out.append(
                f"       !! ORPHANED: {_line(row.orphan_reason)}. This is NOT a "
                "live session's tree and cannot be attached to; recovering it "
                "means reading what is still on disk, not resuming it."
            )
        if row.error:
            out.append(f"       recorded error: {_line(row.error)}")
    return out


def worktree_detail_lines(
    report: WorktreeReport, node_id: str, *, banner: bool = True
) -> List[str]:
    """Render one worktree in full, including its changed-file list."""
    out: List[str] = []
    if banner:
        out.append(current_tree_banner(report))
    row = None
    for item in report.rows:
        if item.node_id == str(node_id):
            row = item
            break
    if row is None:
        out.append(f"WORKTREE {node_id}: no such managed worktree is recorded")
        return out
    out.append(f"WORKTREE {_line(row.node_id)}")
    out.append(f"  path: {_line(row.path or 'unrecorded')}")
    out.append(
        f"  state: {_line('ORPHANED' if row.orphaned else row.state)}   "
        f"exists: {row.exists}"
    )
    if row.orphaned:
        out.append(f"  !! ORPHANED: {_line(row.orphan_reason)}")
        out.append(
            "     a worktree whose session or directory is gone is NOT a live "
            "session: it is a receipt of work that may still be on disk, and it "
            "cannot be resumed or attached to"
        )
    out.append(f"  branch: {_line(row.branch)}   HEAD: {_line(row.commit or 'unavailable')}")
    out.append(f"  pinned base commit: {_line(row.base_commit or 'unrecorded')}")
    out.append(f"  created: {_line(row.created_at or 'unrecorded')}")
    if row.session_id:
        out.append(f"  created by session: {_line(row.session_id)}")
    out.append(f"  clean: {row.clean}   changed files: {row.changed_count}")
    if row.dirty_unknown:
        out.append(
            "  !! the changed-file list could not be read; 'unknown' is NOT "
            "zero and NOT clean"
        )
    elif row.changed_files:
        out.append("  changed:")
        for path in list(row.changed_files)[:40]:
            out.append(f"    - {_line(path)}")
        if len(row.changed_files) > 40:
            out.append(f"    ... and {len(row.changed_files) - 40} more")
    else:
        out.append("  no uncommitted changes in this worktree")
    if row.error:
        out.append(f"  recorded error: {_line(row.error)}")
    return out


# ---------------------------------------------------------------------------
# Operator surface
# ---------------------------------------------------------------------------


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Print the current tree, then the managed worktrees."""
    args = list(sys.argv[1:] if argv is None else argv)
    repo = "."
    root = None
    node = ""
    if "--repo" in args:
        index = args.index("--repo")
        if index + 1 < len(args):
            repo = args[index + 1]
    if "--root" in args:
        index = args.index("--root")
        if index + 1 < len(args):
            root = args[index + 1]
    for index, token in enumerate(args):
        if token in ("list", "detail") and index + 1 < len(args):
            node = args[index + 1]
    report = read_worktrees(repo, root)
    for line in current_tree_lines(report):
        print(line)
    if "--json" in args:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        return 0
    for line in worktree_list_lines(report, banner=False):
        print(line)
    if node:
        for line in worktree_detail_lines(report, node, banner=False):
            print(line)
        for line in cleanup_lines(
            cleanup_receipt(node, report), report=report
        ):
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(_cli())
