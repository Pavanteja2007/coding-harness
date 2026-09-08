"""Git-native output for verified fixes (project-spec product-grade item 26).

Given a working copy whose fix the verifier confirmed, produce:
1. a branch (never commit on the checked-out branch the user was on),
2. a meaningful commit message (what was wrong + what changed), and
3. a PR description explaining the bug and the fix.

The harness works in logs/{task_id}/work/ which is NOT a git repo by default
(the snapshot deliberately drops .git). So this module initializes a fresh
git repo in the work dir when needed, commits the pristine state first (so
the fix is a proper second commit), and the diff between the two commits is
the fix. When the work dir already IS a git repo (harness could snapshot
with .git retained), we branch + commit on top instead.

Runs git via the host's git (NOT the sandbox): git plumbing is host-side
metadata manipulation, not untrusted code execution, and the agent's file
edits are already confined to the work dir by the harness design. Uses
`git -c user.name/-c user.email` per invocation — never writes global
config. All git operations run with cwd=work dir and are non-interactive
(-c core.hooksPath=/dev/null equivalent via --no-verify where applicable).
"""
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_AUTHOR_NAME = "harness-bot"
DEFAULT_AUTHOR_EMAIL = "harness-bot@coding-harness.invalid"
SLUG_MAX = 40


class GitOutputError(RuntimeError):
    """A git operation failed; message carries command + stderr."""


def _slugify(text: str, max_len: int = SLUG_MAX) -> str:
    """ASCII slug of a title, for branch names. Assumes text is short."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "fix"


def _git(work_dir: str, *args: str, check: bool = True) -> "subprocess.CompletedProcess[str]":
    """Run one git command with cwd=work_dir and harness author identity.

    Assumes git is on PATH. Raises GitOutputError on failure when check=True
    (message includes stderr) — never silently proceeds with half-written
    git state.
    """
    cmd = [
        "git", "-c", f"user.name={DEFAULT_AUTHOR_NAME}",
        "-c", f"user.email={DEFAULT_AUTHOR_EMAIL}",
        "-c", "core.hooksPath=",  # never run user hooks from the repo
        *args,
    ]
    try:
        cp = subprocess.run(
            cmd, cwd=work_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
    except OSError as exc:
        raise GitOutputError(f"git not runnable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitOutputError(f"git {' '.join(args[:2])} timed out") from exc
    if check and cp.returncode != 0:
        raise GitOutputError(
            f"git {' '.join(args)} failed (exit {cp.returncode}): "
            f"{(cp.stderr or cp.stdout)[:1000]}"
        )
    return cp


def _git_with_work_tree(
    git_dir: str, work_tree: str, *args: str, check: bool = True
) -> "subprocess.CompletedProcess[str]":
    """Run git with an alternate work tree (staging another directory).

    Assumes git_dir is an initialized .git directory; work_tree is the
    directory whose files should be staged/compared. Used to build the
    pristine commit from pristine_dir while .git lives in the work dir.
    """
    cmd = [
        "git", f"--git-dir={git_dir}", f"--work-tree={work_tree}",
        "-c", f"user.name={DEFAULT_AUTHOR_NAME}",
        "-c", f"user.email={DEFAULT_AUTHOR_EMAIL}",
        "-c", "core.hooksPath=",
        *args,
    ]
    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
    except OSError as exc:
        raise GitOutputError(f"git not runnable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitOutputError(f"git (work-tree) {' '.join(args[:2])} timed out") from exc
    if check and cp.returncode != 0:
        raise GitOutputError(
            f"git {' '.join(args)} failed (exit {cp.returncode}): "
            f"{(cp.stderr or cp.stdout)[:1000]}"
        )
    return cp


def _is_git_repo(work_dir: str) -> bool:
    """True iff work_dir ITSELF is a git repo (toplevel == work_dir).

    Git's rev-parse searches upward for .git — on machines where a parent
    directory (e.g. the user's home) is accidentally a repo, naive
    discovery would make us branch/commit inside THAT repo. So we compare
    the discovered toplevel against work_dir and only trust exact matches.
    """
    cp = _git(work_dir, "rev-parse", "--show-toplevel", check=False)
    if cp.returncode != 0:
        return False
    try:
        found = Path(cp.stdout.strip()).resolve()
    except (OSError, ValueError):
        return False
    return found == Path(work_dir).resolve()


def _ensure_repo(work_dir: str, pristine_dir: Optional[str]) -> None:
    """Ensure work_dir is a git repo whose FIRST commit is the pristine state.

    If work_dir is already a repo (harness kept .git in the snapshot), this
    is a no-op — the caller branches on top of existing history. Otherwise:
    git init, then stage the PRISTINE tree (from pristine_dir via git's
    --work-tree, so files never get copied) and commit it. The fix then
    lands as a second commit whose diff vs. the first is exactly the fix.
    Without pristine_dir, the repo starts empty and the fix commit becomes
    the root commit (branch_and_commit stages the fixed state itself).
    """
    if _is_git_repo(work_dir):
        return
    _git(work_dir, "init", "-q")
    git_dir = str(Path(work_dir) / ".git")
    if pristine_dir and Path(pristine_dir).is_dir():
        _git_with_work_tree(git_dir, pristine_dir, "add", "-A", "--", ".")
        _git_with_work_tree(
            git_dir, pristine_dir, "commit", "-q", "-m",
            "pristine state (pre-fix snapshot)", "--no-verify",
        )


def branch_and_commit(
    work_dir: str,
    commit_message: str,
    branch_name: Optional[str] = None,
    pristine_dir: Optional[str] = None,
    issue_text: Optional[str] = None,
) -> Tuple[str, str]:
    """Commit the verified fix on a new branch. Returns (branch, commit_sha).

    Assumes work_dir holds the post-fix state of a repo whose fix was
    verified by execution.verify. Never commits on the current branch:
    always creates harness/fix-<slug> (or the given branch_name). The first
    commit on a fresh repo is the pristine snapshot (when we can produce
    one), so the fix lands as its own commit with a clean diff.
    """
    _ensure_repo(work_dir, pristine_dir)
    if branch_name is None:
        # Branch slug comes from the message subject WITHOUT the "[fix] "
        # convention prefix (it's a commit convention, not a branch one).
        subject = commit_message.splitlines()[0] if commit_message else ""
        subject = subject.replace("[fix] ", "")
        slug = _slugify(subject)
        branch_name = f"harness/fix-{slug}"

    # Detach-safe branch creation: create + switch; if the branch name
    # already exists (task rerun), reuse it with a numeric suffix.
    base = branch_name
    suffix = 1
    while _git(work_dir, "rev-parse", "--verify", f"refs/heads/{branch_name}",
               check=False).returncode == 0:
        branch_name = f"{base}-{suffix}"
        suffix += 1

    _git(work_dir, "checkout", "-q", "-b", branch_name)
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", commit_message, "--no-verify")
    sha = _git(work_dir, "rev-parse", "HEAD").stdout.strip()
    return branch_name, sha


def commit_message_from(
    issue_text: str,
    changed_files: List[str],
    verification_summary: Optional[str] = None,
) -> str:
    """Render a meaningful commit message: subject, body (what/why), trailer.

    Assumes issue_text is the original bug report text and changed_files is
    the list of repo-relative files the fix touched. Format follows the
    project's `[module] short description` convention for the subject.
    """
    subject_line = _first_sentence(issue_text) or "fix reported issue"
    subject = f"[fix] {subject_line}"[:120]
    body_lines = [""]
    if changed_files:
        body_lines.append("What changed:")
        for f in changed_files[:20]:
            body_lines.append(f"- {f}")
    if verification_summary:
        body_lines.append("")
        body_lines.append("Verification:")
        body_lines.append(verification_summary)
    body_lines.append("")
    body_lines.append(f"Fixes issue: {subject_line}")
    return "\n".join([subject] + body_lines)


def _first_sentence(text: str) -> str:
    """First sentence-ish line of a bug report (<= 100 chars)."""
    text = (text or "").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    m = re.match(r"(.{0,100}?[.!?])\s", first_line + " ")
    if m and len(m.group(1)) > 10:
        return m.group(1)
    return first_line[:100]


def pr_description_from(
    issue_text: str,
    changed_files: List[str],
    diff: Optional[str],
    verification_summary: Optional[str] = None,
    rationale: Optional[str] = None,
) -> str:
    """Render a PR-style markdown description: what was wrong, what changed,
    why, and how it was verified.

    Assumes diff (unified diff text) and rationale (execution.rationale
    paragraph) may each be None; the description degrades gracefully.
    """
    parts: List[str] = ["## Problem", ""]
    parts.append(issue_text.strip() or "(no issue text provided)")
    parts.append("")
    if rationale:
        parts += ["## What was wrong", "", rationale.strip(), ""]
    if changed_files:
        parts += ["## Changes", ""]
        for f in changed_files[:20]:
            parts.append(f"- `{f}`")
        parts.append("")
    if verification_summary:
        parts += ["## Verification", "", verification_summary.strip(), ""]
    if diff:
        parts += ["## Diff", "", "```diff", diff[:20_000], "```", ""]
    return "\n".join(parts)


def produce_git_output(
    work_dir: str,
    issue_text: str,
    changed_files: List[str],
    diff: Optional[str] = None,
    verification_summary: Optional[str] = None,
    rationale: Optional[str] = None,
    branch_name: Optional[str] = None,
    pristine_dir: Optional[str] = None,
) -> dict:
    """End-to-end git-native output for one verified fix.

    Assumes work_dir contains the post-fix state (verified by the caller)
    and is safe to `git init`/branch/commit in. pristine_dir (the harness's
    logs/{task_id}/pristine copy) makes the fresh-repo history two commits:
    pristine state, then the fix — so the fix commit's diff is exactly the
    fix. Returns a dict: {"branch", "commit_sha", "commit_message",
    "pr_description"}.
    """
    msg = commit_message_from(issue_text, changed_files, verification_summary)
    branch, sha = branch_and_commit(
        work_dir, msg, branch_name, pristine_dir=pristine_dir
    )
    desc = pr_description_from(
        issue_text, changed_files, diff, verification_summary, rationale
    )
    return {
        "branch": branch,
        "commit_sha": sha,
        "commit_message": msg,
        "pr_description": desc,
    }
