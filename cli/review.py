"""VEX-PF-05 - review what the agent did to the live repository, and put it back.

The agent edits the user's ACTUAL working tree. Until this module there was
no first-class surface to read that diff with the evidence that produced it,
and no first-class way to reverse it. Those are the two halves of "a tool you
trust with real work", and this file is both halves.

Six properties, each of which is the reason the feature exists:

1. **The diff is the real one.** The change set is measured, never declared.
   It comes from the run's own ``pristine/`` reference against the tree the
   run actually wrote, through the same :mod:`cli.fileview` projection every
   other surface renders, and it is cross-checked against the harness's own
   :func:`harness.editor.changed_files`. A disagreement between the two is
   REPORTED, not resolved by picking a winner.

2. **Revert is hash-pinned in BOTH directions.** The receipt carries the
   pre-image hash, the hash of the bytes on disk immediately before the
   write, and the hash of the bytes immediately after. A path counts as
   restored only when ``after == pristine``; a path that could not be
   restored is named with its reason instead of being dropped.

3. **A concurrent user edit is REFUSED, not overwritten.** "This file
   differs from the pre-image" is NOT evidence of a user edit - a revert is
   SUPPOSED to overwrite the change the run itself made, and refusing on
   that difference refuses every ordinary revert. The evidence is a live
   content hash that appears NOWHERE in the set of hashes the run's own
   journal receipts, its work-tree copy, and its pristine pre-image
   account for. That is the same rule the memory layer's staged store uses,
   reached from a different source, and it is the only rule that both
   refuses a real user edit and permits a real revert.

4. **A corrupt snapshot is REPORTED.** If the run's journal says it mutated
   a path whose pre-image the run recorded as existing, and the reference
   tree no longer holds that file, the reference is corrupt for that path.
   The review says so and the revert refuses; it never restores from
   nothing and never quietly reports an empty diff.

5. **An unverified run stays fully reviewable.** Reviewability is
   independent of verification. ``completed_unverified`` renders as
   unverified, every per-file claim fails closed through
   :func:`cli.fileview.file_change_verified`, and accept / reject / revert
   all still work. The user is never locked out of their own changes by the
   absence of a clean verifier receipt.

6. **Every rendered line is PLAIN text.** A repository path may contain
   ``[`` and a diff line is untrusted file content. Nothing in this module
   emits markup, and :func:`review_text_lines` returns ``rich.text.Text``
   objects for surfaces that want syntax highlighting - ``Text`` is never
   markup-parsed, so the highlighting path and the plain path are both
   injection-proof. A render failure can therefore never delete a message.

Nothing here reads a credential, contacts a provider, needs Docker, or
mutates a repository except through the explicit revert action. Every
function is total: an unusable input is a value carrying a reason, never an
exception, because a ``/review`` that raised inside a Textual handler takes
the whole TUI down.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cli import fileview as _fv
from cli.toggles import MIN_SECTION_ENTRIES, section_rows

__all__ = [
    "DEFAULT_DIFF_STYLE",
    "DIFF_REVIEW_VERBS",
    "DIFF_STYLE_CHOICES",
    "DIFF_STYLE_SPLIT_MIN_COLUMNS",
    "RESOLVED_DIFF_STYLES",
    "REVIEW_DECISIONS",
    "REVIEW_FILE_STATES",
    "REVIEW_RECEIPT_NAME",
    "REVIEW_RECEIPT_SCHEMA_VERSION",
    "REVIEW_VERDICTS",
    "REVIEW_WIDGET_ID",
    "CommandTools",
    "FileReview",
    "RevertReceipt",
    "ReviewDocument",
    "SnapshotHealth",
    "accept_hunks",
    "accept_paths",
    "blast_radius",
    "build_review",
    "changed_files_indicator",
    "decisions",
    "diff_review_verbs",
    "hunk_decisions",
    "read_review_receipt",
    "record_decision",
    "reject_hunks",
    "reject_paths",
    "render_receipt",
    "render_review",
    "resolve_diff_style",
    "revert_paths",
    "review_command",
    "review_lines",
    "review_text_lines",
    "snapshot_health",
    "tool_evidence",
]

#: The anti-clutter rule, read from the product's own authority rather than
#: restated as a literal. A threshold copied into a second file is a
#: threshold that will drift; this one cannot. The per-file roster is the
#: documented EXEMPTION and is never passed through it - a one-file review
#: that rendered nothing would be a broken surface, not a tidy one.
MIN_REVIEW_ROWS = MIN_SECTION_ENTRIES


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The widget id Prompt 01 mounts. Declared HERE, beside the payload it
#: renders, so the two cannot drift and a test can pin the handoff text.
REVIEW_WIDGET_ID = "vex-review"

#: The additive receipt this module writes. It is deliberately NOT the
#: journal: ``trace.jsonl`` is the append-only record with contiguous
#: sequence allocation, and a second writer guessing at its sequence would
#: corrupt ``replay_run``. Same reasoning as ``trust.json`` and ``redo.json``.
REVIEW_RECEIPT_NAME = "review.json"
REVIEW_RECEIPT_SCHEMA_VERSION = 1

#: What a person may type after ``/diff``. Closed: a new word is an explicit
#: edit here rather than a spelling nobody reviewed.
DIFF_STYLE_CHOICES: Tuple[str, ...] = ("auto", "stacked")

#: The layout ``auto`` can resolve to. ``split`` is the wide-terminal
#: side-by-side; ``stacked`` is the never-broken narrow layout.
RESOLVED_DIFF_STYLES: Tuple[str, ...] = ("split", "stacked")

#: ``auto`` resolves to ``stacked`` at or below this width. A split needs
#: two readable columns plus a gutter; below this it produces a broken
#: layout rather than a narrow one, which is the whole point.
DIFF_STYLE_SPLIT_MIN_COLUMNS = 100

#: The shipped preference. ``auto`` - the layout follows the terminal.
DEFAULT_DIFF_STYLE = "auto"

#: What a file's change MEASURES. Not a verdict: a verdict is a decision,
#: and a measurement cannot decide anything.
REVIEW_FILE_STATES: Tuple[str, ...] = (
    "unchanged",  # byte-identical to the run's reference
    "changed",  # differs, and the reference can restore it
    "unrestorable",  # differs, and there is no trustworthy pre-image
    "unmeasured",  # the path could not be compared at all
)

#: What a PERSON has decided. Separate from the measurement on purpose: an
#: acceptance is not a verification, and a rejection is not a restore.
REVIEW_DECISIONS: Tuple[str, ...] = ("pending", "accepted", "rejected")

#: The closed set a renderer may print for a file's decision. A verdict word
#: this module does not define is a word somebody invented at the call site.
REVIEW_VERDICTS: Tuple[str, ...] = REVIEW_DECISIONS

#: Journal row kinds that carry a mutation receipt with pre/post content
#: hashes. These are what make a concurrent user edit DETECTABLE rather than
#: assumed: the run is accountable for the contents it recorded.
_MUTATION_JOURNAL_KINDS = (
    "edit_applied",
    "edit_rolled_back",
    "edit_refused",
    "edit_result",
    "file_changed",
    "patch_edit",
    "file_change",
)

#: Journal row kinds that name a tool call. The blast radius is folded from
#: these and nothing else, so "which files were read" is a measurement of
#: the run's own record rather than a guess about intent.
_TOOL_JOURNAL_KINDS = ("tool_call", "batch_call", "tool_result", "tool_completed")

#: Tools that READ repository content. Listed so the receipt can separate
#: "read" from "wrote"; the classification is over the recorded tool name
#: and is labelled as such.
_READ_TOOLS = frozenset(
    {
        "read",
        "read_file",
        "glob",
        "grep",
        "search",
        "list",
        "read_symbol",
        "find_definition",
        "find_references",
        "blast_radius",
        "git_blame",
        "git_show",
        "git_log",
        "recall",
        "docs_lookup",
        "lsp_definition",
    }
)

#: Tools that MUTATE repository content.
_WRITE_TOOLS = frozenset(
    {"edit", "write", "apply_patch", "patch", "rename", "delete", "rm", "remove"}
)

#: Tools that run a COMMAND. A command is not a file mutation, so it lands
#: in the blast radius on its own axis.
_COMMAND_TOOLS = frozenset(
    {"bash", "shell", "run", "test", "process", "process_kill", "process_write_stdin"}
)

#: Tools that leave the process.
_EXTERNAL_TOOLS = frozenset({"mcp", "mcp_call", "fetch", "web_fetch", "web_search"})

#: A path token in a recorded command that could be outside the repository.
#: Deliberately narrow: it matches ABSOLUTE forms and home-relative ones
#: only, because a relative token cannot be resolved without knowing the
#: command's working directory, and guessing that would invent findings.
_OUTSIDE_PATH = re.compile(
    r"""(?ix)
    (?:^|[\s'"=;|&(])
    (?:
        ~(?=[/\\])                       # ~/anything
      | [A-Za-z]:[\\/]                   # C:\ or C:/
      | /(?![/*])                        # /absolute, but not /**/
    )
    """
)

#: A `cd` to something that is not a repository-relative path.
_CD_COMMAND = re.compile(r"(?:^|[;&|]\s*)cd\s+(?P<target>\S+)")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotHealth:
    """Whether the run's pre-image reference can still be trusted.

    ``state`` is one of ``ok`` / ``degraded`` / ``missing`` / ``corrupt``.
    ``missing`` and ``corrupt`` are DIFFERENT answers and must stay so: a run
    with no reference at all can still be reviewed against git, while a
    reference that lost a file the run recorded as existing can only be
    reported, because restoring from it would restore from nothing.
    """

    state: str = "ok"
    reference: str = ""
    reason: str = ""
    checked: int = 0
    missing_preimages: Tuple[str, ...] = ()
    mismatched_preimages: Tuple[str, ...] = ()
    unreadable: Tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """True when a restore can be trusted for at least the clean paths."""
        return self.state in {"ok", "degraded"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "reference": self.reference,
            "reason": self.reason,
            "checked": int(self.checked),
            "missing_preimages": list(self.missing_preimages),
            "mismatched_preimages": list(self.mismatched_preimages),
            "unreadable": list(self.unreadable),
            "usable": self.usable,
        }


@dataclass(frozen=True)
class FileReview:
    """One file's measured change, its evidence, and the person's verdict."""

    path: str
    kind: str = "modified"
    state: str = "unchanged"
    decision: str = "pending"
    actor: str = "unknown"
    reason: str = ""
    verified: bool = False
    verification_state: str = "not_run"
    additions: int = 0
    deletions: int = 0
    truncated: bool = False
    binary: bool = False
    run_evidenced: bool = False
    checkpoint_ids: Tuple[str, ...] = ()
    tools: Tuple[str, ...] = ()
    content_hash: str = ""
    pre_image_hash: str = ""
    #: Every content hash the run is accountable for on this path. A live
    #: hash outside this set (and outside the pre-image) is the ONLY
    #: evidence of a concurrent third-party edit.
    accounted_hashes: Tuple[str, ...] = ()
    revert_reason: str = ""
    hunk_headers: Tuple[str, ...] = ()
    diff_lines: Tuple[str, ...] = ()
    #: Per-HUNK decisions as ``(hunk_index, decision)`` pairs, 1-based to
    #: match the ``file#2`` addressing every other surface already speaks.
    #: Empty means nobody has decided a hunk, and the file's own ``decision``
    #: is then the whole story - so a review nobody has touched renders
    #: byte-identically to one recorded before hunks existed.
    hunk_decisions: Tuple[Tuple[int, str], ...] = ()

    @property
    def verdict(self) -> str:
        """The decision word, which is never a verification word.

        With no hunk decisions this is exactly the per-file decision, so
        the property is unchanged for every review recorded before hunk
        granularity existed. With hunk decisions it is a REDUCTION over
        them, and the reduction can say "partly": a file whose hunk 2 is
        rejected and whose hunk 5 is accepted is neither accepted nor
        rejected, and rendering it as either is the lie this avoids.
        """
        base = str(self.decision or "pending")
        if not self.hunk_decisions:
            return base
        words = {str(word) for _, word in self.hunk_decisions}
        if words == {"accepted"}:
            return "accepted"
        if words == {"rejected"}:
            return "rejected"
        return f"{base}/hunks-partial"

    def hunk_verdict(self, index: Any) -> str:
        """One hunk's decision word, or ``pending`` when undecided.

        ``pending`` is the honest answer for a hunk nobody has ruled on.
        It is never rendered as an empty cell and never as ``0``: a
        missing decision is a missing decision.
        """
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            return "pending"
        for number, word in self.hunk_decisions:
            if int(number) == wanted:
                return str(word)
        return "pending"

    def hunk_verdict_summary(self) -> str:
        """``3 accepted / 1 rejected / 4 undecided`` for this file's hunks.

        Undecided hunks are COUNTED rather than dropped, because a summary
        that reads "all 4 hunks accepted" when 4 of 8 were never looked at
        is a fabricated measurement.
        """
        total = len(self.hunk_headers)
        accepted = sum(1 for _, word in self.hunk_decisions if str(word) == "accepted")
        rejected = sum(1 for _, word in self.hunk_decisions if str(word) == "rejected")
        decided = len(self.hunk_decisions)
        undecided = max(0, total - decided)
        return f"{accepted} accepted / {rejected} rejected / {undecided} undecided"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "state": self.state,
            "decision": self.decision,
            "verdict": self.verdict,
            "actor": self.actor,
            "reason": self.reason,
            "verified": self.verified,
            "verification_state": self.verification_state,
            "additions": int(self.additions),
            "deletions": int(self.deletions),
            "truncated": bool(self.truncated),
            "binary": bool(self.binary),
            "run_evidenced": bool(self.run_evidenced),
            "checkpoint_ids": list(self.checkpoint_ids),
            "tools": list(self.tools),
            "content_hash": self.content_hash,
            "pre_image_hash": self.pre_image_hash,
            "accounted_hashes": list(self.accounted_hashes),
            "revert_reason": self.revert_reason,
            "hunk_headers": list(self.hunk_headers),
            "hunk_decisions": [
                [int(number), str(word)] for number, word in self.hunk_decisions
            ],
            "hunk_verdicts": {
                str(number): self.hunk_verdict(number)
                for number in range(1, len(self.hunk_headers) + 1)
            },
        }


@dataclass(frozen=True)
class ReviewDocument:
    """The whole review surface: the run, its files, and the reference health."""

    task_id: str = ""
    repo: str = ""
    status: str = ""
    verdict: str = "unknown"
    verified: bool = False
    verification_state: str = "not_run"
    reviewable: bool = True
    reviewable_reason: str = ""
    diff_style: str = DEFAULT_DIFF_STYLE
    resolved_style: str = "stacked"
    style_reason: str = ""
    width: int = 80
    tree: str = "live"
    #: WHICH tree the run wrote, in one sentence. It rides the headline and
    #: not the notes section because "your working tree was not modified" is
    #: the single most consequential fact this surface can state, and the
    #: anti-clutter rule would hide it as a one-row section.
    tree_note: str = ""
    files: Tuple[FileReview, ...] = ()
    snapshot: SnapshotHealth = field(default_factory=SnapshotHealth)
    decisions: Dict[str, str] = field(default_factory=dict)
    #: ``{path: {hunk_index: decision}}`` - the recorded per-hunk decisions.
    #: Separate from ``decisions`` rather than folded into it because a file
    #: decision and a hunk decision are different questions, and a surface
    #: that read one as the other would report a file as decided when only
    #: one of its five hunks was.
    hunk_decisions: Dict[str, Dict[int, str]] = field(default_factory=dict)
    additions: int = 0
    deletions: int = 0
    unevidenced: Tuple[str, ...] = ()
    disagreement: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-friendly projection. Every value is data, never markup."""
        return {
            "task_id": self.task_id,
            "repo": self.repo,
            "status": self.status,
            "verdict": self.verdict,
            "verified": bool(self.verified),
            "verification_state": self.verification_state,
            "reviewable": bool(self.reviewable),
            "reviewable_reason": self.reviewable_reason,
            "diff_style": self.diff_style,
            "resolved_style": self.resolved_style,
            "style_reason": self.style_reason,
            "width": int(self.width),
            "tree": self.tree,
            "tree_note": self.tree_note,
            "files": [item.to_dict() for item in self.files],
            "snapshot": self.snapshot.to_dict(),
            "decisions": dict(self.decisions),
            "hunk_decisions": {
                str(path): {
                    str(index): str(word) for index, word in sorted(per.items())
                }
                for path, per in self.hunk_decisions.items()
            },
            "totals": {
                "files": len(self.files),
                "additions": int(self.additions),
                "deletions": int(self.deletions),
                "changed": sum(1 for f in self.files if f.state == "changed"),
                "unrestorable": sum(1 for f in self.files if f.state == "unrestorable"),
                "unmeasured": sum(1 for f in self.files if f.state == "unmeasured"),
                "unevidenced": len(self.unevidenced),
            },
            "unevidenced": list(self.unevidenced),
            "disagreement": list(self.disagreement),
            "notes": list(self.notes),
        }

    def path(self) -> str:
        """The reviewed path, or the empty string for an absent one."""
        return str(self.files[0].path) if self.files else ""


@dataclass(frozen=True)
class RevertReceipt:
    """What a revert restored, and - load-bearing - what it could not."""

    ok: bool = False
    status: str = "refused"
    task_id: str = ""
    #: Which tree the revert actually wrote into: ``work`` (the run's own
    #: copy) or ``live`` (the user's working tree). Carried on the receipt
    #: because "your working tree was not modified" is a different fact from
    #: "your working tree is back to where it was", and a receipt that could
    #: not tell them apart would be worse than no receipt.
    tree: str = ""
    paths: Tuple[str, ...] = ()
    restored: Tuple[Dict[str, Any], ...] = ()
    unchanged: Tuple[str, ...] = ()
    not_restored: Tuple[Dict[str, Any], ...] = ()
    deleted: Tuple[str, ...] = ()
    overwritten_user_edits: Tuple[Dict[str, Any], ...] = ()
    forced: bool = False
    verified: bool = False
    verified_paths: int = 0
    checked_paths: int = 0
    snapshot_state: str = "ok"
    receipt_path: str = ""
    written: bool = False
    reason: str = ""
    started_at: float = 0.0
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": REVIEW_RECEIPT_SCHEMA_VERSION,
            "ok": bool(self.ok),
            "status": self.status,
            "task_id": self.task_id,
            "tree": self.tree,
            "paths": list(self.paths),
            "restored": [dict(item) for item in self.restored],
            "unchanged": list(self.unchanged),
            "not_restored": [dict(item) for item in self.not_restored],
            "deleted": list(self.deleted),
            "overwritten_user_edits": [
                dict(item) for item in self.overwritten_user_edits
            ],
            "overwritten_count": len(self.overwritten_user_edits),
            "forced": bool(self.forced),
            "verified": bool(self.verified),
            "verified_paths": int(self.verified_paths),
            "checked_paths": int(self.checked_paths),
            "snapshot_state": self.snapshot_state,
            "receipt_path": self.receipt_path,
            "written": bool(self.written),
            "reason": self.reason,
            "started_at": float(self.started_at),
            "duration_ms": float(self.duration_ms),
        }


@dataclass(frozen=True)
class CommandTools:
    """One recorded command and whether its text reached outside the repo.

    ``outside`` is a LOWER BOUND derived from the recorded command text
    alone. A command can reach anywhere without naming a path, and this
    record does not claim otherwise - ``detection`` names what was examined
    so a reader can weigh the evidence.
    """

    command: str
    tool: str = ""
    outside: bool = False
    reason: str = ""
    cwd: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "tool": self.tool,
            "outside": bool(self.outside),
            "reason": self.reason,
            "cwd": self.cwd,
        }


# ---------------------------------------------------------------------------
# Style resolution
# ---------------------------------------------------------------------------


def _bounded_int(value: Any, default: int = 0, floor: int = 0) -> int:
    try:
        return max(floor, int(value))
    except (TypeError, ValueError):
        return default


def resolve_diff_style(
    preference: Any = DEFAULT_DIFF_STYLE, width: Any = 80
) -> Tuple[str, str]:
    """Resolve ``diff_style`` against a terminal width.

    Returns ``(resolved, reason)`` where ``resolved`` is one of
    :data:`RESOLVED_DIFF_STYLES`. The reason is not decoration: a layout
    that silently changed because a window got narrower is a layout nobody
    chose, and the receipt has to be able to say which one ran.

    The rules, in order:

    * an unrecognised preference degrades to ``auto``. It does NOT raise:
      a typo in a settings file must not make ``/diff`` unusable, and
      ``auto`` is the value that is safe at every width.
    * ``stacked`` is honoured unconditionally. A caller that asks for the
      never-broken layout gets it even on a wide terminal, because a caller
      that knows its own width better than the probe is entitled to the
      answer.
    * ``auto`` resolves to ``split`` at or above
      :data:`DIFF_STYLE_SPLIT_MIN_COLUMNS` and to ``stacked`` below it.
    * an unmeasurable width - zero, negative, non-numeric, or a pipe with
      no ``isatty`` - resolves to ``stacked``. "I do not know how wide this
      is" must never resolve to the layout that needs width to survive.
    """
    asked = str(preference or "").strip().lower()
    reason = ""
    if asked not in DIFF_STYLE_CHOICES:
        reason = f"unrecognised diff_style {preference!r}; using {DEFAULT_DIFF_STYLE!r}"
        asked = DEFAULT_DIFF_STYLE
    try:
        columns = int(width)
    except (TypeError, ValueError):
        columns = 0
    if columns <= 0:
        return (
            "stacked",
            reason or "terminal width unknown; stacked is the layout that cannot break",
        )
    if asked == "stacked":
        return ("stacked", reason or "diff_style=stacked was requested explicitly")
    if columns < DIFF_STYLE_SPLIT_MIN_COLUMNS:
        return (
            "stacked",
            reason
            or (
                f"{columns} columns is below the {DIFF_STYLE_SPLIT_MIN_COLUMNS}-column "
                "split floor; a side-by-side diff would be unreadable, not narrow"
            ),
        )
    return (
        "split",
        reason
        or (
            f"{columns} columns is at or above the {DIFF_STYLE_SPLIT_MIN_COLUMNS}-column "
            "split floor; rendering removals and additions side by side"
        ),
    )


# ---------------------------------------------------------------------------
# Bounded text
# ---------------------------------------------------------------------------

#: The truncation marker. Deliberately ASCII: a review receipt is a LOG
#: artifact that gets copied into issues and terminals with unknown
#: encodings, and a marker that raises ``UnicodeEncodeError`` on cp1252 is
#: a receipt that cannot be read exactly when it is needed.
_ELLIPSIS = "..."

#: What a line reads as when the sanitiser cannot be reached at all. It is
#: declared HERE, in the module that withholds, so it cannot itself be a
#: f-string through the broken path. ASCII on purpose, like ``_ELLIPSIS``.
_WITHHELD_NO_SANITISER = "(detail withheld: sanitiser unavailable)"


#: Resolved once, lazily, and CACHED. ``review.py`` imports no rendering
#: module at module scope (it must stay importable on a host without rich,
#: and the surface is a pure projection of a run's own records), so the
#: sanitiser is bound on first use and reused after.
#:
#: WHY IT LIVES HERE AND NOT AT THE RENDER SINK: ``_bound`` is the single
#: function every rendered review line passes through - the headline, the
#: file roster, the hunk headers, the diff lines, the notes, the receipt
#: words. Sanitising at each call site would be a list that can be short.
#: This is the same shape as ``cli.fileview._sanitize_diff``, which the
#: P0 sanitiser audit named as the model: sanitise at the boundary the
#: content crosses, not at the place it is printed. A review renders a
#: unified diff of a file a run wrote, and a file's bytes are DATA.
_SANITIZE = None
_SANITIZE_RESOLVED = False


def _sanitizer():
    """Return ``cli.ui.sanitize_text``, resolved once, or ``None``.

    ``None`` means the sanitiser could not be imported AT ALL, and every
    caller then withholds rather than printing. That is the fail-closed
    direction: a review that cannot be sanitised is a review that shows
    ``(detail withheld: sanitiser unavailable)`` instead of the diff, which
    is inconvenient, over a review that prints a credential, which is the
    leak this whole function exists to prevent.
    """
    global _SANITIZE, _SANITIZE_RESOLVED
    if not _SANITIZE_RESOLVED:
        _SANITIZE_RESOLVED = True
        try:
            from cli import ui as _ui

            _SANITIZE = getattr(_ui, "sanitize_text", None)
        except Exception:
            _SANITIZE = None
    return _SANITIZE


def _bound(text: Any, width: int, *, marker: str = _ELLIPSIS) -> str:
    """Bound one line to ``width`` characters, marking what was cut.

    A bounded identifier is still an identifier; a clipped sentence stops
    being true at the cut. So the cut is always marked, and the mark
    itself is budgeted so the result is never LONGER than ``width``.

    **THE SANITISER CONTRACT.** Every rendered review line goes through this
    function, and every rendered review line is sanitised here. The ORDER is
    load-bearing and is not incidental: this function strips control
    characters (which removes the ANSI escapes that SPLIT a credential into
    visually-contiguous bytes) and only then hands the reassembled text to
    ``cli.ui.sanitize_text``, which redacts. Redacting first would protect a
    string the reader never sees and the strip would then reassemble a live
    credential out of the pieces - the exact defect P0 found and fixed in
    ``cli/ui.py``.

    A sanitiser that is unavailable, or that RAISES, withholds the value.
    A renderer that fails closed by rendering the raw value is a renderer
    that fails open.
    """
    value = str(text or "").replace("\t", " ").replace("\r", " ")
    value = "".join(
        character for character in value if character >= " " or character == " "
    )
    sanitize = _sanitizer()
    if sanitize is None:
        value = _WITHHELD_NO_SANITISER
    else:
        try:
            value = str(sanitize(value) or "")
        except Exception:
            value = _WITHHELD_NO_SANITISER
    limit = max(8, _bounded_int(width, 80, 1))
    if len(value) <= limit:
        return value
    keep = max(1, limit - len(marker))
    return value[:keep].rstrip() + marker


# ---------------------------------------------------------------------------
# Reading the run's own record
# ---------------------------------------------------------------------------


def _task_dir(value: Any) -> Optional[Path]:
    try:
        candidate = Path(str(value)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate if candidate.is_dir() else None


def _journal_rows(directory: Optional[Path]) -> List[Dict[str, Any]]:
    """Every well-formed row of the run's journal, in order.

    Tolerant by contract: a torn tail, a half-written line, or a non-object
    row is SKIPPED, because a run that is still writing is the normal case
    for a mid-run review and a review that raises on a torn line is a
    review that cannot run mid-run.
    """
    if directory is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        raw = (directory / "trace.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _row_parts(row: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """``(kind, payload)`` accepting both the normalized and legacy shapes."""
    kind = str(row.get("event") or row.get("kind") or "")
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        payload = row.get("data")
    if not isinstance(payload, Mapping):
        payload = row
    return kind, dict(payload)


def _row_relative(value: Any) -> str:
    """A repository-relative POSIX path, or the empty string.

    Uses the module's OWN normaliser so a path that the review will render
    is the same path the review will hash, resolve and revert.
    """
    text = str(value or "").replace("\\", "/").strip()
    if not text or "\x00" in text:
        return ""
    candidate = text[2:] if text[:2] == "./" else text
    if not candidate or candidate.startswith("/"):
        return ""
    if re.match(r"^[A-Za-z]:", candidate):
        return ""
    parts = [part for part in candidate.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def tool_evidence(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Fold the run's journal into per-path and per-tool facts.

    Returns the accounting the whole module rests on:

    ``hashes``   path -> every content hash the run recorded for it. A live
                  content hash outside this set is the only evidence of a
                  concurrent third-party edit.
    ``pre_hashes`` path -> the hashes recorded as the file's PRE-image. A
                  path here is a file the reference is supposed to hold, and
                  the reference's bytes must still hash to one of them. A
                  path the run only ever CREATED has no pre-image, so its
                  absence from the reference is not corruption.
    ``tools``    path -> the tool names that touched it, in order.
    ``read``     paths a read-shaped tool named.
    ``written``  paths a write-shaped tool named.
    ``commands`` the recorded command strings, in order.
    ``planned``  paths the plan declared through ``files_hint``.
    ``muts``     the number of mutation receipts seen.

    Every value comes from the run's OWN rows. Nothing here infers intent:
    a tool named in a row is reported as having been called, which is a
    fact, and not as having done what its name suggests, which is not.
    """
    hashes: Dict[str, set] = {}
    pre_hashes: Dict[str, set] = {}
    tools: Dict[str, List[str]] = {}
    read: List[str] = []
    written: List[str] = []
    commands: List[str] = []
    planned: List[str] = []
    muts = 0

    def _remember(path: str, tool: str) -> None:
        if not path:
            return
        bucket = tools.setdefault(path, [])
        if tool and tool not in bucket:
            bucket.append(tool)

    def _digest(value: Any) -> str:
        text = str(value or "").strip().lower()
        return text if re.match(r"^[0-9a-f]{64}$", text) else ""

    for row in rows:
        kind, data = _row_parts(row)
        if not kind:
            continue
        if kind in _MUTATION_JOURNAL_KINDS:
            muts += 1
            path = _row_relative(
                data.get("path")
                or data.get("file")
                or data.get("target")
                or data.get("relative_path")
            )
            if not path:
                continue
            bucket = hashes.setdefault(path, set())
            for key in (
                "pre_sha256",
                "post_sha256",
                "sha256",
                "digest",
                "content_sha256",
            ):
                value = _digest(data.get(key))
                if value:
                    bucket.add(value)
            # The PRE-image is tracked SEPARATELY and on purpose. Merging it
            # into the accounted set is fine for the concurrent-edit rule,
            # but it would make a run-CREATED file indistinguishable from a
            # run-EDITED one here, and the reference would then be reported
            # corrupt for the absence of a file that never existed.
            pre = _digest(data.get("pre_sha256") or data.get("before_sha256"))
            if pre:
                pre_hashes.setdefault(path, set()).add(pre)
            _remember(path, str(data.get("tool") or data.get("kind") or kind).lower())
        if (
            kind not in _TOOL_JOURNAL_KINDS
            and kind not in _MUTATION_JOURNAL_KINDS
            and kind != "plan"
        ):
            # A `plan` row is in neither set and is still folded: it is the
            # ONLY place the run declares what it intends to touch, so
            # skipping it here is what made "scope creep" a signal that could
            # never fire - the plan set was always empty, and an empty plan
            # set correctly suppresses the warning.
            continue
        arguments = data.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = data.get("args")
        if not isinstance(arguments, Mapping):
            arguments = {}
        tool = str(data.get("tool") or data.get("name") or "").lower()
        if kind == "tool_call" and not tool:
            # The legacy fix loop journals a shell command with no tool name.
            command = str(arguments.get("command") or data.get("command") or "")
            if command:
                tool = "bash"
                if command not in commands:
                    commands.append(command)
        if tool in _WRITE_TOOLS:
            path = _row_relative(
                arguments.get("path")
                or arguments.get("file")
                or data.get("path")
                or data.get("target")
            )
            if path:
                if path not in written:
                    written.append(path)
                _remember(path, tool)
        elif tool in _READ_TOOLS:
            for key in ("path", "file", "target", "pattern", "query", "symbol"):
                path = _row_relative(arguments.get(key) or data.get(key))
                if path and path not in read:
                    read.append(path)
                    _remember(path, tool)
        elif tool in _COMMAND_TOOLS:
            command = str(arguments.get("command") or data.get("command") or "")
            if command and command not in commands:
                commands.append(command)
            _remember(_row_relative(arguments.get("path")), tool)
        if tool in _EXTERNAL_TOOLS:
            _remember(_row_relative(arguments.get("path") or data.get("path")), tool)
        if kind == "plan" or (tool in {"plan", "todo"} and arguments.get("plan")):
            # The step list is read from the ROW PAYLOAD first, because that
            # is where the harness writes it (``trace.log("plan", {"plan":
            # [...]})``), and only from the tool arguments as a fallback.
            # Reading only the arguments left the plan set permanently empty
            # on every real run, which silently DISABLED the scope-creep
            # signal instead of reporting it - a gate that cannot fail.
            steps: Any = None
            for container in (data, arguments):
                for key in ("plan", "steps", "items"):
                    value = (
                        container.get(key) if isinstance(container, Mapping) else None
                    )
                    if isinstance(value, list):
                        steps = value
                        break
                if steps is not None:
                    break
            for step in steps or []:
                if not isinstance(step, Mapping):
                    continue
                hints = step.get("files_hint")
                if not isinstance(hints, list):
                    fallback = step.get("files")
                    hints = fallback if isinstance(fallback, list) else []
                for hint in hints:
                    path = _row_relative(hint)
                    if path and path not in planned:
                        planned.append(path)
        for hint in data.get("files_hint") or []:
            path = _row_relative(hint)
            if path and path not in planned:
                planned.append(path)

    return {
        "hashes": {path: tuple(sorted(values)) for path, values in hashes.items()},
        "pre_hashes": {
            path: tuple(sorted(values)) for path, values in pre_hashes.items()
        },
        "tools": {path: tuple(values) for path, values in tools.items()},
        "read": tuple(read),
        "written": tuple(written),
        "commands": tuple(commands),
        "planned": tuple(planned),
        "mutations": muts,
    }


# ---------------------------------------------------------------------------
# Snapshot health
# ---------------------------------------------------------------------------


def snapshot_health(
    task_dir: Any,
    repo: Any = None,
    *,
    known: Optional[Mapping[str, str]] = None,
) -> SnapshotHealth:
    """Report whether the run's pre-image reference can still be trusted.

    The interesting failure is not "there is no reference" - that is an
    honest, expected state for a run whose reference was never captured, and
    the review can still diff against git. The interesting failure is a
    reference that no longer matches what the run recorded about it:

    * the run's receipt says ``pre_sha256`` for a path and the reference no
      longer holds that file at all;
    * or the reference holds the file but its bytes no longer hash to the
      ``pre_sha256`` the run recorded.

    Both are :data:`CORRUPT`, reported per path, and refused by the revert.
    A tampered pre-image is the more dangerous of the two, because a
    presence-only check would happily restore from it and call the result
    verified - and a "verified" restore from altered bytes is the worst
    receipt this module could emit.

    ``corrupt`` never means "the diff looks empty" - an empty diff from a
    corrupt reference is exactly the silent failure this exists to stop.

    ``known`` supplies digests the caller has already measured, so a review
    that needs the same hash for a file's state does not read those bytes a
    second time. A caller that passes a STALE map inherits the staleness -
    that is why the map is a parameter of a single call rather than a
    module-level cache with a lifetime nobody controls.
    """
    directory = _task_dir(task_dir)
    if directory is None:
        return SnapshotHealth(
            state="missing",
            reason="no run directory is readable for this task id",
        )
    reference = directory / "pristine"
    if not reference.is_dir():
        return SnapshotHealth(
            state="missing",
            reference="",
            reason=(
                "this run captured no pristine reference; the review falls back to "
                "git and a revert has nothing verified to restore from"
            ),
        )
    rows = _journal_rows(directory)
    evidence = tool_evidence(rows)
    missing: List[str] = []
    mismatched: List[str] = []
    unreadable: List[str] = []
    checked = 0
    for path, digests in evidence["pre_hashes"].items():
        if not digests:
            continue
        checked += 1
        target = _fv.safe_repo_path(reference, path)
        if target is None or not _fv.path_exists(target):
            missing.append(path)
            continue
        actual = (
            str(known.get(path) or "")
            if known is not None and path in known
            else (_fv.content_hash(target) or "")
        )
        if not actual:
            unreadable.append(path)
        elif actual not in digests:
            mismatched.append(path)
    state = "ok"
    reason = f"{checked} recorded pre-image(s) verified byte-for-byte"
    if missing or mismatched or unreadable:
        state = "corrupt"
        parts = []
        if missing:
            parts.append(f"{len(missing)} recorded pre-image(s) are gone")
        if mismatched:
            parts.append(
                f"{len(mismatched)} recorded pre-image(s) no longer match their hash"
            )
        if unreadable:
            parts.append(f"{len(unreadable)} pre-image(s) are unreadable")
        reason = (
            "; ".join(parts)
            + " - the reference no longer matches what the run recorded, so a "
            "restore from it would restore from nothing or from altered bytes"
        )
    return SnapshotHealth(
        state=state,
        reference=str(reference),
        reason=reason,
        checked=checked,
        missing_preimages=tuple(sorted(missing)),
        mismatched_preimages=tuple(sorted(mismatched)),
        unreadable=tuple(sorted(unreadable)),
    )


# ---------------------------------------------------------------------------
# The review document
# ---------------------------------------------------------------------------


def _tree_choice(
    directory: Optional[Path], root: Optional[Path]
) -> Tuple[str, Optional[Path], str]:
    """Which tree the run WROTE, and the one sentence that says so.

    A verified-fix run edits its own ``work/`` copy and never touches the
    live repository, so the thing a person reviews is ``work/`` against
    ``pristine/`` - and the review must SAY that, because "your working tree
    was not modified" is a materially different fact from "your working tree
    changed". An agent run edits the live repository in place and has no
    ``work/`` copy, so the live repository IS the run's output.
    """
    if directory is None:
        return ("none", None, "no run directory is readable; nothing is measurable")
    work = directory / "work"
    reference = directory / "pristine"
    if work.is_dir():
        return (
            "work",
            work,
            "this run delivered into its own work copy; your working tree was not modified",
        )
    if root is None:
        return (
            "none",
            None,
            "no work copy and no readable repository; the diff cannot be measured",
        )
    if reference.is_dir():
        return ("live", root, "this run edited the working tree in place")
    return (
        "live",
        root,
        "this run edited the working tree in place and captured no pristine reference",
    )


def _harness_changed(
    directory: Optional[Path], tree: Optional[Path]
) -> Tuple[str, ...]:
    """The harness's OWN changed-file set, for the cross-check.

    ``harness.editor.changed_files`` is the definition the run itself used.
    Reading it here rather than re-deriving the comparison is what makes the
    cross-check a cross-check. A missing editor module degrades to "no
    opinion" rather than to "no changes".
    """
    if directory is None or tree is None:
        return ()
    reference = directory / "pristine"
    if not reference.is_dir():
        return ()
    try:
        from harness import editor
    except Exception:
        return ()
    try:
        return tuple(
            str(item) for item in editor.changed_files(str(reference), str(tree))
        )
    except Exception:
        return ()


def _live_hashes(
    root: Optional[Path], path: str, known: Optional[Mapping[str, str]] = None
) -> Tuple[str, bool]:
    """``(sha256, exists)`` for a repository-relative path, safely.

    ``known`` is the caller's already-measured digest map. A review hashes
    each file once, not once per consumer, and on a host where a small read
    costs milliseconds the duplicate read IS the latency.
    """
    if root is None:
        return ("", False)
    target = _fv.safe_repo_path(root, path)
    if target is None:
        return ("", False)
    if known is not None and path in known:
        return (str(known.get(path) or ""), _fv.path_exists(target))
    return (_fv.content_hash(target) or "", _fv.path_exists(target))


def _reference_hash(
    directory: Optional[Path],
    path: str,
    known: Optional[Mapping[str, str]] = None,
) -> Tuple[str, bool]:
    """``(sha256, exists)`` for the run's pre-image of ``path``."""
    if directory is None:
        return ("", False)
    reference = directory / "pristine"
    if not reference.is_dir():
        return ("", False)
    target = _fv.safe_repo_path(reference, path)
    if target is None:
        if (reference / path).exists():
            # Present but refused by the guard: readable-as-nothing is the
            # honest answer, and the caller treats it as unrestorable.
            return ("", True)
        return ("", False)
    if known is not None and path in known:
        return (str(known.get(path) or ""), _fv.path_exists(target))
    return (_fv.content_hash(target) or "", _fv.path_exists(target))


def build_review(
    task_dir: Any,
    repo: Any = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
    width: Any = 80,
    max_files: Any = 200,
    max_diff_lines: Any = 80,
    include_diff_lines: bool = True,
    include_git: bool = False,
    thorough: bool = True,
) -> ReviewDocument:
    """Build the review document for one run. Read-only; never mutates.

    Every fact in the result is MEASURED from the run's own artifacts: the
    change set from the reference tree against the tree the run wrote, the
    hashes from the bytes, the evidence from the journal, and the run's
    verdict from :func:`cli.runview.run_verdict` (the one fail-closed
    reduction). A file's ``verified`` flag is
    :func:`cli.fileview.file_change_verified`, so a per-file claim can only
    CONFIRM a run that already proved itself and can never create one.

    ``completed_unverified`` is a fully reviewable state: the document is
    built, every action is available, and the verdict says ``unverified``.

    ``include_git`` is OFF by default and that default is a decision, not a
    default. With git on, a file on a SHARED working tree that a teammate
    edited appears in the change set - and the review's subject is "what
    did this run do to my repository", so a surface that listed a
    teammate's uncommitted work under it would be making the same
    attribution lie the file layer already fixed. The live indicator turns
    it ON and then reports those rows as ``unevidenced`` rather than as this
    run's work, which is the one place seeing them is useful.

    ``thorough`` is the I/O knob and it is fail-closed about itself. The
    two expensive checks - verifying every recorded pre-image against its
    hash, and cross-checking the harness's own change set - are O(the whole
    run), measured at seconds for a 200-file change set on a host where a
    small file read costs milliseconds. A review a person is reading is
    worth that. A live indicator polled every tick is not, so it passes
    ``thorough=False``; the document then reports
    ``snapshot.state == "unchecked"`` and a note naming both skipped
    checks. It NEVER reports them as passing - a check that did not run and
    a check that passed are different answers, and conflating them is how a
    bound becomes a lie.
    """
    directory = _task_dir(task_dir)
    root = _fv.repo_root(repo) if repo is not None else _fv.repo_root(os.getcwd())
    tree_name, tree, tree_note = _tree_choice(directory, root)
    columns = _bounded_int(width, 80, 0)
    style, style_reason = resolve_diff_style(_config_style(config), columns)

    rows = _journal_rows(directory)
    evidence = tool_evidence(rows)
    recorded = read_review_receipt(task_dir)

    projection: Dict[str, Any] = {}
    status = ""
    verification_state = "not_run"
    verdict = "unknown"
    try:
        from cli.runview import read_live_projection, run_verdict

        if directory is not None:
            projection = read_live_projection(directory)
        status = str(projection.get("status") or "")
        verification_state = str(projection.get("verification_state") or "not_run")
        evidence_rows = projection.get("verification_evidence") or []
        if not evidence_rows and isinstance(projection.get("result"), Mapping):
            result = projection["result"]
            single = result.get("verification_evidence") or result.get("verification")
            evidence_rows = (
                single if isinstance(single, list) else ([single] if single else [])
            )
        verdict = run_verdict(
            status,
            verification_state=verification_state,
            evidence=evidence_rows,
        )
    except Exception:
        projection = {}
        status = ""
        verification_state = "not_run"
        verdict = "unknown"

    file_projection: Dict[str, Any] = {}
    if directory is not None and tree is not None:
        try:
            file_projection = _fv.build_file_projection(
                directory,
                root,
                # The journal was already folded above; handing the
                # projection over means the file layer does not read and
                # fold the same journal a second time.
                snapshot=projection or None,
                include_git=bool(include_git),
                max_files=_bounded_int(max_files, 200, 1),
                max_diff_lines=_bounded_int(max_diff_lines, 80, 1),
            )
        except Exception:
            file_projection = {}

    records = _projection_records(file_projection)

    # Hash each reference file ONCE. The reference health check and each
    # file's own state need the same digest, and reading it twice was
    # measured as the single largest cost of building a review. The scope
    # is the paths this document will actually examine - plus every
    # journalled pre-image when the caller asked for the thorough check,
    # because a bound that silently stopped verifying corruption would be
    # the opposite of a bound.
    wanted: List[str] = [_row_relative(item.get("path")) for item in records]
    if thorough:
        wanted += list(evidence["pre_hashes"])
    reference_digests: Dict[str, str] = {}
    for path in wanted:
        if not path or path in reference_digests:
            continue
        digest, _present = _reference_hash(directory, path)
        if digest:
            reference_digests[path] = digest
    if thorough:
        health = snapshot_health(task_dir, repo, known=reference_digests)
    else:
        health = SnapshotHealth(
            state="unchecked",
            reference=str(directory / "pristine") if directory is not None else "",
            reason=(
                "this sample skipped the pre-image integrity check and the "
                "harness cross-check to stay cheap enough to poll; neither was "
                "run, and neither passed"
            ),
        )

    cross = _harness_changed(directory, tree) if thorough else ()
    cross_set = set(cross)
    review_files: List[FileReview] = []
    unevidenced: List[str] = []
    notes: List[str] = []
    additions = 0
    deletions = 0
    limit = _bounded_int(max_files, 200, 1)

    for record in records[:limit]:
        path = _row_relative(record.get("path"))
        if not path:
            continue
        content_digest, content_exists = _live_hashes(tree, path)
        pre_digest, pre_exists = _reference_hash(
            directory, path, known=reference_digests
        )
        run_digests = tuple(evidence["hashes"].get(path, ()))
        # The reference's own digest is part of what the run is accountable
        # for: restoring to it IS the operation, and treating "the file is
        # already back to its pre-image" as a third-party edit would make an
        # idempotent revert impossible.
        accounted = tuple(
            sorted(set(run_digests) | ({pre_digest} if pre_digest else set()))
        )
        state, revert_reason = _file_state(
            content_digest=content_digest,
            content_exists=content_exists,
            pre_digest=pre_digest,
            pre_exists=pre_exists,
            health=health,
            path=path,
        )
        additions += _bounded_int(record.get("additions"), 0, 0)
        deletions += _bounded_int(record.get("deletions"), 0, 0)
        run_evidenced = bool(record.get("run_evidenced"))
        if not run_evidenced and state != "unchanged":
            unevidenced.append(path)
        hunks = record.get("hunks") if isinstance(record.get("hunks"), list) else []
        headers = tuple(
            str(hunk.get("header") or "") for hunk in hunks if isinstance(hunk, Mapping)
        )
        lines: Tuple[str, ...] = ()
        if include_diff_lines:
            collected: List[str] = []
            for hunk in hunks:
                if not isinstance(hunk, Mapping):
                    continue
                for line in hunk.get("lines") or []:
                    collected.append(str(line))
            lines = tuple(collected[: max(0, _bounded_int(max_diff_lines, 80, 0))])
        review_files.append(
            FileReview(
                path=path,
                kind=str(record.get("kind") or "modified"),
                state=state,
                decision=str((recorded.get("decisions") or {}).get(path) or "pending"),
                actor=str(record.get("actor") or "unknown"),
                reason=str(record.get("reason") or ""),
                verified=bool(record.get("verified")),
                verification_state=str(
                    record.get("verification_state") or verification_state
                ),
                additions=_bounded_int(record.get("additions"), 0, 0),
                deletions=_bounded_int(record.get("deletions"), 0, 0),
                truncated=bool(record.get("truncated")),
                binary=bool(record.get("binary")),
                run_evidenced=run_evidenced,
                checkpoint_ids=tuple(
                    str(item) for item in (record.get("checkpoint_ids") or [])
                ),
                tools=tuple(evidence["tools"].get(path, ())),
                content_hash=content_digest,
                pre_image_hash=pre_digest,
                accounted_hashes=accounted,
                revert_reason=revert_reason,
                hunk_headers=headers,
                diff_lines=lines,
                hunk_decisions=tuple(
                    sorted(
                        (int(index), str(word))
                        for index, word in (
                            (recorded.get("hunk_decisions") or {}).get(path) or {}
                        ).items()
                    )
                ),
            )
        )

    disagreement: Tuple[str, ...] = ()
    if cross:
        measured = {item.path for item in review_files if item.state != "unchanged"}
        only_cross = tuple(sorted(cross_set - measured))
        only_measured = tuple(sorted(measured - cross_set))
        if only_cross or only_measured:
            disagreement = tuple(
                [
                    f"{len(only_cross)} path(s) the harness measured as changed are "
                    "absent from this review",
                    f"{len(only_measured)} path(s) this review measured are absent "
                    "from the harness's own change set",
                ]
            )
    if health.state == "corrupt":
        notes.append(
            "the pristine reference lost a file the run recorded as existing; "
            "those paths are reported and refused, not restored"
        )
    if not thorough:
        notes.append(
            "a cheap sample was requested: the pre-image integrity check and the "
            "harness cross-check did not run for this read"
        )
    if limit < len(records):
        notes.append(f"bounded to {limit} file(s) of {len(records)} measured")

    reviewable = True
    reviewable_reason = (
        "every action is available; verification describes the run, not this surface"
    )
    return ReviewDocument(
        task_id=str(directory.name) if directory is not None else "",
        repo=str(root) if root is not None else "",
        status=status,
        verdict=str(verdict or "unknown"),
        verified=bool(verdict == "verified"),
        verification_state=verification_state,
        reviewable=reviewable,
        reviewable_reason=reviewable_reason,
        diff_style=_config_style(config),
        resolved_style=style,
        style_reason=style_reason,
        width=columns,
        tree=tree_name,
        tree_note=tree_note,
        files=tuple(review_files),
        snapshot=health,
        decisions={
            str(key): str(value)
            for key, value in (recorded.get("decisions") or {}).items()
        },
        hunk_decisions={
            str(path): {int(index): str(word) for index, word in dict(per).items()}
            for path, per in (recorded.get("hunk_decisions") or {}).items()
        },
        additions=additions,
        deletions=deletions,
        unevidenced=tuple(unevidenced),
        disagreement=disagreement,
        notes=tuple(notes),
    )


def _projection_records(projection: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The per-file records a file projection carries, as plain dicts."""
    diff = projection.get("diff")
    source: Any = None
    if isinstance(diff, Mapping):
        source = diff.get("files")
    if not isinstance(source, list):
        source = projection.get("file_changes")
    if not isinstance(source, list):
        return []
    return [dict(item) for item in source if isinstance(item, Mapping)]


def _file_state(
    *,
    content_digest: str,
    content_exists: bool,
    pre_digest: str,
    pre_exists: bool,
    health: SnapshotHealth,
    path: str,
) -> Tuple[str, str]:
    """Measure one file's state and say why a restore is or is not possible.

    This is the whole ``(state, reason)`` decision, and each branch names a
    different real situation rather than defaulting to a guess:

    * neither side readable                  -> ``unmeasured``
    * live equals the pre-image             -> ``unchanged``
    * the reference is corrupt for this path -> ``unrestorable``
    * no pre-image and the file is gone      -> ``unchanged`` (already back)
    * no pre-image and the file is there     -> ``unrestorable`` unless the
      run can prove it created it, because DELETING a file the reference
      never held is the one irreversible thing this module could do
    * the run DELETED a file the reference holds -> ``changed``, because
      the pre-image is on disk and writing it back is the whole operation.
      Reading "the file is gone" as "nothing to do" is how a run that
      deleted your file gets reported as an empty diff.
    """
    if not content_exists and not pre_exists:
        return ("unchanged", "absent from both the reference and the tree")
    if pre_digest and content_digest == pre_digest:
        return ("unchanged", "byte-identical to its pre-image")
    if path in health.missing_preimages:
        return (
            "unrestorable",
            "the pristine reference lost this file although the run recorded a "
            "pre-image for it; the reference is corrupt, not the file",
        )
    if path in health.mismatched_preimages:
        return (
            "unrestorable",
            "the pristine copy of this file no longer hashes to the pre-image the "
            "run recorded, so restoring from it would put back altered bytes",
        )
    if path in health.unreadable:
        return ("unrestorable", "the pristine reference copy is not a readable file")
    if not pre_exists:
        if not content_exists:
            return ("unchanged", "absent from both the reference and the tree")
        return (
            "unrestorable",
            "the reference holds no pre-image and the run recorded none, so a "
            "restore would have to DELETE a file nothing can vouch for",
        )
    if not content_exists:
        return ("changed", "the run deleted a file the reference still holds")
    if not pre_digest or not content_digest:
        return (
            "unmeasured",
            "one side of the comparison could not be hashed; nothing is claimed",
        )
    return ("changed", "")


def _config_style(config: Optional[Mapping[str, Any]]) -> str:
    """Read ``review_diff_style`` from a run config BY KEY PRESENCE.

    Key presence rather than truthiness, and no entry in
    ``harness/config.py::DEFAULTS``: a default there is merged into every
    task and every eval arm, so publishing a layout preference would
    silently switch every run in the project.

    The value is returned AS ASKED, including a value the resolver will
    not accept. Degrading it here would erase the only evidence that a
    configured preference was a typo - the document would echo ``auto`` as
    though that is what the run asked for, and the reason string could not
    say the word ``unrecognised`` because nothing would know any other word
    was ever supplied. The resolver owns the decision and the explanation.
    """
    if not isinstance(config, Mapping):
        return DEFAULT_DIFF_STYLE
    if "review_diff_style" not in config:
        return DEFAULT_DIFF_STYLE
    return str(config.get("review_diff_style") or "").strip().lower()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _headline(document: ReviewDocument, width: int) -> List[str]:
    verdict = str(document.verdict or "unknown")
    marker = "verified" if verdict == "verified" else "NOT verified"
    head = (
        f"review {document.task_id or '(no task)'} - "
        f"{len(document.files)} file(s) +{document.additions} -{document.deletions}"
    )
    lines = [_bound(head, width)]
    lines.append(
        _bound(
            f"run status {document.status or 'unknown'} - {marker} - "
            f"layout {document.resolved_style} ({document.diff_style} requested)",
            width,
        )
    )
    if document.verdict != "verified":
        lines.append(
            _bound(
                "this run is NOT verified. the diff is still yours to read, accept, "
                "reject or revert - unverified means unproven, not unreviewable",
                width,
            )
        )
    if document.tree_note:
        lines.append(_bound(document.tree_note, width))
    return lines


def review_lines(
    document: ReviewDocument, *, expanded: Iterable[str] = ()
) -> List[str]:
    """Render a review document as PLAIN, unstyled lines.

    Plain by construction, not by convention: every string that reaches this
    function came from a repository path, a file's content, or a journal row,
    and a line carrying a ``[`` from any of those would be eaten by a markup
    parser rather than printed. Nothing here emits markup, so both shells
    can render these identically and a hostile path cannot delete a message.

    Layout, in order: the headline, the reference health, the per-file
    roster COLLAPSED by default, the evidence section, and the notes. The
    evidence and notes sections obey the anti-clutter rule - a section with
    fewer than :data:`MIN_REVIEW_ROWS` entries is a heading plus a fact the
    reader already has somewhere else, and that is noise. The per-file
    roster is the review's SUBJECT, not a section of it, and is the one
    documented exemption: a one-file review that renders nothing would be a
    broken surface, not a tidy one.
    """
    width = max(40, _bounded_int(document.width, 80, 1))
    want = {str(item) for item in expanded or ()}
    lines = _headline(document, width)
    health = document.snapshot
    if health.state != "ok":
        lines.append(
            _bound(f"pristine reference: {health.state} - {health.reason}", width)
        )
    for note in _section(document.notes):
        lines.append(_bound(f"note: {note}", width))
    for item in document.files:
        lines.extend(_file_lines(item, width, item.path in want))
    for item in _section(document.unevidenced):
        lines.append(
            _bound(
                f"no run evidence for {item!r}: it differs from the reference, but "
                "nothing in this run's own record says it wrote it",
                width,
            )
        )
    for item in _section(document.disagreement):
        lines.append(_bound(f"measurement disagreement: {item}", width))
    return lines


def _section(entries: Iterable[Any]) -> List[Any]:
    """Apply the anti-clutter rule through the product's own authority.

    Delegates to :func:`cli.toggles.section_rows` so the threshold is read
    from one place, and returns plain rows for the renderers to bound.
    """
    return [item for item in section_rows(entries)]


def _file_lines(record: FileReview, width: int, expanded: bool) -> List[str]:
    """One collapsed row per file, plus its diff when it was asked for."""
    sign = {
        "unchanged": "=",
        "changed": "~",
        "unrestorable": "!",
        "unmeasured": "?",
    }.get(record.state, "?")
    verdict = record.verdict
    meta = (
        f"{record.kind} +{record.additions} -{record.deletions} "
        f"{record.actor} {verdict}"
    )
    if record.hunk_decisions:
        # The hunk tally rides IMMEDIATELY after the verdict it qualifies,
        # and before the corroborating `via`/tools tail, for a measured
        # reason: at 80 columns the tail is what gets cut, so a tally
        # appended at the end of the row was truncated to "hu..." on a
        # terminal this product is built for. The undecided count is part
        # of the fact - a row reading "4 accepted" when 4 of 8 hunks were
        # never looked at is a fabricated measurement.
        meta += f" hunks {record.hunk_verdict_summary()}"
    if record.verified:
        meta += " run-verified"
    if record.tools:
        meta += f" via {','.join(record.tools[:3])}"
    if record.truncated or record.binary:
        meta += " bounded-view"
    rows = [_bound(f"{sign} {record.path} - {meta}", width)]
    if record.reason:
        rows.append(_bound(f"    why: {record.reason}", width))
    if record.revert_reason:
        rows.append(_bound(f"    revert: {record.revert_reason}", width))
    if not expanded:
        # COLLAPSED BY DEFAULT. A 200-file change set rendered in full is a
        # wall nobody reads, and the collapsed row already carries the four
        # facts a reader scans for: what, how much, who, and which verdict.
        return rows
    for number, header in enumerate(record.hunk_headers, start=1):
        word = record.hunk_verdict(number)
        # The verdict is on the header row, not a separate row, so per-hunk
        # granularity costs NO extra vertical space - which is the whole
        # reason the interaction is affordable on a 200-file change set.
        rows.append(_bound(f"    hunk {number} [{word}] {header}", width))
    for line in record.diff_lines:
        rows.append(_bound(f"    {line}", width))
    return rows


def review_text_lines(
    document: ReviewDocument, *, expanded: Iterable[str] = ()
) -> List[Any]:
    """The same lines as ``rich.text.Text`` for syntax-highlighted surfaces.

    The highlighting path cannot inject markup either, and for a stronger
    reason than escaping: ``rich.text.Text`` is never passed through a markup
    parser, so there is no delimiter for untrusted content to close. This
    routes through :func:`cli.ui.diff_render_lines`, which is the ONE
    language-aware diff renderer in the product, so a highlighted review and
    the REPL's ``/diff`` cannot colour the same line two ways.

    Returns ``[]`` when rich is unavailable rather than degrading to a
    different KIND of output, because a caller that asked for highlighting
    and silently received plain strings would render it wrongly.
    """
    plain = review_lines(document, expanded=expanded)
    try:
        from cli import ui
    except Exception:
        return []
    try:
        return list(ui.diff_render_lines(plain))
    except Exception:
        return []


def render_review(
    document: ReviewDocument,
    *,
    width: Any = 80,
    expanded: Iterable[str] = (),
    diff_style: Any = "",
) -> ReviewDocument:
    """Return a copy of ``document`` re-resolved for a render width.

    Layout is a property of the DOCUMENT rather than of the call site, so a
    surface that re-renders at a new width cannot accidentally keep the
    previous width's decisions. Nothing else about the document changes.
    """
    columns = _bounded_int(width, 80, 0)
    style, reason = resolve_diff_style(str(diff_style or document.diff_style), columns)
    return ReviewDocument(
        task_id=document.task_id,
        repo=document.repo,
        status=document.status,
        verdict=document.verdict,
        verified=document.verified,
        verification_state=document.verification_state,
        reviewable=document.reviewable,
        reviewable_reason=document.reviewable_reason,
        diff_style=document.diff_style,
        resolved_style=style,
        style_reason=reason,
        width=columns,
        tree=document.tree,
        tree_note=document.tree_note,
        files=document.files,
        snapshot=document.snapshot,
        decisions=document.decisions,
        hunk_decisions={
            str(path): {int(index): str(word) for index, word in dict(per).items()}
            for path, per in document.hunk_decisions.items()
        },
        additions=document.additions,
        deletions=document.deletions,
        unevidenced=document.unevidenced,
        disagreement=document.disagreement,
        notes=document.notes,
    )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def read_review_receipt(task_dir: Any) -> Dict[str, Any]:
    """Read this module's own additive receipt, tolerating any damage.

    A corrupt or absent receipt yields an empty decision set, never an
    exception: the receipt is bookkeeping, and losing it must not make a
    user's changes unreviewable.
    """
    directory = _task_dir(task_dir)
    if directory is None:
        return {}
    try:
        value = json.loads(
            (directory / REVIEW_RECEIPT_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    decisions = value.get("decisions")
    out = {
        str(key): str(item)
        for key, item in (decisions or {}).items()
        if str(item) in REVIEW_DECISIONS
    }
    # Per-hunk decisions are read through the SAME closed decision vocabulary.
    # A receipt carrying a word this module does not define is dropped rather
    # than rendered: an unknown verdict is a receipt nobody can act on, and
    # rendering it as "pending" would quietly turn a corruption into a fact.
    raw_hunks = value.get("hunk_decisions")
    hunk_out: Dict[str, Dict[int, str]] = {}
    if isinstance(raw_hunks, Mapping):
        for path, per in raw_hunks.items():
            if not isinstance(per, Mapping):
                continue
            words: Dict[int, str] = {}
            for index, word in per.items():
                text = str(word)
                if text not in REVIEW_DECISIONS:
                    continue
                try:
                    number = int(index)
                except (TypeError, ValueError):
                    continue
                if number >= 1:
                    words[number] = text
            if words:
                hunk_out[str(path)] = words
    return {
        "schema_version": _bounded_int(value.get("schema_version"), 0, 0),
        "task_id": str(value.get("task_id") or ""),
        "decisions": out,
        "hunk_decisions": hunk_out,
        "reverts": list(value.get("reverts") or []),
        "updated_at": float(value.get("updated_at") or 0.0),
    }


def decisions(task_dir: Any) -> Dict[str, str]:
    """The recorded per-path decisions, as a plain mapping."""
    return dict(read_review_receipt(task_dir).get("decisions") or {})


def hunk_decisions(task_dir: Any) -> Dict[str, Dict[int, str]]:
    """The recorded per-hunk decisions, as ``{path: {index: word}}``."""
    return {
        str(path): {int(index): str(word) for index, word in dict(per).items()}
        for path, per in (
            read_review_receipt(task_dir).get("hunk_decisions") or {}
        ).items()
    }


def _write_review_receipt(task_dir: Any, payload: Mapping[str, Any]) -> bool:
    directory = _task_dir(task_dir)
    if directory is None:
        return False
    return _fv.atomic_write_bytes(
        directory / REVIEW_RECEIPT_NAME,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        ),
    )


def record_decision(
    task_dir: Any,
    paths: Sequence[Any],
    decision: str,
    *,
    note: str = "",
    hunks: Optional[Mapping[str, Sequence[Any]]] = None,
) -> Dict[str, Any]:
    """Record what a person decided about specific files OR hunks.

    ``accept`` and ``reject`` are DECISIONS, not operations. Keeping them
    apart from :func:`revert_paths` is deliberate: a rejection that silently
    restored files would destroy the review-then-undo sequence a person
    actually performs, and a decision nobody recorded is a decision the next
    session cannot honour.

    ``hunks`` is ``{path: [1, 2]}``. When it is supplied the decision is
    recorded PER HUNK and the file's own ``decisions`` row is left exactly
    as it was - a file verdict and a hunk verdict are different questions,
    and writing "accepted" against a file because hunk 2 was accepted is how
    a review ends up claiming five hunks were ruled on when one was.

    Returns ``{ok, status, decision, paths, recorded, reason}``. ``status``
    is ``recorded`` / ``unknown_decision`` / ``unavailable`` / ``failed``,
    and ``ok`` is true only for ``recorded`` - a refusal is never a quiet
    success, because a decision that was not written down is a decision the
    product will re-ask about.
    """
    asked = str(decision or "").strip().lower()
    if asked not in REVIEW_DECISIONS:
        return {
            "ok": False,
            "status": "unknown_decision",
            "decision": asked,
            "paths": [],
            "recorded": False,
            "reason": f"unknown decision {decision!r}; expected one of "
            + ", ".join(REVIEW_DECISIONS),
        }
    if asked == "pending":
        return {
            "ok": False,
            "status": "unknown_decision",
            "decision": asked,
            "paths": [],
            "recorded": False,
            "reason": "'pending' clears nothing; say accept or reject to clear a decision",
        }
    normalised: List[str] = []
    for value in paths or ():
        path = _row_relative(
            value
            if not isinstance(value, Mapping)
            else value.get("path") or value.get("file")
        )
        if path and path not in normalised:
            normalised.append(path)
    if not normalised and not hunks:
        return {
            "ok": False,
            "status": "unavailable",
            "decision": asked,
            "paths": [],
            "recorded": False,
            "reason": "no safe repository-relative path was named",
        }
    current = read_review_receipt(task_dir)
    merged = dict(current.get("decisions") or {})
    merged_hunks: Dict[str, Dict[str, str]] = {
        str(path): {str(index): str(word) for index, word in dict(per).items()}
        for path, per in (current.get("hunk_decisions") or {}).items()
    }
    decided: Dict[str, List[int]] = {}
    if hunks:
        for raw_path, indexes in dict(hunks).items():
            path = _row_relative(raw_path)
            if not path:
                continue
            wanted: List[int] = []
            for value in indexes or ():
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number >= 1 and number not in wanted:
                    wanted.append(number)
            if not wanted:
                continue
            decided[path] = wanted
            per = merged_hunks.setdefault(path, {})
            for number in wanted:
                per[str(number)] = asked
            if path not in normalised:
                normalised.append(path)
    else:
        for path in normalised:
            merged[path] = asked
    payload = {
        "schema_version": REVIEW_RECEIPT_SCHEMA_VERSION,
        "task_id": str(current.get("task_id") or _task_name(task_dir)),
        "decisions": merged,
        "hunk_decisions": merged_hunks,
        "reverts": list(current.get("reverts") or []),
        "updated_at": time.time(),
    }
    written = _write_review_receipt(task_dir, payload)
    return {
        "ok": bool(written),
        "status": "recorded" if written else "failed",
        "decision": asked,
        "paths": normalised,
        "recorded": bool(written),
        "hunks": {path: list(indexes) for path, indexes in sorted(decided.items())},
        "granularity": "hunk" if decided else "file",
        "note": str(note or ""),
        "reason": "" if written else "the review receipt could not be written",
        "decisions": merged,
        "hunk_decisions": merged_hunks,
    }


def _task_name(task_dir: Any) -> str:
    directory = _task_dir(task_dir)
    return directory.name if directory is not None else ""


def accept_paths(
    task_dir: Any, paths: Sequence[Any], *, note: str = ""
) -> Dict[str, Any]:
    """Record that a person KEEPS these changes. Never touches the tree."""
    return record_decision(task_dir, paths, "accepted", note=note)


def reject_paths(
    task_dir: Any,
    repo: Any,
    paths: Sequence[Any],
    *,
    force: bool = False,
    in_flight: bool = False,
    note: str = "",
) -> Dict[str, Any]:
    """Record a rejection AND apply the restore for the same paths.

    The decision and the action ship together because "reject this change"
    with the change still on disk is a state a person cannot act on. The
    restore is :func:`revert_paths` unmodified, so a rejection is audited
    by exactly the same hash-pinned receipt a bare revert is.

    ``repo`` is forwarded, not dropped: the restore resolves which tree to
    act on from it, so a rejection that lost it would fall back to the
    process working directory and restore into the wrong tree.
    """
    verdict = record_decision(task_dir, paths, "rejected", note=note)
    receipt = revert_paths(task_dir, repo, paths, force=force, in_flight=in_flight)
    return {"decision": verdict, "revert": receipt}


def accept_hunks(
    task_dir: Any,
    hunks: Mapping[str, Sequence[Any]],
    *,
    note: str = "",
) -> Dict[str, Any]:
    """Record that a person KEEPS these HUNKS. Never touches the tree.

    A partial accept changes NO bytes, so it cannot move the live content
    hash. That is the whole reason the concurrent-edit rule in
    :func:`_concurrent_edit` keeps holding under hunk granularity: the gate
    compares the live hash against the run's accounted set, and an accept
    adds nothing to the tree and therefore nothing to that set. The set
    stays exactly what the run's own journal accounted for, so a later
    whole-file revert is still recognised as this run's change and is not
    refused as a third-party edit.
    """
    return record_decision(task_dir, [], "accepted", note=note, hunks=dict(hunks or {}))


def reject_hunks(
    task_dir: Any,
    repo: Any,
    hunks: Mapping[str, Sequence[Any]],
    *,
    force: bool = False,
    in_flight: bool = False,
    note: str = "",
) -> Dict[str, Any]:
    """Record a per-hunk rejection AND apply the restore it can apply.

    **The restore is per FILE, and the receipt says so.** Reverting one hunk
    of a unified diff means reconstructing a file from a patch, and this
    module's restore is deliberately hash-pinned to whole bytes: it restores
    a file to a recorded pre-image and verifies the result by re-hashing.
    Inventing a hunk-level writer here would bypass every one of those
    checks, so a partial rejection is recorded and REPORTED as not applied
    rather than half-applied.

    So: a rejection whose named hunks cover EVERY hunk of their file
    reverts that file through the ordinary hash-verified path, and a partial
    one reverts nothing and says which hunks it left. The decision is
    recorded either way, because a decision nobody recorded is a decision
    the next session re-asks about.

    ``applied`` in the returned mapping is the honest answer, and it is
    ``False`` with a reason rather than absent when a partial rejection
    could not be applied.
    """
    wanted = {str(path): list(per) for path, per in dict(hunks or {}).items()}
    if not wanted:
        return {
            "decision": {
                "ok": False,
                "status": "unavailable",
                "reason": "no hunk was named",
            },
            "revert": None,
            "applied": False,
            "applied_to": "nothing",
            "reason": "no hunk was named",
        }
    document = build_review(task_dir, repo, max_files=1000, include_diff_lines=False)
    by_path = {item.path: item for item in document.files}
    covered: List[str] = []
    partial: List[str] = []
    missing: List[str] = []
    for path, indexes in sorted(wanted.items()):
        record = by_path.get(path)
        if record is None:
            missing.append(path)
            continue
        total = len(record.hunk_headers)
        if total and all(
            number in {int(i) for i in indexes} for number in range(1, total + 1)
        ):
            covered.append(path)
        else:
            partial.append(path)
    verdict = record_decision(task_dir, [], "rejected", note=note, hunks=wanted)
    receipt = None
    if covered:
        receipt = revert_paths(
            task_dir, repo, covered, force=force, in_flight=in_flight
        )
    reasons: List[str] = []
    if partial:
        reasons.append(
            f"{len(partial)} file(s) rejected only in part and were NOT rewritten: "
            "a partial hunk revert is not applied, so the bytes are still on disk"
        )
    if missing:
        reasons.append(
            f"{len(missing)} named path(s) are not in this run's change set and were ignored"
        )
    return {
        "decision": verdict,
        "revert": receipt,
        "applied": bool(receipt is not None and receipt.ok),
        "applied_to": "file" if covered else "nothing",
        "covered": list(covered),
        "partial": list(partial),
        "missing": list(missing),
        "reason": "; ".join(reasons),
    }


# ---------------------------------------------------------------------------
# The revert
# ---------------------------------------------------------------------------


def _concurrent_edit(
    record: FileReview, current_digest: str
) -> Optional[Dict[str, Any]]:
    """Describe a live file whose content this run is NOT accountable for.

    The rule, stated once so the two call sites cannot differ:

    A live content hash is SAFE to overwrite when it is the pre-image (the
    file is already back where it started), or when it appears among the
    hashes the run's own journal receipts, its work-tree copy, or its
    pre-image account for. Anything else was written by something whose
    receipt is not in this run, and the honest response is to refuse.

    Returning the detail rather than deciding is the point: whether that
    detail becomes a REFUSAL or a RECORDED OVERWRITE is the caller's
    ``force`` choice, and a ``force`` that downgraded the refusal to a
    record has to keep going and write. A function that returned ``None``
    for both "safe" and "forced through" made the force path
    indistinguishable from the clean one - which is the same defect class as
    a forced undo that forgets to name what it destroyed.
    """
    if not current_digest:
        return None
    if current_digest == record.pre_image_hash:
        return None
    if current_digest in record.accounted_hashes:
        return None
    return {
        "path": record.path,
        "reason": "concurrent_user_edit",
        "actual_hash": current_digest,
        "expected_hashes": list(record.accounted_hashes),
        "detail": (
            "the file on disk hashes to content this run never recorded, so it was "
            "changed by somebody else after the capture; a revert here would "
            "destroy that edit, so it is refused"
        ),
    }


def revert_paths(
    task_dir: Any,
    repo: Any,
    paths: Sequence[Any],
    *,
    force: bool = False,
    in_flight: bool = False,
    repo_hint: Any = None,
) -> Any:
    """Restore the named paths to the run's pre-image, hash-pinned both ways.

    ``repo`` is the tree to restore INTO. ``task_dir`` is the run whose
    reference decides what "restored" means. A run that wrote its own
    ``work/`` copy is restored there and the live repository is not touched
    at all - the receipt says which tree it acted on, because "your working
    tree was not modified" is a different fact from "your working tree is
    back to where it was".

    Hash-pinned in BOTH directions, which is the property that makes the
    receipt worth reading:

    * BEFORE the write, the live bytes are hashed. That hash is the
      ``before_hash``, and it is what the concurrent-edit rule is judged
      against.
    * the pre-image bytes are read from the reference and hashed into
      ``pristine_hash``.
    * AFTER the write, the file is RE-READ and hashed. A path counts as
      restored only when ``after_hash == pristine_hash``.

    A path that could not be restored is named under ``not_restored`` with
    its reason. The receipt therefore answers both halves of the question -
    what came back and what did not - because a receipt that lists only
    successes is indistinguishable from one that did nothing.

    Never deletes a file it cannot restore: a deletion happens only when
    the reference proves the file did not exist before the run, and the
    live bytes are the run's own recorded content.
    """
    started = time.time()
    directory = _task_dir(task_dir)
    if directory is None:
        return RevertReceipt(
            status="unavailable",
            reason="no run directory is readable for this task id",
            started_at=started,
        )
    if bool(in_flight):
        return RevertReceipt(
            ok=False,
            status="refused",
            task_id=directory.name,
            reason="run_in_flight",
            started_at=started,
        )

    health = snapshot_health(task_dir, repo)
    tree_name, tree, tree_note = _tree_choice(directory, _fv.repo_root(repo))
    if tree is None:
        return RevertReceipt(
            status="unavailable",
            task_id=directory.name,
            reason=tree_note,
            snapshot_state=health.state,
            started_at=started,
        )

    document = build_review(
        task_dir,
        repo,
        config=None,
        width=80,
        max_files=1000,
        max_diff_lines=1,
        include_diff_lines=False,
    )
    by_path = {item.path: item for item in document.files}

    requested: List[str] = []
    for value in paths or ():
        path = _row_relative(
            value
            if not isinstance(value, Mapping)
            else value.get("path") or value.get("file")
        )
        if path and path not in requested:
            requested.append(path)
    if not requested:
        return RevertReceipt(
            status="nothing",
            task_id=directory.name,
            reason="no safe repository-relative path was named",
            snapshot_state=health.state,
            started_at=started,
        )

    restored: List[Dict[str, Any]] = []
    unchanged: List[str] = []
    not_restored: List[Dict[str, Any]] = []
    deleted: List[str] = []
    overwritten: List[Dict[str, Any]] = []
    checked = 0

    for path in requested:
        record = by_path.get(path)
        if record is None:
            # Never measured: the file is not in the change set at all. A
            # restore of an unmeasured path would be a restore from a
            # guess, and the honest answer names that.
            not_restored.append(
                {
                    "path": path,
                    "reason": "not_measured",
                    "detail": (
                        "this path is not in the run's measured change set, so there "
                        "is no evidence it was touched and no reason to revert it"
                    ),
                }
            )
            continue
        if record.state == "unmeasured":
            not_restored.append(
                {
                    "path": path,
                    "reason": "unmeasured",
                    "detail": record.revert_reason or "one side could not be hashed",
                }
            )
            continue
        if record.state == "unrestorable":
            not_restored.append(
                {
                    "path": path,
                    "reason": "corrupt_snapshot"
                    if health.state == "corrupt"
                    else "no_pre_image",
                    "detail": record.revert_reason,
                }
            )
            continue
        if record.state == "unchanged":
            unchanged.append(path)
            continue

        target = _fv.safe_repo_path(tree, path)
        if target is None:
            not_restored.append(
                {
                    "path": path,
                    "reason": "unsafe_path",
                    "detail": "the path escapes the tree, is absolute, or has a symlinked component",
                }
            )
            continue
        before_digest, _before_exists = _live_hashes(tree, path)
        checked += 1

        # The ONE concurrent-edit gate. Unaccounted content is refused, and
        # `force` converts that refusal into a RECORDED OVERWRITE that still
        # writes - never into silence, and never into a path that skips the
        # restore and reports itself as already handled. The record is only
        # folded into the receipt AFTER the write verifies, because an
        # overwrite that never happened must not be listed as one.
        forced_here = _concurrent_edit(record, before_digest)
        if forced_here is not None:
            if not force:
                not_restored.append(forced_here)
                continue
            forced_here["reason"] = "concurrent_user_edit_forced"
            forced_here["detail"] = (
                "forced over a concurrent edit: the content that was on disk hashes to "
                f"{str(before_digest)[:12]} and is recorded here so the overwrite is not silent"
            )

        reference = directory / "pristine"
        source = _fv.safe_repo_path(reference, path)
        if source is None or not _fv.path_exists(source):
            not_restored.append(
                {
                    "path": path,
                    "reason": "no_pre_image",
                    "detail": "the reference holds no readable pre-image for this path",
                }
            )
            continue
        payload = _read_file_bytes(source)
        if payload is None:
            not_restored.append(
                {
                    "path": path,
                    "reason": "unreadable_pre_image",
                    "detail": "the reference copy exists but could not be read as bytes",
                }
            )
            continue
        pristine_digest = hashlib.sha256(payload).hexdigest()
        if not _fv.atomic_write_bytes(target, payload):
            not_restored.append(
                {
                    "path": path,
                    "reason": "write_failed",
                    "detail": "the atomic write did not land; the file is unchanged",
                }
            )
            continue
        after_digest, after_exists = _live_hashes(tree, path)
        verified = bool(after_exists and after_digest == pristine_digest)
        if not verified:
            not_restored.append(
                {
                    "path": path,
                    "reason": "restore_unverified",
                    "before_hash": before_digest,
                    "pristine_hash": pristine_digest,
                    "after_hash": after_digest,
                    "detail": (
                        "the file was written but does not hash to the pre-image; the "
                        "restore is reported as NOT verified rather than as a success"
                    ),
                }
            )
            continue
        if forced_here is not None:
            forced_here["overwritten_hash"] = before_digest
            forced_here["restored_to_hash"] = pristine_digest
            overwritten.append(forced_here)
        restored.append(
            {
                "path": path,
                "action": "restored",
                "before_hash": before_digest,
                "pristine_hash": pristine_digest,
                "after_hash": after_digest,
                "verified": True,
            }
        )

    checked_paths = len(restored) + len(unchanged)
    # `ok` and `verified` are SEPARATE facts and both are fail-closed. A
    # revert that restored something but could not restore everything is
    # `partial`: it is not a failure the user should retry (their tree moved
    # back), and it is not a success they can rely on. Collapsing the two
    # words into one flag is how a half-revert becomes a clean receipt.
    verified = (
        bool(restored)
        and not not_restored
        and all(bool(item.get("verified")) for item in restored)
    )
    ok = bool(checked_paths) and not not_restored
    if not checked_paths:
        status = "refused"
    elif not_restored:
        status = "partial"
    else:
        status = "reverted"
    receipt = RevertReceipt(
        ok=ok,
        status=status,
        task_id=directory.name,
        tree=tree_name,
        paths=tuple(requested),
        restored=tuple(restored),
        unchanged=tuple(unchanged),
        not_restored=tuple(not_restored),
        deleted=tuple(deleted),
        overwritten_user_edits=tuple(overwritten),
        forced=bool(force),
        verified=verified,
        verified_paths=len(restored),
        checked_paths=checked_paths,
        snapshot_state=health.state,
        reason=""
        if not not_restored
        else f"{len(not_restored)} path(s) were not restored",
        started_at=started,
        duration_ms=round((time.time() - started) * 1000.0, 3),
    )
    stored = _append_revert(task_dir, receipt)
    # `dataclasses.replace` rather than re-constructing from a dict: the
    # serialized form carries keys (`schema_version`, `overwritten_count`)
    # that are not FIELDS, so a `**payload` round-trip is a TypeError waiting
    # to happen the first time someone adds a serialization-only key.
    return replace(
        receipt,
        written=stored,
        receipt_path=_receipt_path(task_dir),
    )


def _read_file_bytes(path: Optional[Path]) -> Optional[bytes]:
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except (OSError, ValueError):
        return None


def _receipt_path(task_dir: Any) -> str:
    directory = _task_dir(task_dir)
    return str(directory / REVIEW_RECEIPT_NAME) if directory is not None else ""


def _append_revert(task_dir: Any, receipt: RevertReceipt) -> bool:
    """Fold this revert into the additive receipt. Bookkeeping, best effort.

    A failure to append is NOT a failure of the revert: the tree is already
    restored and hash-verified, and a receipt that could not be written must
    not be reported as a failed restore (which would make a user re-run a
    correct operation) nor as a success that hides a lost audit trail. The
    receipt itself carries ``written`` so a reader can tell.
    """
    current = read_review_receipt(task_dir)
    history = list(current.get("reverts") or [])
    history.append(receipt.to_dict())
    # Bounded: a session with hundreds of reverts keeps the newest 200, and
    # the bound is a REPORTED truncation rather than a silent one.
    truncated = len(history) > 200
    payload = {
        "schema_version": REVIEW_RECEIPT_SCHEMA_VERSION,
        "task_id": receipt.task_id or str(current.get("task_id") or ""),
        "decisions": dict(current.get("decisions") or {}),
        # A revert must not ERASE the hunk decisions. This function rewrites
        # the whole receipt, and dropping a key it does not know about is how
        # "reject hunk 2, then revert" silently un-records the rejection -
        # the same read-modify-write data loss the session store's CAS
        # revision exists to prevent, in a smaller file.
        "hunk_decisions": {
            str(path): {str(index): str(word) for index, word in dict(per).items()}
            for path, per in (current.get("hunk_decisions") or {}).items()
        },
        "reverts": history[-200:],
        "reverts_truncated": max(0, len(history) - 200) if truncated else 0,
        "updated_at": time.time(),
    }
    return _write_review_receipt(task_dir, payload)


# ---------------------------------------------------------------------------
# The live changed-files indicator
# ---------------------------------------------------------------------------


def changed_files_indicator(
    task_dir: Any,
    repo: Any = None,
    *,
    previous: Optional[Mapping[str, Any]] = None,
    config: Optional[Mapping[str, Any]] = None,
    max_files: Any = 200,
) -> Dict[str, Any]:
    """The changed-file indicator a live surface can poll every tick.

    Answers "what has the run touched so far" from the run's OWN records,
    and can be sampled repeatedly: pass the previous result back as
    ``previous`` and the answer carries ``new_paths`` (appeared since the
    last sample) and ``resolved_paths`` (were changed, now are not). That
    is what makes SCOPE CREEP visible in real time rather than discovered at
    the end of a run.

    ``unplanned`` is the scope-creep signal proper: a path this run is
    EVIDENCED to have changed that the run's own plan never named through
    ``files_hint``. Two restrictions make it a fact rather than a suspicion.
    It is reported only when the run actually DECLARED a plan, because "the
    plan did not mention it" is a claim about a plan, and a run with no
    plan has made no claim. And it only counts paths the run's OWN record
    names - a file that merely differs from the reference on a shared
    working tree is reported as ``unevidenced``, which is a different
    sentence: somebody changed this, and the run did not say it did.

    Works mid-run: no terminal event is required, and a journal with a torn
    tail is the normal case rather than an error.

    ``max_files`` is a REPORTED bound, not a silent cap. This function is
    meant to be polled, and a live surface that polls a per-frame widget
    cannot afford an unbounded read of a 200-file change set - the measured
    cost of one is dominated by reading and diffing every file, so a bound
    is the difference between a number a frame budget can hold and one it
    cannot. ``truncated`` says when the answer is partial, because an
    indicator that quietly dropped paths would read as "the run is done".
    """
    limit = _bounded_int(max_files, 200, 1)
    document = build_review(
        task_dir,
        repo,
        config=config,
        width=80,
        max_files=limit,
        max_diff_lines=1,
        include_diff_lines=False,
        include_git=True,
        # A poller asks for a CHEAP SAMPLE, and the document says which
        # checks it skipped rather than reporting them as passing.
        thorough=False,
    )
    rows = _journal_rows(_task_dir(task_dir))
    evidence = tool_evidence(rows)
    changed = [item.path for item in document.files if item.state != "unchanged"]
    run_changed = [
        item.path
        for item in document.files
        if item.state != "unchanged" and item.run_evidenced
    ]
    planned = set(evidence["planned"])
    unplanned = (
        tuple(sorted(path for path in run_changed if path not in planned))
        if planned
        else ()
    )
    before = previous if isinstance(previous, Mapping) else {}
    seen = {str(item) for item in (before.get("changed") or ())}
    new_paths = tuple(path for path in changed if path not in seen)
    resolved = tuple(sorted(seen - set(changed)))
    written = set(evidence["written"])
    read_only = set(evidence["read"]) - written
    return {
        "available": bool(document.task_id or changed),
        "task_id": document.task_id,
        "status": document.status,
        "verdict": document.verdict,
        "verified": bool(document.verified),
        "tree": document.tree,
        "changed": tuple(changed),
        "run_changed": tuple(run_changed),
        "counts": {
            "changed": len(changed),
            "new": len(new_paths),
            "resolved": len(resolved),
            "planned": len(planned),
            "unplanned": len(unplanned),
            "unevidenced": len(document.unevidenced),
        },
        "max_files": limit,
        "truncated": len(document.files) >= limit,
        "planned": tuple(sorted(planned)),
        "unplanned": unplanned,
        "unevidenced": document.unevidenced,
        "new_paths": new_paths,
        "resolved_paths": resolved,
        "written": tuple(sorted(written)),
        "read_only": tuple(sorted(read_only)),
        "scope_creep": bool(unplanned),
        "snapshot_state": document.snapshot.state,
        "read_at": time.time(),
    }


# ---------------------------------------------------------------------------
# The blast radius
# ---------------------------------------------------------------------------


def _command_reaches_outside(command: str, root: Optional[Path]) -> Tuple[bool, str]:
    """Whether a RECORDED command's text names a path outside the repository.

    A LOWER BOUND, and the return value says so: the text is examined for
    absolute and home-relative path tokens and for a ``cd`` to something
    that is not repository-relative. A command can reach anywhere without
    naming a path, and this function does not claim otherwise. What it
    refuses to do is guess a working directory and then resolve a relative
    token against it, because that would INVENT findings - and an invented
    "this ran outside your repository" is the kind of receipt nobody can act
    on.
    """
    text = str(command or "").strip()
    if not text:
        return (False, "")
    for match in _OUTSIDE_PATH.finditer(text):
        token = (
            text[match.start() :].split()[0] if text[match.start() :].split() else ""
        )
        if not token:
            continue
        if _path_is_under(token, root):
            continue
        return (True, f"names {token!r}, which is outside the repository root")
    match = _CD_COMMAND.search(text)
    if match:
        target = str(match.group("target") or "").strip("\"'")
        if target and not _path_is_under(target, root):
            return (
                True,
                f"changes directory to {target!r}, which is outside the repository",
            )
    return (False, "")


def _path_is_under(token: str, root: Optional[Path]) -> bool:
    """Whether a command token names something inside the repository root.

    Three cases, and the middle one is the trap this function exists to
    avoid:

    * a RELATIVE token is assumed to be repository-relative, because the
      recorded command does not carry a working directory and guessing one
      would invent findings;
    * a token with a LEADING SEPARATOR is asked about directly. It is
      absolute on POSIX and drive-relative on Windows, and on Windows
      ``Path("/etc/hosts").is_absolute()`` is ``False`` - so a naive
      relative/absolute branch calls it repository-relative and the check
      reports every absolute path as harmless;
    * a drive-qualified Windows path is resolved and compared.
    """
    if root is None:
        return False
    text = str(token or "").strip("\"'`(),;")
    if not text:
        return False
    if text[0] not in "/\\":
        return True
    try:
        expanded = os.path.expanduser(text)
        absolute = Path(os.path.abspath(expanded))
        absolute.resolve().relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def blast_radius(
    task_dir: Any,
    repo: Any = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
    max_commands: Any = 40,
) -> Dict[str, Any]:
    """State the blast radius: what was read, what was written, what reached out.

    Three axes, and they are three questions:

    * ``files_read``     - paths a read-shaped tool named in the journal.
    * ``files_written``  - paths a write-shaped tool named in the journal.
    * ``commands``       - the recorded command strings, each with whether
      its TEXT names a path outside the repository.

    Every value is a fold of the run's own rows, and ``sources`` names what
    was folded so a reader can weigh it. ``detection`` on the command rows
    says ``recorded_command_text`` rather than implying a syscall trace: the
    journal records the command, not every path it opened.
    """
    directory = _task_dir(task_dir)
    root = _fv.repo_root(repo) if repo is not None else _fv.repo_root(os.getcwd())
    rows = _journal_rows(directory)
    evidence = tool_evidence(rows)
    limit = max(1, _bounded_int(max_commands, 40, 1))
    commands: List[CommandTools] = []
    outside: List[CommandTools] = []
    for text in evidence["commands"]:
        reached, reason = _command_reaches_outside(text, root)
        row = CommandTools(
            command=_bound(text, 200),
            tool="bash",
            outside=reached,
            reason=reason,
        )
        commands.append(row)
        if reached:
            outside.append(row)
    shown = commands[:limit]
    return {
        "available": directory is not None,
        "task_id": directory.name if directory is not None else "",
        "repo": str(root) if root is not None else "",
        "files_read": tuple(evidence["read"]),
        "files_written": tuple(evidence["written"]),
        "read_only": tuple(sorted(set(evidence["read"]) - set(evidence["written"]))),
        "commands": tuple(item.to_dict() for item in shown),
        "commands_outside_repository": tuple(
            item.to_dict() for item in outside[:limit]
        ),
        "counts": {
            "read": len(evidence["read"]),
            "written": len(evidence["written"]),
            "commands": len(commands),
            "commands_outside_repository": len(outside),
            "mutations": int(evidence["mutations"]),
        },
        "truncated": len(commands) > limit,
        "detection": "recorded_command_text",
        "sources": ["trace.jsonl"],
        "reason": (
            "commands are judged by their recorded text; a command can reach "
            "outside the repository without naming a path, so this is a lower bound"
        ),
    }


# ---------------------------------------------------------------------------
# Revert receipt rendering
# ---------------------------------------------------------------------------


def render_receipt(receipt: Any) -> List[str]:
    """Render a revert receipt as PLAIN, unstyled lines.

    The receipt's job is to answer BOTH halves - what came back and what did
    not - so the refusal section is never dropped for being short, and a
    forced revert always names what it overwrote. A revert receipt that
    reads like a clean one after a forced overwrite is the same defect class
    as a gate that cannot fail.
    """
    if not isinstance(receipt, RevertReceipt):
        return ["revert failed: no receipt"]
    lines: List[str] = []
    if receipt.status == "unavailable":
        return [f"revert unavailable: {receipt.reason or 'unknown reason'}"]
    if receipt.status == "nothing":
        return [f"nothing to revert: {receipt.reason or 'no path was named'}"]
    if receipt.status == "refused" and not receipt.checked_paths:
        return [
            f"revert refused: {receipt.reason or 'unknown reason'}",
            "nothing was written",
        ]
    tree_word = {
        "work": "the run's own work copy (your working tree was not modified)",
        "live": "your working tree",
    }.get(receipt.tree, "the workspace")
    lines.append(
        f"reverted {len(receipt.restored)} path(s), "
        f"{len(receipt.unchanged)} already at the pre-image, "
        f"{len(receipt.not_restored)} NOT restored"
    )
    for item in receipt.restored[:12]:
        lines.append(
            f"  restored {item.get('path')!s} "
            f"(sha256 {str(item.get('after_hash'))[:12]})"
        )
    if len(receipt.restored) > 12:
        lines.append(f"  ... and {len(receipt.restored) - 12} more restored path(s)")
    for item in receipt.unchanged[:8]:
        lines.append(f"  unchanged {item!s} (already back where it started)")
    for item in receipt.not_restored[:12]:
        lines.append(
            f"  NOT restored {item.get('path')!s}: {item.get('reason')!s} - "
            f"{item.get('detail') or ''}"
        )
    if len(receipt.not_restored) > 12:
        lines.append(
            f"  ... and {len(receipt.not_restored) - 12} more path(s) not restored"
        )
    if receipt.overwritten_user_edits:
        lines.append(
            "  forced: "
            f"{len(receipt.overwritten_user_edits)} concurrent user edit(s) were "
            "overwritten on purpose: "
            + ", ".join(
                str(item.get("path") or "")
                for item in receipt.overwritten_user_edits
                if isinstance(item, Mapping)
            )
        )
    if receipt.deleted:
        lines.append(
            f"  deleted {len(receipt.deleted)} file(s) the run created: "
            + ", ".join(receipt.deleted[:8])
        )
    lines.append(
        "  verified: "
        + (
            "yes - every restored path re-hashed to its recorded pre-image"
            if receipt.verified
            else "NO - see the paths that were not restored; nothing here claims otherwise"
        )
    )
    if receipt.snapshot_state not in {"ok", ""}:
        lines.append(
            f"  pristine reference: {receipt.snapshot_state} - the restore was "
            "limited to the paths whose pre-image is still trustworthy"
        )
    lines.append(f"  wrote to: {tree_word}")
    if receipt.receipt_path:
        lines.append(f"  receipt: {receipt.receipt_path}")
    if not receipt.written:
        lines.append(
            "  note: the revert is hash-verified but its receipt could not be "
            "written to disk, so there is no audit trail for it"
        )
    return lines


# ---------------------------------------------------------------------------
# The one dispatcher
# ---------------------------------------------------------------------------

#: The verbs this module owns, and what each one MEANS. Declared as data so
#: a registry, a help line and a renderer cannot disagree about the
#: vocabulary, and so a new verb is an explicit edit here.
#:
#: ``all`` is deliberately NOT a verb. It is an ARGUMENT
#: (``/diff revert all``), and a bare ``/diff all`` already means "revert
#: everything" in the historical engine - taking that word for a read-only
#: roster would silently turn a destructive command into a display one,
#: which is the worst possible direction for a collision to go. ``show``
#: already lists every file, so the verb bought nothing.
DIFF_REVIEW_VERBS: Dict[str, str] = {
    "show": "render the diff with per-file verdicts and the evidence behind them",
    "accept": "record that you keep these changes; never touches your files",
    "reject": "record that you refuse these changes and put them back",
    "revert": "put these paths back to the run's pre-image, hash-verified",
}


def diff_review_verbs() -> Tuple[str, ...]:
    """The closed verb vocabulary ``/diff`` answers for the review surface."""
    return tuple(DIFF_REVIEW_VERBS)


def _verb_paths(verb: str, argument: str) -> Tuple[List[str], Dict[str, Any]]:
    """Resolve a typed argument to the paths AND hunks a verb acts on.

    A target is ``path``, ``path#3`` (hunk 3), ``path#2,4`` (hunks 2 and 4)
    or ``path#2-4`` (hunks 2 through 4). The ``#`` spelling is the one every
    other surface in the product already accepts for diff navigation
    (``/diff src/a.py#2``), so a person who can CURSOR to hunk 2 can RULE on
    it with the same string.

    The path is returned WITHOUT the hunk suffix, because the restore
    operates on whole files and a path carrying ``#2`` would fail
    ``safe_repo_path`` and be refused as unsafe - a hunk decision would look
    like a path-traversal refusal.
    """
    text = str(argument or "").strip().strip("\"'`")
    lowered = text.lower()
    if verb in {"accept", "reject", "revert"} and (not text or lowered == "all"):
        return ([], {"all": True})
    if not text:
        return ([], {})
    path, separator, spec = text.rpartition("#")
    if not separator:
        return ([text], {})
    indexes: List[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        span = part.split("-", 1)
        try:
            first = int(span[0])
        except ValueError:
            return ([text], {"hunks": {}, "unparsed": part})
        if len(span) == 2:
            try:
                last = int(span[1])
            except ValueError:
                return ([text], {"hunks": {}, "unparsed": part})
            if last < first:
                return ([text], {"hunks": {}, "unparsed": part})
            indexes.extend(range(first, last + 1))
        else:
            indexes.append(first)
    wanted = [number for number in indexes if number >= 1]
    if not path or not wanted:
        return ([text], {"hunks": {}, "unparsed": spec})
    return ([path], {"hunks": {path: wanted}})


def _hunk_phrase(hunks: Mapping[str, Sequence[Any]]) -> str:
    """``src/a.py#2,4`` - the targets, echoed back so a receipt names them.

    Echoing the request is what lets a person confirm the SHELL parsed what
    they typed: a receipt saying "rejected 1 hunk" when three were named is
    a receipt that hides a parse disagreement.
    """
    parts: List[str] = []
    for path, indexes in sorted(dict(hunks or {}).items()):
        wanted = [number for number in indexes or ()]
        if wanted:
            parts.append(f"{path}#{','.join(str(number) for number in wanted)}")
        else:
            parts.append(str(path))
    return ", ".join(parts) if parts else "(none)"


def review_command(
    task_dir: Any,
    repo: Any,
    argument: str = "",
    *,
    verb: str = "",
    in_flight: bool = False,
    force: bool = False,
    config: Optional[Mapping[str, Any]] = None,
    width: Any = 80,
    previous: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The ONE dispatcher both shells call for the review surface.

    Returns ``{handled, kind, ok, lines, payload}`` - the same shape
    :func:`cli.fileview.undo_command` returns, so a shell that already
    renders an undo receipt can render this one without learning a second
    protocol.

    ``handled=False`` means "not mine": a bare ``/diff undo`` and any path
    the historical per-file engine owns are handed straight back, which is
    what keeps the existing undo surface byte-identical. The check happens
    on the TYPED verb BEFORE it is normalised, because normalising first
    turned every unknown verb into ``show`` - and a ``/diff undo`` that
    rendered this module's review instead of the historical engine's undo
    would silently take a capability away from every run that predates it.

    ``lines`` are PLAIN. A repository path may contain ``[`` and a line
    carrying one would be EATEN by a markup parser rather than printed -
    the exact class of failure this module refuses to reintroduce.
    """
    raw = str(verb or "").strip().lower()
    if raw and raw not in DIFF_REVIEW_VERBS:
        return {
            "handled": False,
            "kind": "",
            "ok": False,
            "lines": [],
            "payload": {},
        }
    asked = raw or "show"

    if asked == "show":
        document = build_review(task_dir, repo, config=config, width=width)
        expanded = [document.path()] if str(argument or "").strip() else []
        rendered = render_review(document, width=width, expanded=expanded)
        return {
            "handled": True,
            "kind": "show",
            "ok": True,
            "lines": review_lines(rendered, expanded=expanded),
            "payload": rendered.as_dict(),
        }

    paths, flags = _verb_paths(asked, argument)
    hunks: Dict[str, List[int]] = dict(flags.get("hunks") or {})
    if flags.get("unparsed"):
        return {
            "handled": True,
            "kind": asked,
            "ok": False,
            "lines": [
                f"cannot read the hunk in {argument!r}: expected path#N, path#N,M or "
                "path#N-M (the same spelling /diff uses to open a hunk)"
            ],
            "payload": {"unparsed": str(flags["unparsed"])},
        }
    if flags.get("all"):
        document = build_review(
            task_dir, repo, config=config, width=width, max_files=1000
        )
        paths = [
            item.path
            for item in document.files
            if item.state in {"changed", "unrestorable"}
        ]
        if not paths:
            return {
                "handled": True,
                "kind": asked,
                "ok": False,
                "lines": [
                    "nothing to act on: this run changed no file it is evidenced to have touched"
                ],
                "payload": {"paths": []},
            }

    if asked == "accept":
        if hunks:
            result = accept_hunks(task_dir, hunks)
            lines = [
                f"accepted hunk(s) {_hunk_phrase(hunks)} (your files were not touched)"
            ]
            if not result.get("ok"):
                lines.append(
                    f"could not record the acceptance: {result.get('reason') or 'unknown'}"
                )
            return {
                "handled": True,
                "kind": "accept",
                "ok": bool(result.get("ok")),
                "lines": lines,
                "granularity": "hunk",
                "payload": result,
            }
        result = accept_paths(task_dir, paths)
        verb_text = "accepted (your files were not touched)"
        if not result.get("ok"):
            verb_text = (
                f"could not record the acceptance: {result.get('reason') or 'unknown'}"
            )
        lines = [f"{len(result.get('paths') or [])} path(s) {verb_text}"]
        for path in (result.get("paths") or [])[:12]:
            lines.append(f"  accepted {path}")
        return {
            "handled": True,
            "kind": "accept",
            "ok": bool(result.get("ok")),
            "lines": lines,
            "granularity": "file",
            "payload": result,
        }

    if asked == "reject":
        if hunks:
            result = reject_hunks(
                task_dir, repo, hunks, force=force, in_flight=in_flight
            )
            decision = result.get("decision") or {}
            lines = [f"rejected hunk(s) {_hunk_phrase(hunks)}"]
            receipt = result.get("revert")
            if receipt is not None:
                lines.extend(render_receipt(receipt))
            if result.get("reason"):
                lines.append(str(result["reason"]))
            if result.get("covered"):
                lines.append(
                    f"  restored whole file(s) covered entirely by the rejection: "
                    f"{len(result['covered'])}"
                )
            if decision.get("reason"):
                lines.append(f"  {decision['reason']}")
            return {
                "handled": True,
                "kind": "reject",
                "ok": bool(decision.get("ok"))
                and bool(result.get("applied") or result.get("partial")),
                "lines": lines,
                "granularity": "hunk",
                "payload": {
                    "decision": decision,
                    "revert": receipt.to_dict() if receipt is not None else None,
                    "applied": bool(result.get("applied")),
                    "applied_to": str(result.get("applied_to") or "nothing"),
                    "covered": list(result.get("covered") or []),
                    "partial": list(result.get("partial") or []),
                },
            }
        result = reject_paths(task_dir, repo, paths, force=force, in_flight=in_flight)
        receipt = result["revert"]
        lines = [f"rejected {len(result['decision'].get('paths') or [])} path(s)"]
        lines.extend(render_receipt(receipt))
        return {
            "handled": True,
            "kind": "reject",
            "ok": bool(receipt.ok),
            "lines": lines,
            "granularity": "file",
            "payload": {"decision": result["decision"], "revert": receipt.to_dict()},
        }

    receipt = revert_paths(task_dir, repo, paths, force=force, in_flight=in_flight)
    return {
        "handled": True,
        "kind": "revert",
        "ok": bool(receipt.ok),
        "lines": render_receipt(receipt),
        "payload": receipt.to_dict(),
    }
