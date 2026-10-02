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

import os
import re
import shutil
import stat
import subprocess
import unicodedata
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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


def _git_environment() -> dict[str, str]:
    """Return a minimal environment that excludes all ambient Git redirection."""
    passthrough = {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TZ",
        "WINDIR",
    }
    env = {
        key: value for key, value in os.environ.items() if key.upper() in passthrough
    }
    for key in tuple(env):
        if key.upper().startswith("GIT_"):
            env.pop(key, None)
    env.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return env


def _git_config_overrides(
    cwd: Optional[str] = None, git_dir: Optional[str] = None
) -> List[str]:
    """Neutralize repository-configured filters, textconv, and external diffs."""
    command = ["git"]
    if git_dir:
        command.append(f"--git-dir={git_dir}")
    if cwd:
        command.extend(["-C", cwd])
    command.extend(
        [
            "config",
            "--get-regexp",
            r"^(filter\..*\.(clean|smudge|process|required)|diff\..*\.textconv)$",
        ]
    )
    try:
        cp = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    filters: set[str] = set()
    drivers: set[str] = set()
    for line in cp.stdout.splitlines():
        key = line.split(None, 1)[0] if line.split(None, 1) else ""
        parts = key.split(".")
        if len(parts) < 3:
            continue
        name = parts[1]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            continue
        if parts[0] == "filter":
            filters.add(name)
        elif parts[0] == "diff":
            drivers.add(name)
    overrides = ["-c", "diff.external="]
    for name in sorted(filters):
        overrides.extend(
            [
                "-c",
                f"filter.{name}.clean=cat",
                "-c",
                f"filter.{name}.smudge=cat",
                "-c",
                f"filter.{name}.process=",
                "-c",
                f"filter.{name}.required=false",
            ]
        )
    for name in sorted(drivers):
        overrides.extend(["-c", f"diff.{name}.textconv="])
    return overrides


def _safe_changed_files(changed_files: Optional[List[str]]) -> Optional[List[str]]:
    """Validate and normalize repository-relative paths for staging."""
    if changed_files is None:
        return None
    normalized: List[str] = []
    for raw in changed_files:
        if not isinstance(raw, str):
            raise GitOutputError("changed_files entries must be strings")
        value = raw.replace("\\", "/").strip()
        if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
            raise GitOutputError(f"changed file is not repository-relative: {raw!r}")
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise GitOutputError(f"changed file has an unsafe path: {raw!r}")
        if any(part in (".git", ".hg", ".svn") for part in parts):
            raise GitOutputError(f"VCS metadata cannot be staged: {raw!r}")
        if value not in normalized:
            normalized.append(value)
    return normalized


def _git(
    work_dir: str, *args: str, check: bool = True
) -> "subprocess.CompletedProcess[str]":
    """Run one git command with cwd=work_dir and harness author identity.

    Assumes git is on PATH. Raises GitOutputError on failure when check=True
    (message includes stderr) — never silently proceeds with half-written
    git state.
    """
    cmd = [
        "git",
        "-c",
        f"user.name={DEFAULT_AUTHOR_NAME}",
        "-c",
        f"user.email={DEFAULT_AUTHOR_EMAIL}",
        "-c",
        "core.hooksPath=",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.safecrlf=false",
        *_git_config_overrides(work_dir),
        *args,
    ]
    try:
        cp = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env=_git_environment(),
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
        "git",
        f"--git-dir={git_dir}",
        f"--work-tree={work_tree}",
        "-c",
        f"user.name={DEFAULT_AUTHOR_NAME}",
        "-c",
        f"user.email={DEFAULT_AUTHOR_EMAIL}",
        "-c",
        "core.hooksPath=",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.safecrlf=false",
        *_git_config_overrides(git_dir=git_dir),
        *args,
    ]
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env=_git_environment(),
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


def _remove_created_git_dir(git_path: Path) -> None:
    """Remove a Git directory created by this module on POSIX and Windows."""
    if not git_path.is_dir() or git_path.is_symlink():
        return

    def make_writable(
        function: Callable[[str], object], path: str, _error: object
    ) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    shutil.rmtree(git_path, onerror=make_writable)


def _ensure_repo(work_dir: str, pristine_dir: Optional[str]) -> bool:
    """Ensure the first commit is pristine and report whether Git was created."""
    if _is_git_repo(work_dir):
        return False
    git_path = Path(work_dir) / ".git"
    if git_path.exists() or git_path.is_symlink():
        raise GitOutputError("work_dir contains .git metadata but is not a repository")
    created = False
    try:
        _git(work_dir, "init", "-q")
        created = True
        git_dir = str(git_path)
        if pristine_dir and Path(pristine_dir).is_dir():
            _git_with_work_tree(git_dir, pristine_dir, "add", "-A", "--", ".")
            _git_with_work_tree(
                git_dir,
                pristine_dir,
                "commit",
                "-q",
                "-m",
                "pristine state (pre-fix snapshot)",
                "--no-verify",
            )
    except Exception:
        if created and git_path.is_dir() and not git_path.is_symlink():
            _remove_created_git_dir(git_path)
        raise
    return True


def _staged_paths(work_dir: str) -> List[str]:
    """Return every path currently present in the index."""
    staged = _git(work_dir, "diff", "--cached", "--name-only", "-z").stdout
    return [item for item in staged.split("\0") if item]


def _ensure_clean_index(work_dir: str) -> None:
    """Refuse to alter a repository whose index already contains data."""
    staged = _staged_paths(work_dir)
    unmerged = _git(work_dir, "ls-files", "-u", "-z", check=False).stdout
    conflicts = [item for item in unmerged.split("\0") if item]
    if staged or conflicts:
        paths = sorted(set(staged + conflicts))
        raise GitOutputError(
            f"work_dir contains pre-existing staged or unmerged paths: {paths}"
        )


def _stage_intended(work_dir: str, changed_files: Optional[List[str]]) -> None:
    """Stage only declared paths after proving the starting index was clean."""
    paths = _safe_changed_files(changed_files)
    if paths is not None and not paths:
        raise GitOutputError("changed_files must name at least one repository path")
    if paths is None:
        _git(work_dir, "add", "-A")
    else:
        _git(work_dir, "add", "-A", "--", *paths)
    staged_paths = _staged_paths(work_dir)
    if paths is not None:
        unexpected = sorted(set(staged_paths) - set(paths))
        if unexpected:
            raise GitOutputError(
                f"staged paths escaped changed_files declaration: {unexpected}"
            )
    if not staged_paths:
        raise GitOutputError("declared changes produced no staged diff")


def _validate_branch_name(branch_name: str) -> str:
    """Validate a branch ref before passing it to Git."""
    value = str(branch_name or "")
    if (
        not value
        or value.startswith("-")
        or value.endswith(("/", "."))
        or ".." in value
        or "//" in value
        or "@{" in value
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
    ):
        raise GitOutputError(f"unsafe branch name: {branch_name!r}")
    return value


def _repository_state(work_dir: str) -> Tuple[str, str]:
    """Return the current symbolic ref and commit without changing repository state."""
    branch = _git(work_dir, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    head = _git(work_dir, "rev-parse", "--verify", "HEAD", check=False)
    return (
        branch.stdout.strip() if branch.returncode == 0 else "",
        head.stdout.strip() if head.returncode == 0 else "",
    )


def _rollback_branch(
    work_dir: str,
    branch_name: str,
    original_ref: str,
    original_head: str,
    repo_created: bool,
) -> Optional[str]:
    """Restore the starting branch and index after a failed Git transaction."""
    if repo_created:
        git_path = Path(work_dir) / ".git"
        if git_path.is_dir() and not git_path.is_symlink():
            _remove_created_git_dir(git_path)
            return None
        return "could not remove the Git directory created for the failed transaction"
    _git(work_dir, "reset", "--mixed", "HEAD", check=False)
    if original_ref:
        checkout = _git(work_dir, "checkout", "-q", original_ref, check=False)
        if checkout.returncode != 0:
            return f"could not restore original branch {original_ref!r}"
    elif original_head:
        checkout = _git(
            work_dir, "checkout", "-q", "--detach", original_head, check=False
        )
        if checkout.returncode != 0:
            return "could not restore the original detached HEAD"
    deleted = _git(work_dir, "branch", "-D", branch_name, check=False)
    if deleted.returncode != 0:
        return f"could not delete failed branch {branch_name!r}"
    return None


def branch_and_commit(
    work_dir: str,
    commit_message: str,
    branch_name: Optional[str] = None,
    pristine_dir: Optional[str] = None,
    issue_text: Optional[str] = None,
    changed_files: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Commit the verified fix on a new branch. Returns (branch, commit_sha).

    Assumes work_dir holds the post-fix state of a repo whose fix was
    verified by execution.verify. Never commits on the current branch:
    always creates harness/fix-<slug> (or the given branch_name). The first
    commit on a fresh repo is the pristine snapshot (when we can produce
    one), so the fix lands as its own commit with a clean diff.
    """
    if not isinstance(commit_message, str) or not commit_message.strip():
        raise GitOutputError("commit_message must be a non-empty string")
    safe_message = _redact_secrets(commit_message).strip()
    if not safe_message:
        raise GitOutputError("commit_message is empty after secret redaction")
    paths = _safe_changed_files(changed_files)
    if branch_name is None:
        subject = safe_message.splitlines()[0].replace("[fix] ", "", 1)
        branch_name = f"harness/fix-{_slugify(subject)}"
    branch_name = _validate_branch_name(branch_name)
    repo_created = _ensure_repo(work_dir, pristine_dir)
    original_ref, original_head = _repository_state(work_dir)
    branch_created = False
    try:
        _ensure_clean_index(work_dir)
        base = branch_name
        suffix = 1
        while (
            _git(
                work_dir,
                "rev-parse",
                "--verify",
                f"refs/heads/{branch_name}",
                check=False,
            ).returncode
            == 0
        ):
            branch_name = f"{base}-{suffix}"
            suffix += 1
        _git(work_dir, "checkout", "-q", "-b", branch_name)
        branch_created = True
        _stage_intended(work_dir, paths)
        _git(work_dir, "commit", "-q", "-m", safe_message, "--no-verify")
        sha = _git(work_dir, "rev-parse", "HEAD").stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise GitOutputError("git returned an invalid commit object id")
        return branch_name, sha
    except Exception as exc:
        rollback_error = None
        if repo_created or branch_created:
            rollback_error = _rollback_branch(
                work_dir,
                branch_name,
                original_ref,
                original_head,
                repo_created,
            )
        message = str(exc)
        if rollback_error:
            message = f"{message}; {rollback_error}"
        if isinstance(exc, GitOutputError) and not rollback_error:
            raise
        raise GitOutputError(message) from exc


def _redact_secrets(value: object) -> str:
    """Remove common credential forms from text before persistence or rendering.

    ============================ SECOND REDACTION POLICY =====================
    This is a 50-line LOCAL redaction table, and it is the one thing the P0/W1
    brief forbids outright: "Do not re-implement redaction." Its output
    diverges from ``shared.security.redact_text`` in the placeholder
    (``[REDACTED]`` vs ``[REDACTED_SECRET]``), in the private-key replacement
    (``[REDACTED PRIVATE KEY]``), and in coverage -- and its patterns carry the
    SAME two quadratic shapes the R2-11 round removed from the shared
    redactor, namely an unterminated PEM block matched with a lazy ``.*?`` and
    an unbounded ``[A-Za-z0-9_-]{8,}`` token run.

    It is NOT removed this round, and the reason is worth stating rather than
    hiding: ``tests/test_git_output_rationale.py`` pins the exact
    ``[REDACTED]`` placeholder, and changing it is a visible contract change
    to a commit message and a PR body. That is a real cost; the divergence is
    a real risk. The decision belongs to whoever owns the shared vocabulary
    and the test that pins it, so it is filed rather than forced.

    Filed as cross-terminal request U-2 in ``execution/AGENTS.md``. The one
    thing that is fixed here is the part nobody can pin: the NUL-byte strip
    stays (it is a transport concern, not a policy), and the docstring now
    says out loud that this is a second policy.
    ==========================================================================
    """
    text = "" if value is None else str(value)
    text = text.replace("\x00", "")
    text = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[^\s]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"\b(?:sk|ghp|gho|ghu|ghs|ghr|xox[baprs]|hf|npm|pypi)[-_][A-Za-z0-9_-]{8,}",
        "[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bgithub_pat_[A-Za-z0-9_-]{8,}",
        "[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:AKIA|ASIA|A3T[A-Z0-9]|AIza)[A-Z0-9_-]{12,}\b",
        "[REDACTED]",
        text,
    )
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd)"
        r"\b\s*([:=])\s*([\"']?)([^\s\"',;]+)\3",
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(https?://)[^\s/:@]+:[^\s/@]+@",
        r"\1[REDACTED]@",
        text,
    )
    return text


def _one_line(value: Optional[str], limit: int = 4000) -> str:
    """Collapse and redact presentation text so it cannot inject content."""
    if not isinstance(value, str):
        return ""
    return " ".join(_redact_secrets(value).split())[:limit]


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
    safe_files = _safe_changed_files(changed_files) or []
    subject_line = _first_sentence(issue_text) or "fix reported issue"
    subject = f"[fix] {subject_line}"[:120]
    body_lines = [""]
    if safe_files:
        body_lines.append("What changed:")
        for f in safe_files[:20]:
            body_lines.append(f"- {f}")
    if verification_summary:
        body_lines.append("")
        body_lines.append("Verification:")
        body_lines.append(_one_line(verification_summary))
    body_lines.append("")
    body_lines.append(f"Fixes issue: {subject_line}")
    return "\n".join([subject, *body_lines])


def _first_sentence(text: str) -> str:
    """Return the first redacted sentence-ish line of a bug report."""
    text = _redact_secrets(text or "").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    m = re.match(r"(.{0,100}?[.!?])\s", first_line + " ")
    if m and len(m.group(1)) > 10:
        return m.group(1)
    return first_line[:100]


def _quoted_issue(value: object, limit: int = 20_000) -> str:
    """Render issue text as bounded Markdown quote content after redaction."""
    text = _redact_secrets(value).strip()
    if not text:
        return "> (no issue text provided)"
    clipped = text[:limit]
    suffix = "\n> [issue text truncated]" if len(text) > limit else ""
    lines = [f"> {line}" if line else ">" for line in clipped.splitlines()]
    return "\n".join(lines) + suffix


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
    safe_files = _safe_changed_files(changed_files) or []
    parts: List[str] = ["## Problem", ""]
    parts.append(_quoted_issue(issue_text))
    parts.append("")
    if rationale:
        parts += ["## What was wrong", "", _one_line(rationale), ""]
    if safe_files:
        parts += ["## Changes", ""]
        for f in safe_files[:20]:
            parts.append(f"- `{f}`")
        parts.append("")
    if verification_summary:
        parts += ["## Verification", "", _one_line(verification_summary), ""]
    if diff:
        safe_diff = _redact_secrets(diff)[:20_000]
        parts += ["## Diff", ""]
        parts.extend(f"    {line}" for line in safe_diff.splitlines())
        parts.append("")
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

    ============================ UNFENCED (injection) =========================
    This is the only egress in the package whose output LEAVES THE MACHINE.
    Everything else ends in a tool result, a journal row or a terminal; the
    PR description is pasted into a pull request on a forge, where it is read
    by a human, indexed, and rendered as markdown or HTML.

    ``issue_text`` arrives here untrusted and attacker-controllable, and it is
    attacker-controllable in the strongest sense available to this product: it
    is the FIRST thing the agent ever sees. It is quoted verbatim into the PR
    body via ``_quoted_issue``, and its first sentence becomes the commit
    subject. Fenced for SECRETS (``_redact_secrets``; see its docstring for
    why that is a second policy and why it was not removed this round). NOT
    fenced for INJECTION: ``shared.security.review_untrusted_source`` has no
    production call site for the ``issue`` source, which is one of the two
    gaps ``shared/AGENTS.md`` records and which this round's brief names
    explicitly. ``shared/`` is Terminal 5's file; filed as request U-3 in
    ``execution/AGENTS.md``.

    The markdown angle is worth one line: an unquoted issue body can close its
    own fence and forge a `## Verification` heading that reads as
    harness-authored. A one-line ``_quoted_issue`` prefix would close that
    today; it is not added here because it changes a pinned PR-body shape and
    the forge-facing format is a product decision, not an execution one.
    ==========================================================================
    """
    msg = commit_message_from(issue_text, changed_files, verification_summary)
    branch, sha = branch_and_commit(
        work_dir,
        msg,
        branch_name,
        pristine_dir=pristine_dir,
        changed_files=changed_files,
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
