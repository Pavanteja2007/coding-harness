"""Memory is context; a verifier run is evidence (P3a W1, T2).

Phase 3a makes runs **different from each other**: a run that recalls a memory
takes a different path than the same run without it. That is the feature
working. It is also, for a measurement system, two new ways to be lied to, and
this module closes both.

**1. A stored memory must NEVER satisfy a :class:`VerificationResult`.** A row
saying "this test passes" is a claim made by a past run and stored as text. It
is not a measurement of the tree in front of you, and nothing downstream of a
memory may clear a verification boolean. Today that is true, but it is true by
ACCIDENT: ``harness/decision_memory.py`` returns strings, and the only thing
keeping it away from the result is that nobody wired it up. This module makes
it structural and then pins it three ways (see
:func:`assert_memory_is_not_evidence` and the callers of
:func:`memory_sources_in`).

**2. Memory influence on a run's path must be REPORTED, or the ablation cannot
attribute anything.** If run A resolves a bug and run B does not *because B
recalled a memory*, then ``memory on/off`` measures the memory and the task
together. :func:`memory_influence` reads the ``decision_memory`` journal rows a
run already writes and turns them into an attributable receipt.

**3. The ``memory on/off`` arm must differ from baseline in MEMORY AND NOTHING
ELSE.** If it also changes tool exposure or prompt content, the ablation is
void -- it would be measuring the second change and calling it memory. There was
**no mechanism at all** for checking this: ``runtime/ablation_memplan.py``
derives both arms from one dict by copy-then-one-assignment and never verifies
the result. :func:`arm_config_diff` is that check, and it is a value rather than
an assertion so a driver can publish it.

**What this module does NOT do.** It does not read the store, does not call a
model, and does not touch ``execution/verify.py``'s mint. It is the layer that
answers "was this evidence, or was it something someone remembered?", and it
answers by refusing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "EVIDENCE_SOURCES",
    "IDENTITY_KEYS",
    "MEMORY_EVIDENCE_REFUSAL",
    "MEMORY_SOURCES",
    "PROSE_KEYS",
    "PROVENANCE_MARKERS",
    "SOURCE_KEYS",
    "UNRECOGNISED_SOURCE_MARKER",
    "VERIFICATION_BOOLEANS",
    "WEAK_MARKERS",
    "ArmComparison",
    "ArmDiff",
    "MemoryEvidenceRefused",
    "MemoryInfluence",
    "arm_config_diff",
    "assert_memory_is_not_evidence",
    "compare_arms",
    "memory_influence",
    "memory_sources_in",
]

#: The only things that may decide a verification boolean. **A memory is not in
#: this set**, and the set is exported so a consumer can branch on it rather
#: than re-derive it.
EVIDENCE_SOURCES: Tuple[str, ...] = (
    "verifier_run",
    "baseline_run",
    "regression_run",
)

#: Vocabulary for "where did this claim come from". Every member is checked
#: against :data:`EVIDENCE_SOURCES`; a member of neither set is a programming
#: error and raises, because an unrecognised source must not default to
#: "acceptable".
#:
#: **``memory`` and ``recall`` are members because those are the spellings THIS
#: TREE uses, measured 2026-10-04.** ``shared.security.UNTRUSTED_SOURCES`` already
#: names a source ``"memory"``, and a recalled row is a ``recall``. A marker
#: table that only knew its author's spelling finds nothing in the vocabulary the
#: rest of the product writes, and a sweep that finds nothing is a gate that has
#: never fired.
MEMORY_SOURCES: Tuple[str, ...] = (
    "decision_memory",
    "knowledge_recall",
    "memory",
    "memory_write",
    "prior_run_assertion",
    "prompt_text",
    "recall",
    "recalled",
)

#: Keys whose STRING value is a provenance claim. A memory named here is refused;
#: a member of :data:`EVIDENCE_SOURCES` is accepted; **anything else is a
#: programming error and raises** -- the same rule the explicit ``source=``
#: keyword obeys, applied to a source the caller attached to the evidence
#: instead.
#:
#: Wave 2 found the gap by measurement: ``{"source": "memory", "passed": True}``
#: was ACCEPTED, because the old sweep only matched marker names appearing as
#: KEYS or inside marker-bearing values, and ``"memory"`` as a source VALUE
#: matched nothing. The most natural spelling in this tree passed the gate built
#: to stop it.
SOURCE_KEYS: Tuple[str, ...] = (
    "source",
    "evidence_source",
    "origin",
    "provenance",
)

#: The marker recorded for a source string that is neither a memory nor a
#: verifier run. Distinct from every memory marker so the refusal can say which
#: of the two failures it was.
UNRECOGNISED_SOURCE_MARKER = "unrecognised_source"

#: Keys whose value is FREE PROSE and is therefore NOT swept for **weak** markers.
#:
#: **This is the exclusion that keeps the gate usable, and it is at the MARKER
#: level rather than the key level, because both of those need to be true at
#: once.** A pytest report that names ``memory/test_x.py`` is not a memory
#: claiming a test passed, and a gate that refuses it is a gate whose users learn
#: to route around it. A report that says ``per decision_memory, test_x.py
#: passes`` IS a memory asserting an outcome, and Wave 1 refused exactly that.
#: So: a *provenance claim* -- a source key, or a marker strong enough to be one
#: -- is refused; a weak mention in prose is not.
PROSE_KEYS: Tuple[str, ...] = (
    "raw_output",
    "stdout",
    "stderr",
    "text",
    "content",
    "message",
    "note",
    "notes",
    "summary",
    "reason",
    "detail",
    "excerpt",
    "transcript",
)

#: The markers that are NOT swept inside :data:`PROSE_KEYS`, because on their own
#: they are a path fragment or an English word rather than a provenance claim.
#:
#: **Deliberately an exclusion list and not a second marker table.** A new member
#: of :data:`MEMORY_SOURCES` is swept in prose by default, so forgetting to update
#: anything here FAILS CLOSED: the marker is caught. A second table would fail the
#: other way -- a marker added to one and not the other would be silently exempt.
WEAK_MARKERS: Tuple[str, ...] = ("memory", "recall", "recalled")

#: The markers that ARE swept everywhere, including inside prose.
PROVENANCE_MARKERS: Tuple[str, ...] = tuple(
    marker for marker in MEMORY_SOURCES if marker not in WEAK_MARKERS
)

#: The booleans on a ``VerificationResult``. Named so the refusal message can say
#: which field it is protecting rather than "verification".
VERIFICATION_BOOLEANS: Tuple[str, ...] = (
    "target_test_passed",
    "baseline_passed",
    "regression_passed",
    "flaky",
)


class MemoryEvidenceRefused(RuntimeError):
    """Raised when something that is not a verifier run is offered as evidence.

    Deliberately **not** a ``ValueError`` and **not** a ``PermissionError``: the
    fail-closed handlers in the gateway and the workspace absorb
    ``PermissionError``, and a memory offered as evidence would then be skipped
    and the run would continue on the next candidate -- which is the exact
    laundering this refuses.
    """


#: The refusal sentence, as a module constant so a UI and a test can render or
#: assert the SAME words.
MEMORY_EVIDENCE_REFUSAL = (
    "a memory is context, not evidence: only a verifier run may decide a "
    "verification boolean, and this claim's source is {source!r}"
)


def memory_sources_in(value: Any) -> List[str]:
    """Return every memory source reachable in ``value``, depth-first.

    Walks mappings, sequences and objects exposing ``to_dict()``, because the
    thing being checked is frequently a ``VerificationResult`` with additive
    instance attributes, and a check that only understood ``dict`` would find
    nothing there. Keys are matched case-insensitively and as substrings of the
    key, so ``knowledge_source``, ``memory_evidence`` and
    ``recalled_from_memory`` are all found -- the vocabulary in this tree is
    not one I own.

    **Three positions, and they are not interchangeable:**

    * a **KEY** containing a marker -- ``memory_evidence``, ``recalled_from``;
    * a **provenance VALUE** under a :data:`SOURCE_KEYS` key -- this is where
      Wave 2's measured hole was, and it is now classified by the SAME vocabulary
      rule the explicit ``source=`` keyword obeys (memory refuses, a verifier run
      accepts, anything else records
      :data:`UNRECOGNISED_SOURCE_MARKER`);
    * free prose -- swept for :data:`PROVENANCE_MARKERS` only, by
      :data:`WEAK_MARKERS`.

    The prose exclusion is **marker-level, not key-level**, and that distinction
    is the whole fix. Two claims must both hold: ``{"raw_output": "per
    decision_memory, tests/test_x.py passes"}`` is a memory asserting an outcome
    and MUST refuse, while ``{"raw_output": "tests/memory/test_x.py PASSED"}`` is
    a pytest report and must not. The first trips on the strong marker
    ``decision_memory``; the second only ever says the weak word ``memory``. So
    prose skips the weak spellings and keeps every provenance-grade marker.
    """
    found: List[str] = []
    seen: List[int] = []

    def _note(marker: str) -> None:
        if marker not in found:
            found.append(marker)

    def _classify_source(value: Any) -> None:
        """Apply the source vocabulary to a provenance value."""
        if not isinstance(value, str):
            return
        name = value.strip().lower()
        if not name:
            return
        for marker in MEMORY_SOURCES:
            if marker in name:
                _note(marker)
                return
        if name in EVIDENCE_SOURCES:
            return  # a verifier run: acceptable, and named as such
        _note(UNRECOGNISED_SOURCE_MARKER)

    def _walk(node: Any, depth: int) -> None:
        if depth > 6 or node is None:
            return
        identity = id(node)
        if identity in seen:
            return
        seen.append(identity)
        if isinstance(node, str):
            # A BARE string has no key to say whether it is a provenance claim
            # or prose, so it is swept: a caller handing a whole dict as a string
            # is rare, and a memory marker in a string is a signal worth having.
            text = node.strip().lower()
            for marker in MEMORY_SOURCES:
                if marker in text:
                    _note(marker)
            return
        if isinstance(node, Mapping):
            for key, item in node.items():
                lowered = str(key).strip().lower()
                for marker in MEMORY_SOURCES:
                    if marker in lowered:
                        _note(marker)
                        break
                if lowered in SOURCE_KEYS:
                    _classify_source(item)
                    continue  # a provenance value is classified, not swept as prose
                if lowered in PROSE_KEYS:
                    # NAMED exclusion, at the MARKER level: a weak mention in
                    # prose is a path fragment, but a provenance-grade marker
                    # here is still a memory asserting something.
                    if isinstance(item, str):
                        text = item.strip().lower()
                        for marker in PROVENANCE_MARKERS:
                            if marker in text:
                                _note(marker)
                    continue
                _walk(item, depth + 1)
            return
        if isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                _walk(item, depth + 1)
            return
        to_dict = getattr(node, "to_dict", None)
        if callable(to_dict):
            try:
                built = to_dict()
            except Exception:  # a receipt that cannot render is not evidence
                return
            if isinstance(built, Mapping):
                _walk(built, depth + 1)
            return
        for attribute in (*VERIFICATION_BOOLEANS, "verification_rung", "reports"):
            if hasattr(node, attribute):
                _walk(getattr(node, attribute), depth + 1)

    _walk(value, 0)
    return found


def assert_memory_is_not_evidence(
    evidence: Any,
    *,
    source: Optional[str] = None,
    boolean: Optional[str] = None,
) -> None:
    """Refuse a claim whose source is a memory. Returns nothing on success.

    **The rule, in one sentence: only a verifier run may decide a verification
    boolean, and a stored memory never can.** A memory row saying "these tests
    pass" is a record of a PAST run's claim; the tree in front of you may have
    changed since, and even if it has not, "a memory says so" is not a
    measurement.

    Four shapes are refused:

    * ``source`` naming a memory (the explicit declaration);
    * ``evidence`` carrying a memory marker anywhere in its structure (the
      structural sweep), which is what catches a caller who attached a memory
      without saying so;
    * a provenance VALUE under a :data:`SOURCE_KEYS` key that is neither a
      memory nor a member of :data:`EVIDENCE_SOURCES` -- the caller attached a
      provenance claim the module cannot classify, and an unclassified claim must
      not default to acceptable;
    * ``source`` that is neither a memory nor a member of
      :data:`EVIDENCE_SOURCES` -- an unrecognised source is a programming error
      and **raises rather than defaulting to acceptable**.

    ``boolean`` names the field being protected and appears only in the message.
    """
    if boolean is not None and boolean not in VERIFICATION_BOOLEANS:
        raise MemoryEvidenceRefused(
            f"{boolean!r} is not a verification boolean this module knows how to "
            f"protect; known: {list(VERIFICATION_BOOLEANS)}"
        )
    if source is not None:
        name = str(source).strip().lower()
        if name in MEMORY_SOURCES or any(m in name for m in MEMORY_SOURCES):
            raise MemoryEvidenceRefused(
                MEMORY_EVIDENCE_REFUSAL.format(source=source)
                + (f"; refusing to decide {boolean}" if boolean else "")
            )
        if name not in EVIDENCE_SOURCES:
            raise MemoryEvidenceRefused(
                f"evidence source {source!r} is neither a verifier run "
                f"({list(EVIDENCE_SOURCES)}) nor a known memory source "
                f"({list(MEMORY_SOURCES)}). An unrecognised source must not "
                "default to acceptable."
            )
    for marker in memory_sources_in(evidence):
        if marker == UNRECOGNISED_SOURCE_MARKER:
            raise MemoryEvidenceRefused(
                f"the evidence carries a provenance source that is neither a "
                f"verifier run ({list(EVIDENCE_SOURCES)}) nor a known memory "
                f"source ({list(MEMORY_SOURCES)}). An unrecognised source must "
                "not default to acceptable"
                + (f"; refusing to decide {boolean}" if boolean else "")
                + " (found on a source key in the evidence, not declared)"
            )
        raise MemoryEvidenceRefused(
            MEMORY_EVIDENCE_REFUSAL.format(source=marker)
            + (f"; refusing to decide {boolean}" if boolean else "")
            + " (found structurally in the evidence, not declared)"
        )


# ---------------------------------------------------------------------------
# 1. memory influence on a run's path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryInfluence:
    """How much memory touched a run, and whether the ablation can attribute it.

    ``influenced`` is the load-bearing field and it is a PROPERTY, not an input:
    a run that recalled nothing cannot claim influence, whatever a caller passes.
    """

    queried: bool = False
    matched: int = 0
    section_chars: int = 0
    queries: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    skipped_reason: str = ""
    source: str = "none"

    @property
    def influenced(self) -> bool:
        """True when a memory demonstrably reached this run's prompt."""
        return self.queried and self.matched > 0

    @property
    def attributable(self) -> bool:
        """True when the ablation can attribute a difference to memory.

        A run whose memory arm queried and matched nothing has no memory effect
        to attribute, so a difference between the arms is a difference in
        something else. Reporting that is the whole point: **an ablation that
        cannot say which arm actually received memory is not measuring memory.**
        """
        return self.influenced

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "queried": self.queried,
            "matched": self.matched,
            "section_chars": self.section_chars,
            "queries": list(self.queries),
            "errors": list(self.errors),
            "skipped_reason": self.skipped_reason,
            "source": self.source,
            "influenced": self.influenced,
            "attributable": self.attributable,
        }


def _iter_journal_rows(path: Path) -> List[Dict[str, Any]]:
    """Read a JSONL journal, skipping a torn or non-mapping line.

    A crash leaves a half-written final line; skipping it is the same tolerance
    the rest of the tree applies, and it is why this reader returns rows rather
    than raising.
    """
    rows: List[Dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a trace row's payload under either the strict or legacy shape."""
    for key in ("payload", "data"):
        value = row.get(key)
        if isinstance(value, Mapping):
            return value
    return row


def memory_influence(
    trace_path: Any,
    *,
    max_queries: int = 8,
) -> MemoryInfluence:
    """Report how much memory reached one run, from the rows it already wrote.

    Reads ``decision_memory`` rows out of a run's ``trace.jsonl``. **It reads
    what the run recorded and derives the verdict itself** -- a caller that
    supplied its own ``matched`` count would be describing the run rather than
    reporting it.

    A run with no ``decision_memory`` row at all reports
    ``queried=False, source="absent"``, which is an honest UNMEASURED and not a
    zero: the run may have had memory without writing a row. The OFF arm of the
    ablation writes ``{"matched": 0, "skipped": "plan_with_memory=False"}``,
    which resolves to ``queried=True, matched=0`` -- a measurement.
    """
    path = Path(trace_path)
    if not path.is_file():
        return MemoryInfluence(source="absent")
    rows = [
        _row_payload(row)
        for row in _iter_journal_rows(path)
        if str(row.get("kind") or row.get("event") or "") == "decision_memory"
    ]
    if not rows:
        return MemoryInfluence(source="absent")
    matched = 0
    chars = 0
    queries: List[str] = []
    errors: List[str] = []
    skipped = ""
    for row in rows:
        try:
            matched += int(row.get("matched") or 0)
        except (TypeError, ValueError):
            pass
        try:
            chars = max(chars, int(row.get("section_chars") or 0))
        except (TypeError, ValueError):
            pass
        query = str(row.get("query") or "")
        if query and query not in queries and len(queries) < max_queries:
            queries.append(query)
        error = str(row.get("error") or "")
        if error and error not in errors:
            errors.append(str(row.get("error")))
        if row.get("skipped") and not skipped:
            skipped = str(row.get("skipped"))
    return MemoryInfluence(
        queried=True,
        matched=matched,
        section_chars=chars,
        queries=tuple(queries),
        errors=tuple(errors),
        skipped_reason=skipped,
        source="trace",
    )


# ---------------------------------------------------------------------------
# 2. the arm diff: does the memory arm differ ONLY in memory?
# ---------------------------------------------------------------------------


#: Keys that identify a run rather than change its behaviour. A ``task_id`` or
#: ``run_id`` is expected to differ between arms -- two arms writing the same
#: directory would collide -- so they are excluded from the diff and named here
#: rather than silently filtered.
IDENTITY_KEYS: frozenset = frozenset({"task_id", "run_id", "attempt", "seed"})


@dataclass(frozen=True)
class ArmDiff:
    """What two ablation arms actually differ in.

    ``clean`` is the verdict: ``True`` when the two configs differ in the
    declared key(s) and in IDENTITY keys only. A ``clean`` arm diff is the
    precondition for attributing any measured difference to memory -- without it
    the ablation is measuring the second difference and calling it memory.

    ``differences`` is the full symmetric difference, so a reader can see WHICH
    keys moved without re-deriving it.
    """

    left: str
    right: str
    expected: Tuple[str, ...]
    differing: Tuple[str, ...] = ()
    unexpected: Tuple[str, ...] = ()
    identity_only: Tuple[str, ...] = ()
    clean: bool = False
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "left": self.left,
            "right": self.right,
            "expected": list(self.expected),
            "differing": list(self.differing),
            "unexpected": list(self.unexpected),
            "identity_only": list(self.identity_only),
            "clean": self.clean,
            "notes": list(self.notes),
        }

    def render(self) -> List[str]:
        """Render PLAIN lines naming the verdict and every differing key."""
        headline = (
            f"arm diff CLEAN: {self.left} and {self.right} differ only in "
            f"{list(self.expected)} (plus identity keys {list(self.identity_only)}), "
            "so a measured difference is attributable to that key"
            if self.clean
            else (
                f"arm diff DIRTY: {self.left} and {self.right} also differ in "
                f"{list(self.unexpected)} -- a measured difference CANNOT be "
                "attributed to the expected key alone; this ablation is void"
            )
        )
        lines = [headline]
        for note in self.notes:
            lines.append(f"  {note}")
        return lines


def arm_config_diff(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    expected: Sequence[str] = ("plan_with_memory",),
    left_name: str = "baseline",
    right_name: str = "arm",
    ignore: Iterable[str] = IDENTITY_KEYS,
) -> ArmDiff:
    """Diff two ablation arm configs and say whether the ablation is attributable.

    ``expected`` is the set of keys the arms are ALLOWED to differ in. Anything
    else that differs is named in ``unexpected`` and makes ``clean`` False.

    **The one-sided check matters as much as the two-sided one.** A key present
    in one arm and absent in the other is a difference, and it is the shape an
    accidental ``cfg.pop("tool_surface")`` takes. So the diff is over the
    symmetric difference of the key SETS first, then over the shared keys'
    values.

    ``ignore`` defaults to :data:`IDENTITY_KEYS` -- run identity, which is
    expected to differ and is not a behaviour change. It is a parameter so a
    driver can widen it deliberately, and every ignored key is reported in
    ``identity_only`` so a widened ignore list cannot hide a behaviour change.
    """
    ignored = frozenset(str(item) for item in ignore)
    left_keys = {str(key) for key in left}
    right_keys = {str(key) for key in right}
    only_left = sorted(left_keys - right_keys)
    only_right = sorted(right_keys - left_keys)

    differing: List[str] = []
    for key in sorted(left_keys & right_keys):
        if left.get(key) != right.get(key):
            differing.append(key)

    moved = set(only_left) | set(only_right) | set(differing)
    # `ignored` is subtracted from `unexpected`, not only reported. An ignore
    # entry that still made the diff DIRTY would mean a caller could never widen
    # the ignore list past run identity -- which is correct: widening it to cover
    # a behaviour key would then be refused rather than silently accepted.
    unexpected = sorted(
        key for key in moved if key not in set(expected) and key not in ignored
    )
    identity_only = sorted(key for key in moved if key in ignored)
    notes: List[str] = []
    if only_left:
        notes.append(f"only in {left_name}: {only_left}")
    if only_right:
        notes.append(f"only in {right_name}: {only_right}")
    for key in differing:
        notes.append(f"{key}: {left.get(key)!r} -> {right.get(key)!r}")
    missing = [key for key in expected if key not in moved]
    if missing:
        notes.append(
            f"the expected key(s) {missing} did NOT differ between the arms, so "
            "this arm pair cannot measure them at all"
        )
    clean = not unexpected and not missing
    return ArmDiff(
        left=left_name,
        right=right_name,
        expected=tuple(str(key) for key in expected),
        differing=tuple(sorted(key for key in differing if key not in ignored)),
        unexpected=tuple(unexpected),
        identity_only=tuple(identity_only),
        clean=clean,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class ArmComparison:
    """Two arms' memory influence plus the config diff that licenses attribution."""

    left: MemoryInfluence
    right: MemoryInfluence
    diff: ArmDiff
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def attributable(self) -> bool:
        """True only when BOTH the configs are clean AND the arms really differed.

        A clean config diff with two arms that both received no memory measures
        nothing, and reporting it as an attributable comparison is the failure
        this type exists to prevent.
        """
        return self.diff.clean and self.left.influenced != self.right.influenced

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "diff": self.diff.to_dict(),
            "attributable": self.attributable,
            "notes": list(self.notes),
        }

    def render(self) -> List[str]:
        """Render PLAIN lines for an ablation report."""
        lines = [f"memory arm comparison: attributable={self.attributable}"]
        lines.append(
            f"  {self.diff.left}: influenced={self.left.influenced} "
            f"matched={self.left.matched} skipped={self.left.skipped_reason or '-'}"
        )
        lines.append(
            f"  {self.diff.right}: influenced={self.right.influenced} "
            f"matched={self.right.matched} skipped={self.right.skipped_reason or '-'}"
        )
        lines.extend(f"  {line}" for line in self.diff.render())
        for note in self.notes:
            lines.append(f"  {note}")
        return lines


def compare_arms(
    left_trace: Any,
    right_trace: Any,
    left_config: Mapping[str, Any],
    right_config: Mapping[str, Any],
    *,
    expected: Sequence[str] = ("plan_with_memory",),
    left_name: str = "off",
    right_name: str = "on",
) -> ArmComparison:
    """Read both arms' memory influence and the config diff, and combine them.

    This is the single call a driver needs to publish an honest ablation claim.
    It computes :attr:`ArmComparison.attributable` rather than accepting it, and
    adds a note when the configs are clean but neither arm received memory --
    because "the arms differ only in memory and neither arm used it" is a
    vacuous ablation and must not read as a null result.
    """
    left = memory_influence(left_trace)
    right = memory_influence(right_trace)
    diff = arm_config_diff(
        left_config,
        right_config,
        expected=expected,
        left_name=left_name,
        right_name=right_name,
    )
    notes: List[str] = []
    if diff.clean and not left.influenced and not right.influenced:
        notes.append(
            "VACUOUS: the arms differ only in the expected key and NEITHER arm "
            "received a memory, so a null result measures nothing about memory"
        )
    if left.source == "absent" or right.source == "absent":
        notes.append(
            "at least one arm wrote no decision_memory row, so its memory "
            "exposure is UNMEASURED rather than zero"
        )
    return ArmComparison(left=left, right=right, diff=diff, notes=tuple(notes))
