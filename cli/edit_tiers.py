"""The edit TIER vocabulary, and the honest receipt when a tier is unknown.

The problem this module exists to close
--------------------------------------
Phase 2 built a three-tier guarantee about every edit the agent makes:

.. code-block:: text

    exact match  >  fuzzy match  >  LLM-repaired match

and a user reviewing a diff must be able to tell which tier produced an edit.
An LLM-repaired edit that is not visibly labelled is **the weakest guarantee
presented as the strongest**, which is worse than a missing permission.

What is actually true on this tree, MEASURED rather than assumed
----------------------------------------------------------------
* ``harness/fuzzy_edit.py`` implements the ladder and ``harness/editor.py``
  records ``match`` / ``match_strategy`` / ``match_confidence`` / ``match_guard``
  on its ``EditOutcome``. The producer is complete and the fields are serialised.
* **The ladder is not on the production edit path.** The typed kernel's ``edit``
  reaches ``execution/workspace.py::apply_exact_edit``, which raises
  ``WorkspaceEditError`` on a non-verbatim match. A fuzzy edit on the daily path
  is unreachable today.
* **No LLM-repair tier exists.** There is no ``llm_repaired`` value anywhere in
  ``harness/``, ``execution/``, ``cli/`` or ``runtime/``. Wave 1's brief asserted
  one; the measurement says it was not built.
* **Nothing carries a tier to this surface.** ``cli/runview.py`` projects three
  keys off a mutation row (``path``/``target``, ``actor``, ``reason``/``why``)
  and drops the rest, and ``cli/review.py`` reconstructs the diff from
  ``pristine/`` versus ``work/`` rather than from edit receipts at all.

So this module does the only honest thing available from ``cli/``: it **reads a
tier when the journal carries one**, renders it **visibly**, and renders
``unrecorded`` with a reason when it does not. It does not fabricate a tier, and
it does not pretend the LLM fixer exists.

The three properties, and where each is
----------------------------------------
=====================================  ==========================================
:func:`tier_label`                     the ONE word a reviewer reads
:func:`edit_tier_projection`           tier per path, from the journal
:func:`tier_lines` / :func:`tier_line` visibly distinct, one row per path
:data:`LLM_EDIT_REPAIR_TIER`           declared ABSENT, with a named owner
=====================================  ==========================================

Why the tiers are VISUALLY distinct and not just differently worded
------------------------------------------------------------------
The requirement is "distinguishable at a glance, not buried in metadata". So each
tier carries a declared **ASCII marker** in a closed vocabulary, and
:func:`tier_line` puts that marker FIRST -- before the path -- because a marker
at the end of a row is a marker a narrow terminal drops. :func:`assert_markers_are_distinct`
proves the three markers cannot collapse onto each other, and
:func:`assert_every_tier_has_a_distinct_marker` fails if a fourth tier is added
without one, which is the pressure that keeps the vocabulary honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

__all__ = [
    "EDIT_TIERS",
    "LLM_EDIT_REPAIR_STATUS",
    "LLM_EDIT_REPAIR_TIER",
    "TIER_MARKERS",
    "TIER_UNRECORDED",
    "EditTier",
    "assert_every_tier_has_a_distinct_marker",
    "assert_markers_are_distinct",
    "edit_tier_projection",
    "llm_repair_state",
    "tier_line",
    "tier_lines",
    "tier_summary",
]

#: The tier word used when the journal carries NO match information.
#:
#: A closed member of :data:`EDIT_TIERS` rather than an empty string, because
#: "we do not know" and "it was exact" are different facts and an empty field
#: renders as the second one. A file whose edit predates the tier fields reads as
#: ``unrecorded``, which is true, instead of ``exact``, which would be a claim
#: nobody can support.
TIER_UNRECORDED = "unrecorded"

#: The three tiers, strongest first. ``unrecorded`` is a FOURTH member and is
#: deliberately not a tier -- it is the absence of one, kept in the same closed
#: vocabulary so a renderer cannot accidentally treat it as the strongest tier.
EDIT_TIERS: Tuple[str, ...] = ("exact", "fuzzy", "llm_repaired", TIER_UNRECORDED)

#: The declared ASCII marker per tier. FIRST character of a row, on purpose.
#:
#: ASCII by construction, for the reason ``cli.approval_modes.MODE_MARKERS`` is:
#: a tier marker that needs an encoding fallback is a tier marker somebody
#: cannot read, and a diff review is exactly where somebody is relying on it.
TIER_MARKERS: Dict[str, str] = {
    "exact": "=",
    "fuzzy": "~",
    "llm_repaired": "!",
    TIER_UNRECORDED: "?",
}

#: Plain-language description per tier. Written for somebody who does not know
#: what a "cascade" is, because the reader is a person deciding whether to trust
#: a diff.
_TIER_TEXT: Dict[str, str] = {
    "exact": "the file contained this text verbatim",
    "fuzzy": "the text was matched approximately, not verbatim",
    "llm_repaired": "a model rewrote the edit to make it apply",
    TIER_UNRECORDED: (
        "the journal carries no match information for this edit, so the tier "
        "is unknown rather than assumed"
    ),
}

#: The LLM edit fixer. **DECLARED ABSENT, WITH A NAMED OWNER.**
#:
#: The wave-1 brief asserted a nine-stage cascade plus an opt-in LLM edit fixer.
#: Measured on this tree: the cascade has EIGHT stages (there is no ninth), and
#: **no LLM-repair tier exists anywhere** -- no producer, no config key, no
#: journal field, and no value in any vocabulary. Rather than render a tier the
#: product cannot produce -- which would be an affordance for a feature that does
#: not exist -- the state below says plainly that it is not implemented and who
#: owns building it. That is the difference between a receipt and a wish.
LLM_EDIT_REPAIR_TIER = "llm_repaired"

#: A frozen, honest statement about the LLM fixer. Read by ``/diff`` so a user
#: reviewing a diff knows whether a model was consulted for any edit in it --
#: which is a question the wave asks and which, today, has the answer "no tier
#: of that kind is produced at all".
LLM_EDIT_REPAIR_STATUS: Dict[str, Any] = {
    "implemented": False,
    "tier": LLM_EDIT_REPAIR_TIER,
    "owner": "T1 / harness",
    "reason": (
        "no LLM-repair stage exists in harness/fuzzy_edit.py or "
        "harness/third_party/opencode_replacer.py, no config key enables one, "
        "and no journal row carries the tier; the cascade that does exist has "
        "EIGHT stages, not nine"
    ),
    "opt_in_key": "",
    "evidence": (
        "measured by searching harness/, execution/, runtime/, cli/ for an "
        "llm_repair / llm_repaired producer; zero hits outside this module"
    ),
}

#: The journal keys a tier can arrive on, in the order they are read.
#:
#: A LIST rather than one key, because the producer's shape differs by path:
#: ``harness/editor.py`` puts ``match``/``match_strategy``/``match_confidence``
#: on the ``EditOutcome``'s ``to_dict()``, and a journal row may carry them flat
#: or under ``match_detail``. Reading one hard-coded key and concluding
#: ``unrecorded`` is how a tier would be reported as unknown while it was sitting
#: in the row.
_MATCH_KEYS: Tuple[str, ...] = ("match", "match_kind", "edit_match", "tier")
_DETAIL_KEYS: Tuple[str, ...] = ("match_detail", "match_report", "detail")
_STRATEGY_KEYS: Tuple[str, ...] = ("match_strategy", "strategy", "cascade_stage")
_CONFIDENCE_KEYS: Tuple[str, ...] = ("match_confidence", "confidence")
_GUARD_KEYS: Tuple[str, ...] = ("match_guard", "guard")


def llm_repair_state(config: Any = None) -> Dict[str, Any]:
    """Return whether an LLM edit fixer is in force for this run.

    Reads by KEY PRESENCE if a key is ever added, and reports the frozen
    :data:`LLM_EDIT_REPAIR_STATUS` either way. It deliberately does NOT consult
    ``harness/config.py::DEFAULTS``: a value there merges into every task and
    every eval arm, and "a model may rewrite your edits" is emphatically not a
    default anybody should inherit silently.

    Returns the same shape whatever it is asked, so a renderer has no branch to
    forget.
    """
    payload = dict(LLM_EDIT_REPAIR_STATUS)
    values = dict(config or {})
    for key in ("edit_llm_repair", "llm_edit_repair", "edit_fixer"):
        if key in values:
            payload["declared_key"] = key
            payload["declared_value"] = values.get(key)
            payload["implemented"] = False
            payload["note"] = (
                f"{key} is present in this run's configuration but no LLM "
                "repair stage consumes it, so no edit was and could be "
                "LLM-repaired; the key is read and reported, never honoured"
            )
            return payload
    return payload


@dataclass(frozen=True)
class EditTier:
    """One edit's tier, and everything a reviewer needs to judge it."""

    path: str
    tier: str
    strategy: str = ""
    confidence: float = 0.0
    guard: str = ""
    #: Why this tier, or why it is unknown. Never empty -- a tier with no
    #: reason is a label, and a label is not evidence.
    note: str = ""
    source: str = ""

    @property
    def marker(self) -> str:
        """The ASCII marker, declared per tier and never derived from the text."""
        return TIER_MARKERS.get(self.tier, TIER_MARKERS[TIER_UNRECORDED])

    @property
    def label(self) -> str:
        """The word a reviewer reads."""
        return f"{self.marker}{self.tier}"

    @property
    def known(self) -> bool:
        """Whether the tier was actually recorded, as opposed to absent."""
        return self.tier != TIER_UNRECORDED

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt."""
        return {
            "path": self.path,
            "tier": self.tier,
            "marker": self.marker,
            "label": self.label,
            "strategy": self.strategy,
            "confidence": round(float(self.confidence or 0.0), 4),
            "guard": self.guard,
            "known": self.known,
            "note": self.note,
            "source": self.source,
        }


def _first(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _read_tier(row: Mapping[str, Any]) -> Tuple[str, str, float, str]:
    """Return ``(tier, strategy, confidence, guard)`` from one journal row.

    Reads the flat keys and then the nested ``match_detail`` block, because the
    two producers disagree and a reader that checked only one would report a
    recorded tier as ``unrecorded``.
    """
    merged: Dict[str, Any] = {}
    for block in _DETAIL_KEYS:
        nested = row.get(block)
        if isinstance(nested, Mapping):
            merged.update(nested)
    for source in (row, merged):
        raw = _first(source, _MATCH_KEYS)
        if raw is None:
            continue
        tier = str(raw).strip().lower()
        strategy = _first(source, _STRATEGY_KEYS)
        confidence = _first(source, _CONFIDENCE_KEYS)
        guard = _first(source, _GUARD_KEYS)
        try:
            number = float(confidence) if confidence is not None else 0.0
        except (TypeError, ValueError):
            number = 0.0
        # An unknown tier WORD is preserved rather than folded into
        # `unrecorded`, so a producer that grows a fourth tier is VISIBLE in the
        # receipt instead of quietly reading as "we do not know".
        return (
            tier or TIER_UNRECORDED,
            str(strategy or ""),
            number,
            str(guard or ""),
        )
    return (TIER_UNRECORDED, "", 0.0, "")


#: The journal kinds whose payload may carry a tier. Read from the review
#: module's OWN mutation-kind vocabulary so a new mutation kind is picked up
#: rather than silently excluded from the tier projection.
def _mutation_kinds() -> Tuple[str, ...]:
    try:
        from cli.review import _MUTATION_JOURNAL_KINDS

        return tuple(_MUTATION_JOURNAL_KINDS)
    except Exception:
        return (
            "edit_applied",
            "edit_rolled_back",
            "edit_refused",
            "edit_result",
            "file_changed",
            "patch_edit",
            "file_change",
        )


def edit_tier_projection(journal_rows: Iterable[Any] = ()) -> Dict[str, EditTier]:
    """Return ``path -> EditTier`` read from a run's own journal rows.

    Total: a journal this build cannot read yields an EMPTY projection rather
    than an exception, and the caller renders ``unrecorded`` for every path
    rather than a blank row -- because an empty list and an unknown tier are
    different facts and only one of them is true.

    **Newest row wins per path**, matching the review surface's own convention
    for a file that was edited more than once: the tier a reviewer needs is the
    one that produced the bytes they are looking at.
    """
    kinds = set(_mutation_kinds())
    out: Dict[str, EditTier] = {}
    for row in journal_rows or ():
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or row.get("event") or "").strip().lower()
        if kind and kinds and kind not in kinds:
            continue
        payload = row.get("data")
        source = payload if isinstance(payload, Mapping) else row
        path = str(
            _first(source, ("path", "file", "target", "relative_path")) or ""
        ).strip().replace("\\", "/")
        if not path:
            continue
        tier, strategy, confidence, guard = _read_tier(source)
        out[path] = EditTier(
            path=path,
            tier=tier,
            strategy=strategy,
            confidence=confidence,
            guard=guard,
            note=_TIER_TEXT.get(tier, f"an unrecognised tier word ({tier!r})"),
            source=kind or "journal",
        )
    return out


def tier_lines(projection: Mapping[str, Any], *, paths: Sequence[str] = ()) -> List[str]:
    """Return one visibly-marked line per edit, marker FIRST.

    Marker first because a marker at the end of a row is a marker a narrow
    terminal drops, and this is the one place a reviewer is relying on it. Each
    line names the path, the tier, the strategy and confidence for a fuzzy
    match, and -- for an unknown tier -- WHY it is unknown.
    """
    wanted = [str(item) for item in (paths or ())] or sorted(str(k) for k in projection)
    lines: List[str] = []
    for path in wanted:
        tier = projection.get(path)
        if tier is None:
            marker = TIER_MARKERS[TIER_UNRECORDED]
            lines.append(
                f"  {marker}{TIER_UNRECORDED}  {path} - "
                f"{_TIER_TEXT[TIER_UNRECORDED]}"
            )
            continue
        detail = ""
        if tier.tier == "fuzzy":
            strategy = tier.strategy or "an unnamed stage"
            detail = f" via {strategy} at {tier.confidence:.2f} confidence"
            if tier.guard:
                detail += f", guard {tier.guard}"
        # The note is FALLEN BACK rather than trusted. ``EditTier.note`` is
        # documented as "never empty", but a hand-constructed tier can leave it
        # blank and the first version of this function rendered
        # ``?unrecorded  a.py - `` with nothing after the dash -- a row that
        # names a state and says nothing about it, which is the blank-affordance
        # defect one layer up. The vocabulary is the fallback because it is the
        # one thing guaranteed to cover every tier.
        note = (tier.note or "").strip() or _TIER_TEXT.get(
            tier.tier, f"an unrecognised tier word ({tier.tier!r})"
        )
        lines.append(f"  {tier.label:<14} {path} - {note}{detail}")
    return lines


def tier_line(tier: Any, *, path: str = "") -> str:
    """Return the ONE line for a single edit's tier."""
    if isinstance(tier, EditTier):
        return (tier_lines({tier.path: tier}, paths=[tier.path]) or [""])[0]
    known = EditTier(
        path=str(path or ""),
        tier=str(tier or TIER_UNRECORDED),
        note=_TIER_TEXT.get(str(tier or TIER_UNRECORDED), "an unrecognised tier"),
    )
    return (tier_lines({known.path: known}, paths=[known.path]) or [""])[0]


def tier_summary(projection: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the counts per tier, plus the LLM-fixer's own state.

    A summary a ``--json`` surface or ``/approval`` row can publish, so the
    question "was any edit in this run weaker than verbatim?" has an answer
    that is a NUMBER rather than something a reader has to count.
    """
    counts: Dict[str, int] = {name: 0 for name in EDIT_TIERS}
    for tier in (projection or {}).values():
        counts[tier.tier] = counts.get(tier.tier, 0) + 1
    weakest = [name for name in EDIT_TIERS if name not in ("exact", TIER_UNRECORDED)]
    weaker = sum(counts.get(name, 0) for name in weakest)
    return {
        "tiers": list(EDIT_TIERS),
        "markers": dict(TIER_MARKERS),
        "counts": counts,
        "edits": sum(counts.values()),
        "weaker_than_verbatim": weaker,
        "llm_repair": llm_repair_state(),
    }


# ---------------------------------------------------------------------------
# The two marker gates
# ---------------------------------------------------------------------------


def assert_markers_are_distinct() -> None:
    """Raise if two tiers share a marker, or a marker is not ASCII.

    Two tiers with the same marker are not distinguishable "at a glance", which
    is the entire requirement, and a non-ASCII marker is a marker that needs an
    encoding fallback on a ``cp1252`` console -- at which point the tier is
    invisible to exactly the reader who most needs it.
    """
    seen: Dict[str, str] = {}
    for tier, marker in TIER_MARKERS.items():
        if not marker.isascii():
            raise AssertionError(
                f"tier {tier!r} has the non-ASCII marker {marker!r}; a tier "
                "marker that needs an encoding fallback is not readable on every "
                "console this product ships on"
            )
        if marker in seen:
            raise AssertionError(
                f"tiers {seen[marker]!r} and {tier!r} share the marker "
                f"{marker!r}, so they are not distinguishable at a glance"
            )
        seen[marker] = tier


def assert_every_tier_has_a_distinct_marker() -> None:
    """Raise if a declared tier has no marker or no plain-language note.

    The pressure that keeps :data:`EDIT_TIERS` honest: a fourth tier added to
    the vocabulary without a marker and a sentence is a tier no reviewer can
    read, and this is the gate that says so at import-test time rather than in a
    screenshot nobody diffs.
    """
    assert_markers_are_distinct()
    for tier in EDIT_TIERS:
        if not TIER_MARKERS.get(tier):
            raise AssertionError(f"tier {tier!r} has no marker")
        if not _TIER_TEXT.get(tier):
            raise AssertionError(
                f"tier {tier!r} has no plain-language note; a label with no "
                "sentence is not something a reviewer can judge"
            )
    if LLM_EDIT_REPAIR_TIER not in EDIT_TIERS:
        raise AssertionError(
            "the LLM-repair tier is declared but missing from EDIT_TIERS; a "
            "tier a renderer cannot name is a tier nobody can read"
        )
