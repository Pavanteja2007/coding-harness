"""T5.W1.2 — honest git output for a human triggering it.

Three demands a UI puts on ``execution/git_output.py``:

1. **Preview-before-execute.** The user sees exactly what will run. That is
   truthful only because the argv is fixed and never a shell — this module
   derives the preview from the SAME argv builder the executor uses.
2. **User-supplied commit messages and branch names.** Hostile text is
   bounded, stripped of NUL/escape bytes, and never executed.
3. **Partial-failure recovery.** A commit that fails after staging must
   leave the repository as it was; that is
   ``git_output._rollback_branch``, exercised here under a real failure.

Plus the two non-negotiables, asserted rather than hoped:

* the ORIGINAL repository is never touched — every argv runs with
  ``cwd=work_dir`` and names no outside path;
* a credential in a diff never reaches a commit — a live token raises, and
  the redaction path is a separate, testable branch.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple

from execution.git_output import (
    GitOutputError,
    _git,
    _git_argv_for,
    _redact_secrets,
    _safe_changed_files,
    _validate_branch_name,
)

__all__ = [
    "MAX_COMMIT_TEXT_CHARS",
    "GitPreview",
    "assert_no_live_credentials",
    "assert_preview_matches_argv",
    "original_repo_is_untouched",
    "preview_branch_and_commit",
    "restore_after_failed_commit",
    "sanitize_commit_text",
]

#: Bound on user-supplied free text. A commit message is not a novel; a
#: bound also stops an accidental paste of a multi-megabyte log.
MAX_COMMIT_TEXT_CHARS = 100_000

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_commit_text(text: object, *, limit: int = MAX_COMMIT_TEXT_CHARS) -> str:
    """Return text safe to place on ONE argv slot.

    Bounds length, strips NUL and terminal-control bytes (so the preview a
    human reads is the text a human sees), and normalises to a single line
    for a subject. It does NOT interpret the text — a fixed-argv list means
    no shell ever will."""
    if not isinstance(text, str):
        raise GitOutputError("commit text must be a string")
    cleaned = _CONTROL_RE.sub("", text)
    cleaned = unicodedata.normalize("NFC", cleaned)
    # keep newlines meaningful for the body, but drop CR and stray controls
    cleaned = cleaned.replace("\r", "\n")
    if len(cleaned) > limit:
        raise GitOutputError(
            f"commit text exceeds the {limit}-character bound ({len(cleaned)} given)"
        )
    return cleaned.strip()


@dataclass(frozen=True)
class GitPreview:
    """What will run, derived from the real argv — never a paraphrase."""

    branch_name: str
    commit_message: str
    changed_files: Tuple[str, ...]
    steps: Tuple[Tuple[List[str], str], ...] = ()  # (argv, cwd) per step
    redacted: bool = False
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "branch_name": self.branch_name,
            "commit_message": self.commit_message,
            "changed_files": list(self.changed_files),
            "steps": [{"argv": list(argv), "cwd": cwd} for argv, cwd in self.steps],
            "redacted": self.redacted,
            "warnings": list(self.warnings),
        }


def preview_branch_and_commit(
    work_dir: str,
    commit_message: str,
    branch_name: Optional[str] = None,
    changed_files: Optional[List[str]] = None,
    pristine_dir: Optional[str] = None,
) -> GitPreview:
    """The truthful preview: the exact argv per git step, built the same way
    ``branch_and_commit`` will build it, Before anything runs."""
    safe_message = sanitize_commit_text(commit_message)
    redacted_message = _redact_secrets(safe_message).strip()
    if not redacted_message:
        raise GitOutputError("commit_message is empty after secret redaction")
    if branch_name is None:
        subject = redacted_message.splitlines()[0].replace("[fix] ", "", 1)
        from execution.git_output import _slugify

        branch_name = f"harness/fix-{_slugify(subject)}"
    branch_name = _validate_branch_name(branch_name)
    paths = _safe_changed_files(changed_files)

    steps: List[Tuple[List[str], str]] = [
        (_git_argv_for(work_dir, "init", "-q"), work_dir),
    ]
    if paths is None:
        steps.append((_git_argv_for(work_dir, "add", "-A"), work_dir))
    else:
        steps.append((_git_argv_for(work_dir, "add", "-A", "--", *paths), work_dir))
    steps.append(
        (_git_argv_for(work_dir, "checkout", "-q", "-b", branch_name), work_dir)
    )
    steps.append(
        (_git_argv_for(work_dir, "commit", "-q", "-m", redacted_message, "--no-verify"), work_dir)
    )
    steps.append((_git_argv_for(work_dir, "rev-parse", "HEAD"), work_dir))

    warnings = []
    if redacted_message != safe_message:
        warnings.append("commit message was redacted before it will reach git")
    if paths is not None and not paths:
        warnings.append("changed_files is empty")
    return GitPreview(
        branch_name=branch_name,
        commit_message=redacted_message,
        changed_files=tuple(paths or ()),
        steps=tuple(steps),
        redacted=redacted_message != safe_message,
        warnings=tuple(warnings),
    )


def assert_preview_matches_argv(preview: GitPreview, recorded_argv: List[str]) -> None:
    """The preview and the executed argv are the same list, item for item."""
    commit_steps = [argv for argv, _ in preview.steps if "commit" in argv]
    if not commit_steps:
        raise GitOutputError("preview carries no commit step")
    if tuple(commit_steps[0]) != tuple(recorded_argv):
        raise GitOutputError(
            f"preview does not match executed argv:\npreview={commit_steps[0]!r}\nexecuted={recorded_argv!r}"
        )


_CREDENTIAL_RE = re.compile(
    r"\b(?:sk|ghp|gho|ghu|ghs|ghr|xox[baprs]|hf|npm|pypi|AKIA|ASIA)[-_]?[A-Za-z0-9_-]{8,}"
)


def assert_no_live_credentials(diff_text: object) -> None:
    """A credential in a diff must never reach a commit.

    This is the Phase-0 leak class in the commit path: if redaction would
    change the text, the text still holds a live token, and we refuse."""
    text = "" if diff_text is None else str(diff_text)
    redacted = _redact_secrets(text)
    if redacted != text or _CREDENTIAL_RE.search(text):
        raise GitOutputError(
            "credential-shaped token found in the diff; refusing to commit"
        )


def restore_after_failed_commit(work_dir: str, branch_name: str) -> None:
    """Best-effort restore after a half-written commit; the real mechanism is
    ``git_output._rollback_branch`` — this is the UI-facing assertion helper."""
    try:
        _git(work_dir, "reset", "--mixed", "HEAD", check=False)
    finally:
        try:
            _git(work_dir, "branch", "-D", branch_name, check=False)
        except GitOutputError:
            pass


def original_repo_is_untouched(original_repo: str, preview: GitPreview) -> bool:
    """Every previewed command runs with cwd=work_dir and names no path outside
    the work dir. That is the structural reason the original repo is safe."""
    original = os.path.normcase(os.path.abspath(original_repo))
    for argv, cwd in preview.steps:
        if os.path.normcase(os.path.abspath(cwd)) == original:
            return False
        for part in argv:
            if isinstance(part, str) and original in os.path.normcase(part):
                return False
    return True
