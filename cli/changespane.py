"""The right-hand CHANGES pane: a live change list with per-hunk decisions.

Phase 5/W1 task T5.W1.1. This module is the model and the renderers; the
widget that mounts it lives in :mod:`cli.workpane` so the three panes share
one mount, one keyboard map and one width policy.

Five properties are the whole point of this module, and each is a gate
rather than a comment:

1. **Per-hunk accept/reject, in the pane.** Every action routes through
   :func:`cli.review.accept_hunks` / :func:`cli.review.reject_hunks` /
   :func:`cli.review.revert_paths` -- the ONE authority the ``/diff`` verbs
   already use. A second decision path here would be two answers to "did the
   person keep this hunk", and two answers is how the pane and ``/diff``
   start disagreeing about the same file.
2. **The edit's provenance tier is visible and visually distinct.** ``exact``,
   ``fuzzy`` and ``llm_repaired`` are read from the run's OWN journal through
   :mod:`cli.edit_tiers` and rendered with that module's declared ASCII marker
   first on the row. A tier marker at the END of a row is a tier marker a
   narrow terminal drops.
3. **Stale and conflicted state is never presented as ordinary.** A file whose
   live bytes moved outside the run's accounted hash set is ``STALE``; a path
   git reports as an unmerged conflict is ``CONFLICT``. Both are asserted by
   :func:`assert_no_unusual_state_renders_as_ordinary`, which has an in-file
   control arm that plants each state and requires the assertion to fire.
4. **Every rendered value crosses :func:`cli.ui.sanitize_text`.** The renderer
   is the sanitise boundary, exactly as :func:`cli.review._bound` is for the
   review modal, so a repository path carrying a credential cannot reach a
   terminal through this pane.
5. **A bound is a marked bound.** Truncation, oversized diffs and narrow
   panes all emit a marker and a count. A silently shortened row reads as a
   complete one.

The row model is deliberately a value, not a widget: every renderer here is
pure and total, so the whole surface can be exercised without mounting Textual,
and a mount can never be the only evidence a claim has.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cli import edit_tiers as _tiers
from cli import fileview as _fv
from cli import review as _review
from cli import ui as _ui

__all__ = [
    "BINARY_LABEL",
    "CONFLICT_CODES",
    "CONFLICT_LABEL",
    "DELETED_LABEL",
    "ELLIPSIS",
    "MAX_ROWS",
    "MIN_PANE_COLUMNS",
    "NARROW_PANE_COLUMNS",
    "PANE_WIDGET_ID",
    "RENAME_LABEL",
    "STALE_LABEL",
    "TRUNCATED_NOTE",
    "WIDE_PANE_COLUMNS",
    "ChangeRow",
    "PaneModel",
    "apply_file_revert",
    "apply_hunk_accept",
    "apply_hunk_reject",
    "assert_layers_are_exhaustive",
    "assert_no_unusual_state_renders_as_ordinary",
    "change_list_lines",
    "change_rows",
    "file_row",
    "hunk_row",
    "hunk_view_lines",
    "pane_lines",
    "pane_model",
    "tier_label",
    "tier_style",
]

#: The widget id the container mounts this pane under. Declared beside the
#: payload it renders so the two cannot drift, which is the same reason
#: ``cli/review.py`` declares ``REVIEW_WIDGET_ID``.
PANE_WIDGET_ID = "neo-changes"

ELLIPSIS = "…"

#: Below this width the pane cannot render a path plus a marker plus a
#: verdict, and the honest answer is to say so rather than clip a fact.
MIN_PANE_COLUMNS = 24
#: The deliberate narrowing point: below this the file list collapses to the
#: focused file only. Measured against a 42-column rail at 120x36 this is the
#: column count the pane actually receives once padding is removed.
NARROW_PANE_COLUMNS = 44
WIDE_PANE_COLUMNS = 62

#: A row budget, reported rather than applied silently.
MAX_ROWS = 400

#: Porcelain v1 codes git uses for an UNMERGED path. Anything outside this
#: set is an ordinary dirty/modified path and must render as one.
CONFLICT_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})

STALE_LABEL = "STALE"
CONFLICT_LABEL = "CONFLICT"
BINARY_LABEL = "binary"
DELETED_LABEL = "deleted"
RENAME_LABEL = "renamed"

#: The bound marker a truncated hunk view carries. It states the bound so a
#: shortened diff cannot read as a complete one.
TRUNCATED_NOTE = "(diff truncated; showing the first {shown} of {total} hunk(s))"

#: Per-tier rich style NAMES. Declared as data so "the three tiers are
#: visually distinct" is a structural property rather than a claim about three
#: f-strings somebody wrote once. ``unrecorded`` is a FOURTH member and is
#: deliberately not a tier -- it is the absence of one.
TIER_STYLES: Dict[str, str] = {
    "exact": "green",
    "fuzzy": "yellow",
    "llm_repaired": "red",
    _tiers.TIER_UNRECORDED: "grey62",
}


def _bound(value: Any, width: int, *, marker: str = ELLIPSIS) -> str:
    """Sanitise then bound ``value`` to ``width`` columns, marking any cut.

    The ORDER is load-bearing and is the same one ``cli/review.py::_bound``
    documents: strip escapes and redact FIRST (so the bytes the terminal will
    draw are the bytes the redactor saw), and only then shorten. Shortening
    first can cut a credential in half and hand the redactor two harmless
    halves.

    Never raises. A sanitiser that fails closed returns its own withholding
    marker, and that marker is what renders -- a renderer that fails open by
    printing the raw value would reopen the whole Phase 0 leak.
    """
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


def tier_label(tier: Any, *, strategy: str = "", confidence: Any = 0.0) -> str:
    """The tier word a reviewer reads, with a fuzzy strategy and confidence.

    Delegates to :mod:`cli.edit_tiers` for the marker so this pane cannot
    invent a second marker vocabulary, and renders ``unrecorded`` rather than
    defaulting to ``exact``: on this tree the typed kernel's edit path is
    exact-only, so a journal that carries no match field is SILENT about which
    guarantee the reader is looking at, and reading silence as the strongest
    tier asserts verbatim matching from an absence of evidence.
    """
    word = str(tier or _tiers.TIER_UNRECORDED).strip().lower()
    marker = _tiers.TIER_MARKERS.get(word, _tiers.TIER_MARKERS[_tiers.TIER_UNRECORDED])
    if word == "fuzzy":
        detail = str(strategy or "").strip()
        try:
            score = float(confidence or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if detail or score > 0.0:
            shown = detail or "approximate match"
            return f"{marker}{word} {shown}@{score:.2f}"
    return f"{marker}{word}"


def tier_style(tier: Any) -> str:
    """The declared style NAME for a tier. Unrecognised words get ``unrecorded``."""
    word = str(tier or _tiers.TIER_UNRECORDED).strip().lower()
    return TIER_STYLES.get(word, TIER_STYLES[_tiers.TIER_UNRECORDED])


@dataclass(frozen=True)
class ChangeRow:
    """One file's change, everything the pane may say about it, and nothing else.

    Every field is a MEASURED fact or a declared ``False``. There is no
    ``state: str = "ok"`` field, because a free-form state is where "stale"
    and "ordinary" end up spelled the same way.
    """

    path: str
    kind: str = "modified"
    state: str = "unchanged"
    decision: str = "pending"
    verdict: str = "pending"
    tier: str = _tiers.TIER_UNRECORDED
    tier_strategy: str = ""
    tier_confidence: float = 0.0
    verified: bool = False
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    truncated: bool = False
    hunk_headers: Tuple[str, ...] = ()
    hunk_decisions: Tuple[Tuple[int, str], ...] = ()
    #: True when the live bytes differ from the bytes the review recorded AND
    #: the run does not account for the difference -- the same signal the
    #: concurrent-edit gate refuses on, read as a DISPLAY fact rather than a
    #: write refusal.
    stale: bool = False
    #: True when the live bytes differ but the run DOES account for them, so
    #: the diff is behind the tree but is still this run's work.
    drifted: bool = False
    conflict: bool = False
    git_code: str = ""
    content_hash: str = ""
    accounted_hashes: Tuple[str, ...] = ()
    reason: str = ""
    #: File modification time, used for the newest-first ordering. ``0.0`` for
    #: a path that does not exist on disk (a deleted file), and a deleted file
    #: sorts last rather than first.
    mtime: float = 0.0

    @property
    def changed(self) -> bool:
        """Whether this row is a change at all, for the tree's distinct marker."""
        return str(self.state or "") in {"changed", "unrestorable"}

    @property
    def deleted(self) -> bool:
        """A file the run removed. ``unrecorded`` is NOT deleted -- it is unknown."""
        return str(self.kind or "") == "deleted"

    @property
    def renamed(self) -> bool:
        """A rename is a distinct fact from a delete plus an add."""
        return str(self.kind or "") == "renamed"

    def hunk_verdict(self, index: Any) -> str:
        """One hunk's decision word, or ``pending`` when undecided."""
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            return "pending"
        for number, word in self.hunk_decisions:
            if int(number) == wanted:
                return str(word)
        return "pending"

    def hunk_summary(self) -> str:
        """``2 accepted / 1 rejected / 3 undecided``. Undecided are COUNTED."""
        total = len(self.hunk_headers)
        accepted = sum(1 for _, word in self.hunk_decisions if str(word) == "accepted")
        rejected = sum(1 for _, word in self.hunk_decisions if str(word) == "rejected")
        return f"{accepted} accepted / {rejected} rejected / {max(0, total - len(self.hunk_decisions))} undecided"

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-friendly projection. Every value is data, never markup."""
        return {
            "path": self.path,
            "kind": self.kind,
            "state": self.state,
            "decision": self.decision,
            "verdict": self.verdict,
            "tier": self.tier,
            "tier_strategy": self.tier_strategy,
            "tier_confidence": float(self.tier_confidence),
            "verified": bool(self.verified),
            "additions": int(self.additions),
            "deletions": int(self.deletions),
            "binary": bool(self.binary),
            "truncated": bool(self.truncated),
            "hunks": len(self.hunk_headers),
            "hunk_decisions": [
                [int(number), str(word)] for number, word in self.hunk_decisions
            ],
            "stale": bool(self.stale),
            "drifted": bool(self.drifted),
            "conflict": bool(self.conflict),
            "git_code": self.git_code,
            "deleted": self.deleted,
            "renamed": self.renamed,
        }


def _mtime(root: Any, relative: str) -> float:
    """Last-write time of ``relative`` under ``root``, or ``0.0`` when absent."""
    if not root or not relative:
        return 0.0
    try:
        target = _fv.safe_repo_path(root, relative)
    except Exception:
        target = None
    if target is None:
        return 0.0
    try:
        return float(target.stat().st_mtime)
    except (OSError, ValueError):
        return 0.0


def change_rows(
    document: Any,
    repo: Any = None,
    *,
    stale_check: bool = True,
) -> Tuple[ChangeRow, ...]:
    """Project a review document into pane rows, newest first, grouped by file.

    Ordering is by live file modification time DESCENDING with the path as the
    tiebreak, so the row order is deterministic on a tree where several files
    share a timestamp (which is every tree on a filesystem with coarse mtime
    resolution). A deleted file has no mtime and sorts LAST, because "newest"
    for a file that is not there is not a fact.

    ``stale_check=False`` skips the per-file live hash read. The cheap sample
    path uses it; the full render does not, because a change list that cannot
    say STALE is the failure this pane exists to prevent.
    """
    files = tuple(getattr(document, "files", ()) or ())
    status: Dict[str, Dict[str, Any]] = {}
    if repo:
        try:
            status = _fv.read_git_status(repo)
        except Exception:
            status = {}
    rows: List[ChangeRow] = []
    for record in files:
        path = str(getattr(record, "path", "") or "")
        if not path:
            continue
        facts = status.get(path) or {}
        code = str(facts.get("code") or facts.get("status") or "")
        conflict = code in CONFLICT_CODES
        stale = False
        drifted = False
        if stale_check and not conflict:
            stale, drifted = _staleness(record, repo, path)
        rows.append(
            ChangeRow(
                path=path,
                kind=str(getattr(record, "kind", "modified") or "modified"),
                state=str(getattr(record, "state", "unchanged") or "unchanged"),
                decision=str(getattr(record, "decision", "pending") or "pending"),
                verdict=str(getattr(record, "verdict", "pending") or "pending"),
                tier=str(getattr(record, "edit_tier", _tiers.TIER_UNRECORDED) or _tiers.TIER_UNRECORDED),
                tier_strategy=str(getattr(record, "edit_strategy", "") or ""),
                tier_confidence=float(getattr(record, "edit_confidence", 0.0) or 0.0),
                verified=bool(getattr(record, "verified", False)),
                additions=int(getattr(record, "additions", 0) or 0),
                deletions=int(getattr(record, "deletions", 0) or 0),
                binary=bool(getattr(record, "binary", False)),
                truncated=bool(getattr(record, "truncated", False)),
                hunk_headers=tuple(str(item) for item in (getattr(record, "hunk_headers", ()) or ())),
                hunk_decisions=tuple(
                    (int(number), str(word))
                    for number, word in (getattr(record, "hunk_decisions", ()) or ())
                ),
                stale=stale,
                drifted=drifted,
                conflict=conflict,
                git_code=code,
                content_hash=str(getattr(record, "content_hash", "") or ""),
                accounted_hashes=tuple(
                    str(item) for item in (getattr(record, "accounted_hashes", ()) or ())
                ),
                reason=str(getattr(record, "reason", "") or ""),
                mtime=_mtime(repo, path),
            )
        )
    rows.sort(key=lambda item: (-item.mtime, item.path))
    return tuple(rows)


def _staleness(record: Any, repo: Any, path: str) -> Tuple[bool, bool]:
    """``(stale, drifted)`` for one path, read from the run's own accounting.

    The rule is :mod:`cli.review`'s, read as a DISPLAY fact: the live hash is
    compared against the set of hashes the run is accountable for. A live hash
    outside that set is a third-party edit (``stale``); a live hash inside it is
    this run's own work that the diff has not caught up with (``drifted``).
    Rendering both as ordinary is the lie, and rendering them identically would
    be a second lie -- they are different facts with different fixes.
    """
    recorded = str(getattr(record, "content_hash", "") or "")
    accounted = {str(item) for item in (getattr(record, "accounted_hashes", ()) or ())}
    pre = str(getattr(record, "pre_image_hash", "") or "")
    if pre:
        accounted.add(pre)
    if not recorded and not accounted:
        return (False, False)
    try:
        live = _fv.content_hash(_fv.safe_repo_path(repo, path))
    except Exception:
        return (False, False)
    if not live:
        return (False, False)
    if live == recorded:
        return (False, False)
    return (False, True) if live in accounted else (True, False)


@dataclass(frozen=True)
class PaneModel:
    """The pane's whole state as a value, so a render never reads the world."""

    rows: Tuple[ChangeRow, ...] = ()
    task_id: str = ""
    repo: str = ""
    tree: str = "live"
    tree_note: str = ""
    #: ``files_shown`` / ``files_total`` is a BOUND that is stated, because a
    #: capped list that says nothing about the cap reads as a complete answer.
    files_shown: int = 0
    files_total: int = 0
    total_hunks: int = 0
    widths: Tuple[int, ...] = ()
    notes: Tuple[str, ...] = ()
    reviewable: bool = True
    reviewable_reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-friendly projection of the pane, used by the truth gate."""
        return {
            "task_id": self.task_id,
            "repo": self.repo,
            "tree": self.tree,
            "tree_note": self.tree_note,
            "files_shown": int(self.files_shown),
            "files_total": int(self.files_total),
            "total_hunks": int(self.total_hunks),
            "widths": [int(item) for item in self.widths],
            "reviewable": bool(self.reviewable),
            "reviewable_reason": self.reviewable_reason,
            "notes": list(self.notes),
            "rows": [row.as_dict() for row in self.rows],
        }


def pane_model(
    document: Any,
    *,
    repo: Any = None,
    max_rows: int = MAX_ROWS,
    stale_check: bool = True,
) -> PaneModel:
    """Build the pane model from a review document, honouring a stated row bound.

    ``max_rows`` caps the ROWS and the omission is reported as
    ``files_shown`` / ``files_total`` rather than applied silently.

    ``repo`` OVERRIDES the document's own ``repo`` field. That override exists
    because reading live git state changes the row ORDER (a path with a real
    mtime sorts differently from one without), so a caller that read its rows
    from one repository and rendered them through a model built from another
    would focus the wrong file. One repository, one ordering -- measured when
    the truth gate rendered a conflict fixture and every non-focused row came
    out as its neighbour.
    """
    try:
        cap = max(1, int(max_rows))
    except (TypeError, ValueError):
        cap = MAX_ROWS
    every = change_rows(
        document,
        repo if repo is not None else getattr(document, "repo", None),
        stale_check=stale_check,
    )
    shown = every[:cap]
    notes: List[str] = []
    snapshot = getattr(document, "snapshot", None)
    state = str(getattr(snapshot, "state", "") or "")
    if state and state not in {"present", "ok"}:
        notes.append(f"reference snapshot is {state}")
    for extra in getattr(document, "notes", ()) or ():
        text = str(extra).strip()
        if text:
            notes.append(text)
    if getattr(document, "unevidenced", ()):
        notes.append(
            f"{len(tuple(document.unevidenced))} path(s) the run's own journal does not evidence"
        )
    return PaneModel(
        rows=tuple(shown),
        task_id=str(getattr(document, "task_id", "") or ""),
        repo=str(getattr(document, "repo", "") or ""),
        tree=str(getattr(document, "tree", "live") or "live"),
        tree_note=str(getattr(document, "tree_note", "") or ""),
        files_shown=len(shown),
        files_total=len(every),
        total_hunks=sum(len(row.hunk_headers) for row in shown),
        notes=tuple(notes),
        reviewable=bool(getattr(document, "reviewable", True)),
        reviewable_reason=str(getattr(document, "reviewable_reason", "") or ""),
    )


def _state_markers(row: ChangeRow) -> List[str]:
    """The words that must appear for a row's unusual state, in a fixed order.

    Declared as ONE list so the renderer, the gate and the file tree agree on
    the vocabulary instead of each spelling it.
    """
    out: List[str] = []
    if row.conflict:
        out.append(CONFLICT_LABEL)
    if row.stale:
        out.append(STALE_LABEL)
    if row.drifted:
        out.append("behind-tree")
    if row.binary:
        out.append(BINARY_LABEL)
    if row.deleted:
        out.append(DELETED_LABEL)
    if row.renamed:
        out.append(RENAME_LABEL)
    if row.truncated:
        out.append("truncated")
    return out


def file_row(row: ChangeRow, width: int = 60, *, focused: bool = False) -> str:
    """One file's row: marker FIRST, then the path, then the facts that fit.

    The marker is first on purpose. This repository has recorded four separate
    disclosures eaten by a width bound, and the pattern is always the same: the
    fact that got eaten was at the tail. A STALE or CONFLICT label that the
    terminal drops is a surface that reported the conflict to nobody.
    """
    markers = _state_markers(row)
    caret = ">" if focused else " "
    tier = tier_label(row.tier, strategy=row.tier_strategy, confidence=row.tier_confidence)
    head = f"{caret} {tier} "
    if markers:
        head += "[" + "|".join(markers) + "] "
    limit = max(1, int(width))
    fixed = len(head) + 1 + len(row.verdict)
    body = _bound(row.path, max(4, limit - fixed))
    tail = f" {row.verdict}" if row.verdict else ""
    if row.binary:
        tail += f" (+{row.additions}/-{row.deletions} not shown: binary)"
    elif row.additions or row.deletions:
        tail += f" +{row.additions}/-{row.deletions}"
    return _bound(head + body + tail, limit)


def hunk_row(row: ChangeRow, index: Any, width: int = 60, *, focused: bool = False) -> str:
    """One hunk's row: its 1-based address, its decision, and its header."""
    try:
        number = max(1, int(index))
    except (TypeError, ValueError):
        number = 1
    headers = row.hunk_headers
    header = ""
    if 0 < number <= len(headers):
        header = str(headers[number - 1])
    caret = ">" if focused else " "
    verdict = row.hunk_verdict(number)
    head = f"{caret} #{number} {verdict} "
    limit = max(1, int(width))
    return _bound(head + _bound(header, max(4, limit - len(head))), limit)


def change_list_lines(
    model: Any,
    width: int = 60,
    *,
    focused: int = 0,
    expanded: Optional[Sequence[int]] = None,
    rows: Optional[int] = None,
) -> List[str]:
    """Render the change LIST: the grouped, newest-first file rows.

    Below :data:`NARROW_PANE_COLUMNS` the list collapses to the focused file
    alone and SAYS SO. That is the deliberate narrowing behaviour rather than
    clipping every path to a few characters, which is the failure this package
    has recorded four times.
    """
    limit = max(1, int(width))
    lines: List[str] = []
    if limit < MIN_PANE_COLUMNS:
        return [
            f"pane too narrow ({limit} column(s)): widen to {MIN_PANE_COLUMNS}+ "
            "or press ctrl+d to collapse it"
        ]
    entries = tuple(getattr(model, "rows", ()) or ())
    if not entries:
        return [_bound("no changes this run is evidenced to have made", limit)]
    if limit < NARROW_PANE_COLUMNS:
        if entries:
            lines.append(_bound(f"narrow ({limit} col): {len(entries)} file(s)", limit))
            index = max(0, min(int(focused or 0), len(entries) - 1))
            lines.append(file_row(entries[index], limit, focused=True))
        return lines
    index = max(0, min(int(focused or 0), len(entries) - 1))
    shown = entries if rows is None else entries[: max(1, int(rows))]
    for position, row in enumerate(shown):
        lines.append(file_row(row, limit, focused=position == index))
        if position in set(int(item) for item in (expanded or ())):
            lines.extend(hunk_view_lines(row, limit, focused=None, rows=4))
    omitted = len(entries) - len(shown)
    if omitted > 0:
        lines.append(_bound(f"+{omitted} more file(s) not shown", limit))
    return lines


def hunk_view_lines(
    row: ChangeRow,
    width: int = 60,
    *,
    focused: Optional[int] = None,
    rows: Optional[int] = None,
) -> List[str]:
    """Render one file's HUNKS, with the diff truncation bound stated.

    An empty hunk list is a fact, not a blank: a binary file, a deleted file
    and a single-file diff all have zero addressable hunks and each needs a
    different sentence.
    """
    limit = max(1, int(width))
    if row.binary:
        return [_bound(f"binary file - no textual hunk to address ({BINARY_LABEL})", limit)]
    if row.conflict:
        return [
            _bound(f"{CONFLICT_LABEL}: git reports this path unmerged; resolve it before reviewing", limit),
            _bound(f"  git status: {_bound(row.git_code, 12)}", limit),
        ]
    headers = row.hunk_headers
    if not headers:
        if row.deleted:
            return [_bound("deleted by this run - no hunk; use the file row to revert it", limit)]
        return [_bound("no addressable hunk in this diff", limit)]
    budget = len(headers) if rows is None else max(1, int(rows))
    lines = [
        hunk_row(row, number, limit, focused=(focused == number))
        for number in range(1, min(len(headers), budget) + 1)
    ]
    if len(headers) > budget:
        lines.append(_bound(TRUNCATED_NOTE.format(shown=budget, total=len(headers)), limit))
    return lines


def pane_lines(
    model: Any,
    width: int = 60,
    *,
    focused: int = 0,
    expanded: Optional[Sequence[int]] = None,
    rows: Optional[int] = None,
) -> List[str]:
    """The whole pane: header, the bounded change list, and every stated note.

    Never returns an empty list. An empty render is indistinguishable from a
    surface that does not exist, and this pane's whole purpose is to be the
    answer to "what did it change".
    """
    limit = max(1, int(width))
    header = f"changes {int(getattr(model, 'files_shown', 0))}/{int(getattr(model, 'files_total', 0))}"
    tree_note = str(getattr(model, "tree_note", "") or "")
    if tree_note:
        header += f" · {_bound(tree_note, max(4, limit - len(header) - 3))}"
    lines = [_bound(header, limit)]
    if not bool(getattr(model, "reviewable", True)):
        lines.append(_bound(f"NOT REVIEWABLE: {getattr(model, 'reviewable_reason', '')}", limit))
    lines.extend(
        change_list_lines(model, limit, focused=focused, expanded=expanded, rows=rows)
    )
    for note in getattr(model, "notes", ()) or ():
        lines.append(_bound(f"note: {note}", limit))
    return [_bound(line, limit) for line in lines if str(line).strip()] or [
        _bound("changes pane: nothing to report", limit)
    ]


def apply_hunk_accept(task_dir: Any, path: str, index: Any, **kwargs: Any) -> Dict[str, Any]:
    """Accept ONE hunk through :mod:`cli.review` and return the ONE shape.

    Returns ``fileview.undo_command``'s shape, which is what every shell in
    this package already renders. A pane that invented its own receipt
    protocol would be a second protocol.
    """
    safe = str(path or "")
    if not safe:
        return {"handled": True, "kind": "accept", "ok": False, "lines": ["no path was focused"], "payload": {}}
    try:
        number = max(1, int(index))
    except (TypeError, ValueError):
        number = 1
    result = _review.review_command(task_dir, kwargs.pop("repo", None), f"{safe}#{number}", verb="accept", **kwargs)
    result["granularity"] = "hunk"
    return result


def apply_hunk_reject(task_dir: Any, path: str, index: Any, **kwargs: Any) -> Dict[str, Any]:
    """Reject ONE hunk through :mod:`cli.review`, including its partial-restore rule.

    A partial rejection is recorded and REPORTED as not applied by
    :func:`cli.review.reject_hunks`; this wrapper adds nothing and suppresses
    nothing, so the pane cannot render a rejection as an applied revert.
    """
    safe = str(path or "")
    if not safe:
        return {"handled": True, "kind": "reject", "ok": False, "lines": ["no path was focused"], "payload": {}}
    try:
        number = max(1, int(index))
    except (TypeError, ValueError):
        number = 1
    result = _review.review_command(task_dir, kwargs.pop("repo", None), f"{safe}#{number}", verb="reject", **kwargs)
    result["granularity"] = "hunk"
    return result


def apply_file_revert(task_dir: Any, path: str, **kwargs: Any) -> Dict[str, Any]:
    """Revert ONE file through :mod:`cli.review`, so the tier receipt arrives.

    :func:`cli.review.revert_paths` stamps ``edit_tiers`` onto its receipt, so
    the pane's undo says WHICH guarantee produced the bytes it removed rather
    than only how many files came back.
    """
    safe = str(path or "")
    if not safe:
        return {"handled": True, "kind": "revert", "ok": False, "lines": ["no path was focused"], "payload": {}}
    return _review.review_command(task_dir, kwargs.pop("repo", None), safe, verb="revert", **kwargs)


def assert_no_unusual_state_renders_as_ordinary() -> None:
    """Refuse to ship if an unusual state can render as an ordinary row.

    The gate is the requirement, and its control arm is this module: it plants a
    conflict row, a stale row, a drifted row, a binary row, a deleted row and a
    renamed row, renders each, and REQUIRES the declared word to be present.
    Without that arm the gate is a comment, and a comment cannot notice a
    renderer that dropped the marker.
    """
    for marker, row in (
        (CONFLICT_LABEL, ChangeRow(path="a.py", conflict=True)),
        (STALE_LABEL, ChangeRow(path="a.py", stale=True)),
        ("behind-tree", ChangeRow(path="a.py", drifted=True)),
        (BINARY_LABEL, ChangeRow(path="a.py", binary=True)),
        (DELETED_LABEL, ChangeRow(path="a.py", kind="deleted")),
        (RENAME_LABEL, ChangeRow(path="a.py", kind="renamed")),
    ):
        rendered = " ".join(
            [file_row(row, 80), *hunk_view_lines(row, 80)]
        )
        assert marker in rendered, (
            f"the {marker} state renders as an ordinary row: {rendered!r}"
        )
    for tier in ("exact", "fuzzy", "llm_repaired"):
        assert tier in file_row(ChangeRow(path="a.py", tier=tier), 80), (
            f"tier {tier} does not name itself on the row"
        )


def assert_layers_are_exhaustive() -> None:
    """Every declared tier style exists and every tier style is pairwise distinct.

    Distinctness is the property that makes "visually distinct" true; two tiers
    sharing a style render identically at every colour depth, so the test is
    about the STYLE SET and not about the labels.
    """
    for tier, style in TIER_STYLES.items():
        assert tier in _tiers.EDIT_TIERS, f"{tier} is not a member of the edit tier vocabulary"
        assert isinstance(style, str) and style, f"tier {tier} has no declared style"
    assert len(set(TIER_STYLES.values())) == len(TIER_STYLES), (
        f"two tiers share a style, so they are not visually distinct: {TIER_STYLES}"
    )
    assert len(set(_tiers.TIER_MARKERS.values())) == len(_tiers.TIER_MARKERS), (
        "two tiers share a marker"
    )


__all__ += ["TIER_STYLES", "PaneModel"]
