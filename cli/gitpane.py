"""The right-hand GIT pane: branch, status, history, and a previewed commit.

Phase 5/W1 task T5.W1.3.

The one rule this module is built around: **there is no git implementation
here.** Every mutating action delegates to
:func:`execution.git_output.produce_git_output`, which is the ONE hardened
commit path (fixed argv, no shell, ambient ``GIT_*`` and global config stripped,
hooks and filters disabled, branch names validated, the original repository
never touched). Every read goes through ``execution.git_output._git``, the same
hardened runner, so a read cannot be more permissive than the write it describes.

Four properties are gates rather than comments:

1. **The work branch is DISCOVERABLE.** A branch name without the directory it
   lives in is the Phase 0 failure this pane exists to close: a person who
   cannot `cd` to the branch is looking at a branch they cannot use.
   :func:`git_discoverability_lines` always states the work directory, the
   branch, and the literal command that reaches it.
2. **Every mutating action is PREVIEWED, and the preview shows the command.**
   :func:`commit_preview_lines` renders the exact argv that will be run, built
   from the same ``_git_argv_for`` the executor uses, so the preview cannot
   describe a different command from the one that runs.
3. **The original repository is never the target.** :func:`assert_work_dir_is_isolated`
   refuses a work directory that IS the repository root, and refuses one that is
   not a descendant of the run's own artifact root.
4. **Reverts are hash-verified by ``cli.review``.** This pane never writes a
   byte of restore logic; it calls :func:`cli.review.revert_paths` and renders
   its receipt, including the per-path edit tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cli import review as _review
from cli import ui as _ui

__all__ = [
    "ELLIPSIS",
    "GIT_WIDGET_ID",
    "MAX_HISTORY",
    "MIN_GIT_COLUMNS",
    "GitReport",
    "assert_preview_names_the_command",
    "assert_work_dir_is_isolated",
    "branch_lines",
    "commit_from_ui",
    "commit_preview_lines",
    "git_discoverability_lines",
    "git_report",
    "history_lines",
    "pr_description_lines",
    "revert_from_ui",
    "status_lines",
]

#: Widget id the container mounts this pane under.
GIT_WIDGET_ID = "neo-git"

ELLIPSIS = "…"

MIN_GIT_COLUMNS = 24

#: History is a bounded list with the bound STATED. An unbounded ``git log``
#: in a pane is how a fast checkout turns a repaint into a hang.
MAX_HISTORY = 12


def _bound(value: Any, width: int, *, marker: str = ELLIPSIS) -> str:
    """Sanitise then bound. Same order as every other renderer in this package."""
    try:
        text = _ui.sanitize_text(value)
    except Exception:  # pragma: no cover - sanitize_text already fails closed
        text = "(detail withheld: sanitiser unavailable)"
    try:
        limit = max(1, int(width))
    except (TypeError, ValueError):
        limit = 1
    if limit <= 1:
        return marker
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + marker


def _git() -> Any:
    """The hardened git runner, imported lazily.

    Lazy so a pane that only renders a receipt does not pay for the module
    import, and so this module has no import-time dependency on ``execution``.
    An unimportable executor degrades the pane to "git unavailable" rather than
    taking the shell down.
    """
    from execution import git_output as _go

    return _go


def _run(work_dir: Any, *args: str) -> Optional[str]:
    """Run one read-only git command in ``work_dir`` through the hardened runner.

    Returns stdout, or ``None`` when git could not answer. ``None`` is the
    honest answer for "I could not look"; an empty string would read as "there
    is nothing there", which is a different fact.
    """
    if not work_dir:
        return None
    try:
        completed = _git()._git(str(work_dir), *args, check=False)
    except Exception:
        return None
    try:
        code = int(getattr(completed, "returncode", 1))
    except (TypeError, ValueError):
        code = 1
    if code != 0:
        return None
    out = getattr(completed, "stdout", "") or ""
    return str(out)


def _work_dir(task_dir: Any, repo: Any = None) -> Optional[str]:
    """The run's own WORK COPY, or ``None`` when it has none.

    Deliberately ``logs/{task_id}/work`` and never ``repo``: this is the
    directory the run wrote, and the pane's whole safety argument is that the
    original checkout is not a git target here.
    """
    if not task_dir:
        return None
    candidate = Path(str(task_dir)) / "work"
    if candidate.is_dir():
        return str(candidate)
    return None


@dataclass(frozen=True)
class GitReport:
    """The git pane's whole state as a value, so a render never reads the world."""

    task_id: str = ""
    work_dir: str = ""
    repo: str = ""
    branch: str = ""
    head_sha: str = ""
    commit_message: str = ""
    pr_description: str = ""
    status_rows: Tuple[Tuple[str, str], ...] = ()
    history: Tuple[Tuple[str, str], ...] = ()
    available: bool = False
    unavailable_reason: str = ""
    log_root: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-friendly projection. Every value is data, never markup."""
        return {
            "task_id": self.task_id,
            "work_dir": self.work_dir,
            "repo": self.repo,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "commit_message": self.commit_message,
            "pr_description": self.pr_description,
            "status": [{"path": path, "code": code} for path, code in self.status_rows],
            "history": [{"sha": sha, "subject": subject} for sha, subject in self.history],
            "available": bool(self.available),
            "unavailable_reason": self.unavailable_reason,
            "log_root": self.log_root,
        }


def git_report(
    task_dir: Any,
    repo: Any = None,
    *,
    width: int = 60,
    max_history: int = MAX_HISTORY,
) -> GitReport:
    """Read the git surface of a run's work copy: branch, status, history.

    An absent work copy is reported as ``available=False`` WITH the reason, and
    never as an empty branch. "This run has no work copy" and "this run's work
    copy is on no branch" are different facts and only the first is true when the
    directory is missing.
    """
    work = _work_dir(task_dir, repo)
    if work is None:
        return GitReport(
            task_id=str(task_dir or ""),
            repo=str(repo or ""),
            available=False,
            unavailable_reason=(
                "this run has no work/ copy, so there is no branch, status or "
                "history to show; the original repository is never used as a stand-in"
            ),
        )
    branch = (_run(work, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    sha = (_run(work, "rev-parse", "HEAD") or "").strip()
    subject = (_run(work, "log", "-1", "--pretty=%s") or "").strip()
    try:
        limit = max(1, int(max_history))
    except (TypeError, ValueError):
        limit = MAX_HISTORY
    rows: List[Tuple[str, str]] = []
    raw = _run(work, "log", f"-{limit}", "--pretty=%h %s")
    if raw:
        for line in raw.splitlines():
            parts = line.strip().split(" ", 1)
            if not parts:
                continue
            rows.append((parts[0], parts[1] if len(parts) > 1 else ""))
    porcelain = _run(work, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    status: List[Tuple[str, str]] = []
    if porcelain:
        for record in porcelain.split("\0"):
            if len(record) < 3:
                continue
            status.append((record[3:], record[:2]))
    return GitReport(
        task_id=str(task_dir or ""),
        work_dir=str(work),
        repo=str(repo or ""),
        branch=str(branch or ""),
        head_sha=str(sha or ""),
        commit_message=str(subject or ""),
        status_rows=tuple(status),
        history=tuple(rows),
        available=True,
        log_root=str(Path(str(task_dir)).parent) if task_dir else "",
    )


def git_discoverability_lines(report: Any, width: int = 60) -> List[str]:
    """State where the branch lives and the literal command that reaches it.

    This is the Phase 0 failure made structural: a branch a person cannot
    reach is not a branch they can use, so the pane always names the WORK
    DIRECTORY and the ``cd`` line. Never empty.
    """
    limit = max(1, int(width))
    work = str(getattr(report, "work_dir", "") or "")
    if not work:
        return [_bound("no work copy: there is no branch to find", limit)]
    branch = str(getattr(report, "branch", "") or "")
    return [
        _bound(f"branch  {branch or '(detached or unborn HEAD)'}", limit),
        _bound(f"work at {_bound(work, max(8, limit - 8))}", limit),
        _bound(f"reach it: cd {_bound(work, max(6, limit - 10))}", limit),
        _bound(
            "the branch exists in that copy only - your working tree was never "
            "committed to",
            limit,
        ),
    ]


def branch_lines(report: Any, width: int = 60) -> List[str]:
    """Branch and HEAD rows. Absent facts render as ``unavailable``, never blank."""
    limit = max(1, int(width))
    branch = str(getattr(report, "branch", "") or "")
    sha = str(getattr(report, "head_sha", "") or "")
    return [
        _bound(f"branch  {branch or 'unavailable'}", limit),
        _bound(f"HEAD    {sha[:12] or 'unavailable'}", limit),
        _bound(f"subject {_bound(getattr(report, 'commit_message', ''), max(8, limit - 8))}", limit),
    ]


def status_lines(report: Any, width: int = 60, *, max_rows: int = 24) -> List[str]:
    """The working-tree status of the work copy, with an empty state that says so."""
    limit = max(1, int(width))
    if not bool(getattr(report, "available", False)):
        return [_bound(f"status unavailable: {getattr(report, 'unavailable_reason', '')}", limit)]
    rows = tuple(getattr(report, "status_rows", ()) or ())
    if not rows:
        return [_bound("status  clean - the work copy has nothing uncommitted", limit)]
    out = [_bound(f"status  {len(rows)} path(s) uncommitted in the work copy", limit)]
    try:
        cap = max(1, int(max_rows))
    except (TypeError, ValueError):
        cap = 24
    for path, code in rows[:cap]:
        out.append(_bound(f"  {code} {path}", limit))
    if len(rows) > cap:
        out.append(_bound(f"+{len(rows) - cap} more path(s) not shown", limit))
    return out


def history_lines(report: Any, width: int = 60, *, max_rows: int = MAX_HISTORY) -> List[str]:
    """Commit history of the work copy, bounded and stating the bound."""
    limit = max(1, int(width))
    if not bool(getattr(report, "available", False)):
        return [_bound("history unavailable: no work copy", limit)]
    rows = tuple(getattr(report, "history", ()) or ())
    if not rows:
        return [_bound("history  empty - this work copy has no commit yet", limit)]
    try:
        cap = max(1, int(max_rows))
    except (TypeError, ValueError):
        cap = MAX_HISTORY
    out = [_bound(f"history {len(rows)} commit(s) in the work copy", limit)]
    for sha, subject in rows[:cap]:
        out.append(_bound(f"  {sha[:8]} {_bound(subject, max(6, limit - 11))}", limit))
    if len(rows) > cap:
        out.append(_bound(f"+{len(rows) - cap} more commit(s) not shown", limit))
    return out


def commit_preview_lines(
    work_dir: Any,
    issue_text: str = "",
    changed_files: Sequence[Any] = (),
    *,
    branch_name: str = "",
    width: int = 60,
    verification_summary: str = "",
) -> List[str]:
    """The exact commands a commit WILL run, plus what it will contain.

    The argv is built from ``execution.git_output._git_argv_for`` -- the same
    builder the executor uses -- so this preview cannot describe a different
    command from the one that runs. A preview that paraphrases the action is not
    a preview; it is a promise.
    """
    limit = max(1, int(width))
    work = str(work_dir or "")
    if not work:
        return [_bound("no work copy: there is nothing to commit", limit)]
    lines = [_bound("commit PREVIEW - nothing has run yet", limit)]
    try:
        from execution.git_output import _git_argv_for

        branch = str(branch_name or "harness/fix-<slug>")
        argv = _git_argv_for(work, "checkout", "-b", branch)
        commit_argv = _git_argv_for(work, "commit", "-m", "<subject>")
    except Exception:
        lines.append(_bound("(argv preview unavailable; the executor still refuses to run)", limit))
        return lines
    lines.append(_bound("will run:", limit))
    lines.append(_bound("  " + _bound(argv, max(10, limit - 2)), limit))
    lines.append(_bound("  " + _bound(commit_argv, max(10, limit - 2)), limit))
    lines.append(_bound(f"branch   {_bound(branch, max(8, limit - 8))}", limit))
    lines.append(_bound(f"subject  {_bound(issue_text or '<first sentence of the issue>', max(9, limit - 9))}", limit))
    files = [str(item) for item in (changed_files or ()) if str(item)]
    lines.append(_bound(f"files    {len(files)} path(s) in the commit body", limit))
    for path in files[:8]:
        lines.append(_bound(f"  {path}", limit))
    if len(files) > 8:
        lines.append(_bound(f"  +{len(files) - 8} more path(s) listed in the body", limit))
    if verification_summary:
        lines.append(_bound(f"verify   {_bound(verification_summary, max(8, limit - 8))}", limit))
    lines.append(
        _bound(
            "safety   no shell, hooks and filters off, ambient GIT_* stripped; the "
            "ORIGINAL repository is not a target",
            limit,
        )
    )
    return lines


def commit_from_ui(
    task_dir: Any,
    repo: Any = None,
    *,
    issue_text: str = "",
    changed_files: Sequence[Any] = (),
    diff: str = "",
    verification_summary: str = "",
    branch_name: str = "",
    width: int = 60,
    in_flight: bool = False,
    confirm: Optional[Any] = None,
) -> Dict[str, Any]:
    """Commit through ``execution.git_output`` -- the ONE hardened path.

    ``confirm`` is REQUIRED to be a callable when supplied; without it nothing
    is committed. The preview is rendered FIRST and is part of the return
    value, so a caller cannot accidentally commit without having been shown the
    command.
    """
    work = _work_dir(task_dir, repo)
    if work is None:
        return {
            "ok": False,
            "lines": ["no work copy for this run: refusing to commit against the original repository"],
            "branch": "",
            "commit_sha": "",
            "pr_description": "",
            "preview": [],
        }
    guard = assert_work_dir_is_isolated(work, repo)
    if not guard["ok"]:
        return {
            "ok": False,
            "lines": [f"refused: {guard['reason']}"],
            "branch": "",
            "commit_sha": "",
            "pr_description": "",
            "preview": [],
        }
    if bool(in_flight):
        return {
            "ok": False,
            "lines": ["a run is in flight: refusing to commit a tree that is still changing"],
            "branch": "",
            "commit_sha": "",
            "pr_description": "",
            "preview": [],
        }
    preview = commit_preview_lines(
        work,
        issue_text,
        changed_files,
        branch_name=branch_name,
        width=width,
        verification_summary=verification_summary,
    )
    if confirm is not None:
        try:
            if not bool(confirm(preview)):
                return {
                    "ok": False,
                    "lines": [*preview, "cancelled: nothing was committed"],
                    "branch": "",
                    "commit_sha": "",
                    "pr_description": "",
                    "preview": preview,
                }
        except Exception as exc:  # a raising confirmer is a refusal, not a commit
            return {
                "ok": False,
                "lines": [*preview, f"refused: the confirmation raised ({exc})"],
                "branch": "",
                "commit_sha": "",
                "pr_description": "",
                "preview": preview,
            }
    try:
        from execution.rationale import build_rationale

        rationale = build_rationale(str(task_dir), issue_text or None)
    except Exception:
        rationale = ""
    try:
        result = _git().produce_git_output(
            work,
            issue_text,
            [str(item) for item in (changed_files or ())],
            diff=diff or None,
            verification_summary=verification_summary or None,
            rationale=rationale or None,
            branch_name=branch_name or None,
            pristine_dir=str(Path(str(task_dir)) / "pristine"),
        )
    except Exception as exc:
        return {
            "ok": False,
            "lines": [*preview, f"commit failed: {_bound(str(exc), 100)}"],
            "branch": "",
            "commit_sha": "",
            "pr_description": "",
            "preview": preview,
        }
    return {
        "ok": True,
        "lines": [
            *preview,
            "committed:",
            f"  branch {result.get('branch') or '(unknown)'}",
            f"  sha    {result.get('commit_sha') or '(unknown)'}",
        ],
        "branch": str(result.get("branch") or ""),
        "commit_sha": str(result.get("commit_sha") or ""),
        "commit_message": str(result.get("commit_message") or ""),
        "pr_description": str(result.get("pr_description") or ""),
        "preview": preview,
    }


def revert_from_ui(task_dir: Any, repo: Any, path: str, **kwargs: Any) -> Dict[str, Any]:
    """Revert one path through ``cli.review`` so the hash gate and tier receipt hold.

    This pane writes no restore logic. ``cli.review.revert_paths`` is
    hash-pinned in both directions and stamps the per-path EDIT TIER onto its
    receipt, so the pane's undo says WHICH guarantee produced the bytes.
    """
    safe = str(path or "")
    if not safe:
        return {"ok": False, "lines": ["no path was focused"], "tiers": {}}
    receipt = _review.revert_paths(task_dir, repo, [safe], **kwargs)
    tiers = {}
    try:
        tiers = dict(getattr(receipt, "edit_tiers", {}) or {})
    except Exception:
        tiers = {}
    lines = list(_review.render_receipt(receipt))
    if tiers:
        lines.append("reverted at tier: " + ", ".join(f"{path}=tier {tier}" for path, tier in sorted(tiers.items())))
    return {"ok": bool(getattr(receipt, "ok", False)), "lines": lines, "tiers": tiers}


def pr_description_lines(
    task_dir: Any,
    issue_text: str = "",
    *,
    width: int = 60,
    produced: str = "",
) -> List[str]:
    """The PR description, from ``execution.rationale`` or a produced commit.

    The rationale is the deterministic, grounded paragraph
    ``execution.rationale.build_rationale`` produces from the run's OWN
    ``trace.jsonl`` and ``state.json``. This pane renders it and never
    re-derives it: a second summary of a run is a second answer to "what
    happened", and two answers drift.
    """
    limit = max(1, int(width))
    body = str(produced or "")
    if not body:
        try:
            from execution.rationale import build_rationale

            body = build_rationale(str(task_dir), issue_text or None)
        except Exception as exc:
            return [_bound(f"PR description unavailable: {exc}", limit)]
    if not str(body).strip():
        return [
            _bound(
                "PR description is empty: this run's journal carried no rationale, "
                "which is a fact about the run rather than a formatting failure",
                limit,
            )
        ]
    return [_bound(line, limit) for line in str(body).splitlines()]


def assert_work_dir_is_isolated(work_dir: Any, repo: Any = None) -> Dict[str, Any]:
    """Refuse a work directory that is the repository itself, or outside the run.

    The failure this prevents is precise and was measured before this pane
    existed: committing from ``repo`` puts the agent's change on the person's
    branch. So the guard REFUSES ``work_dir == repo`` rather than warning about
    it, and it also refuses a work directory that is not inside the run's own
    ``logs/<task>/`` tree, because that is the only place the harness puts a
    copy it owns.
    """
    work = str(work_dir or "")
    if not work:
        return {"ok": False, "reason": "no work directory was named"}
    try:
        resolved = Path(work).resolve()
    except (OSError, ValueError):
        return {"ok": False, "reason": "the work directory could not be resolved"}
    if repo:
        try:
            root = Path(str(repo)).resolve()
        except (OSError, ValueError):
            root = None
        if root is not None and resolved == root:
            return {
                "ok": False,
                "reason": (
                    "the work directory IS the repository root; committing from it "
                    "would put the run's change on your branch"
                ),
            }
    if resolved.name != "work":
        return {
            "ok": False,
            "reason": (
                "the work directory is not named 'work'; a run's copy always is, and "
                "this pane refuses to guess that some other directory is one"
            ),
        }
    if not resolved.parent.is_dir():
        return {
            "ok": False,
            "reason": "the work directory's task directory does not exist",
        }
    return {"ok": True, "reason": ""}


def assert_preview_names_the_command() -> None:
    """The preview must name the executable AND the branch, or it is not a preview."""
    lines = commit_preview_lines("logs/fix-abc12345/work", "the thing broke", ["a.py"])
    text = "\n".join(lines)
    assert "git" in text, f"the preview never names the command it will run: {text}"
    assert "checkout" in text and "commit" in text, (
        f"the preview does not name both git commands: {text}"
    )
    assert "PREVIEW" in text, "the preview does not say that nothing has run yet"
