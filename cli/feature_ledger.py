"""The feature ledger as the honest progress view.

`docs/feature-matrix.md` is this repository's capability claim of record. Every
row already carries a status from ``scripts.docs_truth.CLAIM_STATUSES`` and at
least one evidence reference, and `python -m scripts.docs_truth` fails a build
when either is missing. **This module does not own that table and does not keep
a second one.** It reads it, checks each reference against the working tree, and
renders what it found.

Why that matters for Phase 4: the corpus's worst failure mode is an agent
reporting "all features complete" when they are not, and the defence is a ledger
a human can read. A progress bar hides which features are done; a list does
not. So this is a list, every row carries its verification state, and the one
render rule that cannot be broken is:

    **A feature with no evidence cannot render as passing.**

P4/W2 strengthened that rule, because Wave 1 could only check the word
"EVIDENCE" and the brief's assertion is stronger than that::

    for every feature marked passing:
        assert feature.evidence exists
        assert feature.evidence was run (not reused-from-unchanged-state, not declared)
        assert feature.evidence corresponds to THIS feature

All three clauses are checked at the RENDER layer by
:func:`assert_no_unevidenced_passing`, and all three are now EXPRESSIBLE: an
:class:`EvidenceRef` carries a verification ``method``, a recorded ``outcome``
from :data:`EVIDENCE_OUTCOMES`, and the ``subject`` the run verified. Before
this change the third clause could not be written down at all.

**The consequence is stated rather than softened.** ``docs/feature-matrix.md``
is this repository's capability claim of record: it cites paths and declares
statuses, and it declares no RUNS. With no verification receipt present - and
**nothing in this repository writes one yet**, see
:data:`VERIFICATION_RECEIPT_NAME` - every row of the real ledger renders as NOT
verified and states why. That is not a regression; it is the finding. A row that
cites ``tests/test_x.py`` has said where to look, not that anything was executed
and that it passed.

    python -m cli.feature_ledger [--repo DIR] [--json] [--feature NAME]
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cli.ui import sanitize_text

__all__ = [
    "BLOCKED_PREFIX",
    "DEFAULT_LEDGER_RELPATH",
    "EVIDENCE_ANNOTATION_FIELDS",
    "EVIDENCE_OUTCOMES",
    "FEATURE_STATES",
    "NO_RUN_REASON",
    "VERIFICATION_RECEIPT_NAME",
    "Feature",
    "FeatureLedger",
    "LedgerContractError",
    "apply_verification",
    "assert_no_unevidenced_passing",
    "coverage_summary",
    "current_feature_lines",
    "feature_detail_lines",
    "feature_lines",
    "load_ledger",
    "no_unevidenced_passing",
    "parse_feature_matrix",
    "read_verification_receipts",
    "reason_histogram",
    "render_coverage_lines",
    "summary_line",
    "wish_lines",
]

#: The ledger of record, relative to the repository root.
DEFAULT_LEDGER_RELPATH = "docs/feature-matrix.md"

#: The closed vocabulary of a verification OUTCOME - what actually happened when
#: the evidence was exercised.
#:
#: Added in P4/W2 because Wave 1 could only answer "does the cited file exist",
#: which is a question about the FILESYSTEM and not about the feature. The
#: brief's assertion is "``feature.evidence`` was run (not reused-from-unchanged-
#: state, not declared)", and ``exists`` cannot express any of those three
#: exclusions. This vocabulary can, and an outcome nobody declared is ``""``,
#: which is NOT a member and therefore cannot promote anything.
#:
#: ``passed`` is the ONLY member that may render as passing. The other five exist
#: so that a row which is not passing can say WHY in a word rather than by
#: omission: ``failed`` is a check that failed, ``flaky`` is a check that
#: disagreed with itself, ``timed_out`` is a check that could not decide,
#: ``reused`` is a receipt from a state that never changed, and ``declared`` is
#: an assertion with no run behind it - a wish.
EVIDENCE_OUTCOMES: Tuple[str, ...] = (
    "passed",
    "failed",
    "flaky",
    "timed_out",
    "reused",
    "declared",
)

#: The annotation fields an evidence cell may declare, in the order they are
#: written inside the brackets. Position is the contract: ``[passed ; pytest ;
#: 2026-10-06 ; the alpha thing]`` is outcome, method, when, and which feature
#: the run verified.
#:
#: The separator is a SEMICOLON and that is load-bearing, not cosmetic. The
#: first draft used ``|``, which is the markdown table cell separator: the
#: annotation was split off the row before this module ever saw it, and the
#: parsed "reference" came back as ``ests/test_alpha.py [mostly fine`` - a
#: reference that resolves to nothing, for a row that looked annotated. A
#: format that collides with its own container's grammar cannot be diagnosed
#: from the output, which is why the collision is recorded rather than merely
#: fixed.
EVIDENCE_ANNOTATION_FIELDS: Tuple[str, ...] = ("outcome", "method", "ran_at", "subject")

#: What separates the annotation's fields. Declared so the parser and its
#: docstring cannot drift apart.
EVIDENCE_ANNOTATION_SEPARATOR = ";"

#: Why a row that cites evidence but records no run is not a passing row. Stated
#: once so the renderer, the detail surface and the coverage summary all say the
#: same words, and so a future edit cannot quietly weaken one of them.
NO_RUN_REASON = (
    "the row cites a reference but records NO RUN of it, so nothing verified "
    "this feature: a cited file is a wish, not a receipt"
)

#: Why a row whose run exists but was reused is not a passing row.
REUSED_REASON = (
    "the recorded run was REUSED from an unchanged state, so it is a receipt "
    "from a moment when nothing changed - not evidence that this feature works"
)

#: Why a row whose run verified a different feature is not a passing row.
WRONG_SUBJECT_REASON = (
    "the recorded run names a DIFFERENT feature as its subject, so it cannot be "
    "this row's evidence however well that other feature passes"
)

#: Why a row that recorded a run but declared no METHOD is not a passing row.
#:
#: Separate from :data:`NO_RUN_REASON` on purpose. The first draft returned the
#: no-run sentence for this case, which says the opposite of what happened - a
#: run WAS recorded and it DID pass - so the receipt described a missing receipt
#: for a row that had one. "A wish" means no method; "a cited file" means no run.
NO_METHOD_REASON = (
    "no verification METHOD is declared for this row. A recorded run with no "
    "declared way to reproduce it is not evidence anyone can check, so the row "
    "is a wish rather than a receipt"
)

#: A status is "blocked" when it starts with this. The exact spellings are NOT
#: re-declared here - ``scripts.docs_truth.CLAIM_STATUSES`` is the authority and
#: ``assert_statuses_come_from_the_authority`` pins that this module cannot grow
#: its own vocabulary underneath it.
BLOCKED_PREFIX = "Blocked"

#: The closed vocabulary this module renders. Four states, because a row that
#: says "implemented" is only meaningful next to the row that says "nobody ran
#: the check" - and a two-state vocabulary cannot say that.
FEATURE_STATES = ("passing", "failing", "unverified", "blocked")


class LedgerContractError(RuntimeError):
    """Raised when a render would claim more than the evidence supports."""


def _declared_statuses() -> frozenset:
    """Return the claim-status vocabulary from the ONE authority.

    Imported lazily and defensively: ``scripts/`` is not a package this CLI
    depends on for anything else, and a missing authority must degrade to a
    refusal rather than a second copy of the vocabulary.
    """
    try:
        from scripts.docs_truth import CLAIM_STATUSES

        return frozenset(CLAIM_STATUSES)
    except Exception:
        return frozenset()


def assert_statuses_come_from_the_authority() -> None:
    """Refuse to run against a forked status vocabulary.

    A renderer that keeps its own status list is a second answer to "what does
    a status mean", which is the drift class this repository records paying for
    in four places. The gate is a published contract, not an internal detail.
    """
    if not _declared_statuses():
        raise LedgerContractError(
            "the claim-status authority (scripts.docs_truth.CLAIM_STATUSES) is "
            "unavailable, so no row may be classified as passing or blocked"
        )


@dataclass(frozen=True)
class EvidenceRef:
    """One evidence reference: what it is, and what happened when it was run.

    Wave 1's version carried three fields and answered one question - does this
    path resolve in this checkout. That is a question about the FILESYSTEM, and
    a row citing a test file rendered ``[pass]`` on the strength of the file's
    existence alone, with nothing having been executed. The four fields added
    here are what the brief's three assertions need and ``exists`` cannot carry:

    ``method``
        HOW this feature is verified. Empty means no verification method is
        declared, and the brief's own words for that row are "a wish".
    ``outcome``
        WHAT happened, from :data:`EVIDENCE_OUTCOMES`. Empty means no run is
        recorded at all, which is the shipped state of every row in
        ``docs/feature-matrix.md`` because that file is a capability claim of
        record and declares no runs.
    ``subject``
        WHICH feature the run verified. Empty means the run does not say, and a
        run that does not name its subject is not evidence for this row - it is
        evidence for an unnamed one.
    ``ran_at`` / ``detail``
        WHEN, and the run's own words. Informational, and deliberately not
        load-bearing: a receipt with no timestamp is still a receipt, and a
        receipt from an hour ago is not thereby not one.
    """

    reference: str
    exists: bool
    kind: str = "unknown"  # test | script | report | artifact | unknown
    method: str = ""
    outcome: str = ""
    subject: str = ""
    ran_at: str = ""
    detail: str = ""

    @property
    def run_recorded(self) -> bool:
        """Return whether anything recorded what happened to this reference."""
        return bool(self.outcome)

    @property
    def outcome_known(self) -> bool:
        """Return whether the outcome is a member of the closed vocabulary.

        An unrecognised word is NOT an outcome. Coercing ``"mostly fine"`` into
        a pass would be the exact substitution this module exists to refuse, so
        the parser drops it and the row falls back to the no-run reason.
        """
        return self.outcome in EVIDENCE_OUTCOMES

    @property
    def reused(self) -> bool:
        """Return whether the run was reused from an unchanged state."""
        return self.outcome == "reused"

    @property
    def flaky(self) -> bool:
        """Return whether the run disagreed with itself."""
        return self.outcome == "flaky"

    @property
    def timed_out(self) -> bool:
        """Return whether the run could not decide."""
        return self.outcome == "timed_out"

    def line(self) -> str:
        """Return a plain, sanitised, display line for this reference."""
        state = "present" if self.exists else "MISSING from this checkout"
        out = [f"    - {self.reference} [{self.kind}] ({state})"]
        if self.method:
            out.append(f"      verification method: {self.method}")
        if self.outcome_known:
            out.append(f"      recorded run: {self.outcome}")
        else:
            out.append("      recorded run: NONE - nothing was executed and recorded")
        if self.subject:
            out.append(f"      run subject: {self.subject}")
        elif self.run_recorded:
            out.append(
                "      run subject: UNSTATED - a run that does not name its "
                "feature cannot be this row's evidence"
            )
        if self.ran_at:
            out.append(f"      run at: {self.ran_at}")
        if self.detail:
            out.append(f"      note: {self.detail}")
        return "\n".join(out)


@dataclass(frozen=True)
class Feature:
    """One ledger row, with its verification state derived from its evidence.

    ``declared_status`` is the ledger's own word. ``state`` is what this module
    is willing to render, and it is deliberately NARROWER: a row that says
    ``Implemented`` whose evidence no longer exists on disk is ``unverified``,
    because "the matrix says it is implemented" and "the thing that would fail
    if it stopped being true is present" are two different facts.

    P4/W2 narrowed it further, and the narrowing is the round. A row is
    ``passing`` only when all of the following hold, and each clause below is
    one of the brief's three assertions:

    1. the ledger declares it implemented;
    2. the row cites at least one evidence reference;
    3. that reference resolves in this checkout;
    4. the row declares a verification METHOD (a wish is not a receipt);
    5. a run is RECORDED and its outcome is ``passed``;
    6. that run was not REUSED from an unchanged state;
    7. that run names THIS feature as its subject.
    """

    name: str
    declared_status: str
    notes: str = ""
    evidence: Tuple[EvidenceRef, ...] = ()
    line_number: int = 0

    # -- derived facts ---------------------------------------------------
    @property
    def blocked(self) -> bool:
        """Return whether the ledger itself says this is blocked."""
        return self.declared_status.startswith(BLOCKED_PREFIX)

    @property
    def evidence_present(self) -> bool:
        """Return whether at least one reference resolves in this checkout."""
        return any(item.exists for item in self.evidence)

    @property
    def verification_ran(self) -> bool:
        """Return whether a verification is OBSERVABLE here.

        Deliberately NOT "the ledger claims it". A claim with a reference that
        no longer exists has no observable verification behind it, and reporting
        it as one is the quiet-zero shape `phases/DOCTRINE.md` §1 forbids.

        Deliberately also NOT "a run was recorded": this property is about what
        can be SEEN on disk, and :attr:`run_recorded` is about what the row
        CLAIMS happened. Wave 1 conflated the two by using this one to decide
        ``passing``, which is how a file that merely exists became a pass.
        """
        return bool(self.evidence) and self.evidence_present

    @property
    def evidenced(self) -> bool:
        """Return whether the row carries evidence AT ALL."""
        return bool(self.evidence)

    @property
    def verification_method(self) -> str:
        """Return the declared verification method, or ``""`` when none is."""
        for item in self.evidence:
            if item.method:
                return item.method
        return ""

    @property
    def run_recorded(self) -> bool:
        """Return whether the row records ANY outcome for any reference."""
        return any(item.outcome_known for item in self.evidence)

    @property
    def recorded_outcomes(self) -> Tuple[str, ...]:
        """Return every recorded outcome, in declaration order."""
        return tuple(item.outcome for item in self.evidence if item.outcome_known)

    @property
    def run_passed(self) -> bool:
        """Return whether a recorded run actually passed."""
        return any(item.outcome == "passed" for item in self.evidence)

    @property
    def run_reused(self) -> bool:
        """Return whether the only recorded runs were reused from no change."""
        recorded = [item for item in self.evidence if item.outcome_known]
        return bool(recorded) and all(item.reused for item in recorded)

    @property
    def run_flaky(self) -> bool:
        """Return whether any recorded run disagreed with itself."""
        return any(item.flaky for item in self.evidence)

    @property
    def run_timed_out(self) -> bool:
        """Return whether any recorded run could not decide."""
        return any(item.timed_out for item in self.evidence)

    @property
    def run_scoped_here(self) -> bool:
        """Return whether a passing run names THIS row's feature as subject.

        A run that names no subject does NOT pass this test. The brief's third
        assertion is that the evidence "corresponds to THIS feature", and a run
        whose subject is unknown cannot be shown to correspond to anything.
        Reading the absence as "presumably it was this one" is precisely the
        substitution this module refuses elsewhere.
        """
        needle = self.name.strip().lower()
        if not needle:
            return False
        for item in self.evidence:
            if item.outcome == "passed" and item.subject.strip().lower() == needle:
                return True
        return False

    @property
    def passing(self) -> bool:
        """Return whether this row may be rendered as passing.

        Seven conditions, all required; see the class docstring for the list and
        for which clause of the brief each one discharges.
        """
        return (
            not self.blocked
            and self.declared_status == "Implemented"
            and self.evidenced
            and self.verification_ran
            and bool(self.verification_method)
            and self.run_passed
            and not self.run_reused
            and self.run_scoped_here
        )

    @property
    def not_passing_reason(self) -> str:
        """Return WHY this row is not passing, in one sentence, or ``""``.

        A row that is not passing and gives no reason is indistinguishable from
        a row nobody looked at, which is the absence-of-affordance failure this
        module's own docstring names. The order is most-specific first, because
        the most specific reason is the most actionable one.
        """
        if self.blocked:
            return (
                f"the ledger declares this row blocked ({self.declared_status}); a "
                "blocked row is not a passing row"
            )
        if self.run_timed_out:
            return (
                "the recorded verification TIMED OUT, so it could not decide "
                "whether this feature works; an undecided check is not a pass"
            )
        if self.run_reused:
            return REUSED_REASON
        if self.run_flaky:
            return (
                "the recorded verification was FLAKY - it disagreed with itself, "
                "so this feature is not known to work and is not known broken"
            )
        if self.run_recorded and not self.run_passed:
            return (
                "the recorded verification did not pass ("
                + ", ".join(sorted(set(self.recorded_outcomes)))
                + "), so this row is not passing"
            )
        if self.evidenced and not self.run_scoped_here and self.run_passed:
            return WRONG_SUBJECT_REASON
        if not self.evidenced:
            return "no evidence is cited for this row, so it cannot be verified"
        if not self.verification_ran:
            return (
                "the cited evidence does not resolve in this checkout, so no "
                "verification is observable here"
            )
        if self.run_recorded and self.run_passed and not self.verification_method:
            return NO_METHOD_REASON
        if not self.verification_method:
            return NO_RUN_REASON
        if not self.run_recorded:
            return NO_RUN_REASON
        return NO_RUN_REASON

    @property
    def state(self) -> str:
        """Return the render state, from the closed :data:`FEATURE_STATES`.

        P4/W2 remapped ``failing``, and the reason is a lie in the other
        direction. Wave 1 defined ``failing`` as "evidenced and not passing", so a
        row that had never been run rendered ``[FAIL]`` - which asserts that a
        check ran and failed, when in truth nothing ran at all. ``failing`` now
        means a run HAPPENED and did not pass; a row that cites evidence nobody
        exercised is ``unverified``, which is the word for "we do not know".

        ``timed_out`` maps to ``blocked`` rather than to ``failing``: the brief's
        words are "a feature whose verification timed out -> blocked, not
        passing", and the reason is that a check which could not decide says
        nothing about the feature, whereas ``failing`` claims it does.
        """
        if self.blocked or self.run_timed_out:
            return "blocked"
        if self.passing:
            return "passing"
        if self.run_recorded:
            return "failing"
        return "unverified"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection (additive keys only)."""
        return {
            "name": self.name,
            "declared_status": self.declared_status,
            "notes": self.notes,
            "line_number": int(self.line_number),
            "evidence": [
                {
                    "reference": item.reference,
                    "exists": item.exists,
                    "kind": item.kind,
                    "method": item.method,
                    "outcome": item.outcome,
                    "outcome_known": item.outcome_known,
                    "subject": item.subject,
                    "ran_at": item.ran_at,
                    "detail": item.detail,
                    "run_recorded": item.run_recorded,
                }
                for item in self.evidence
            ],
            "evidenced": self.evidenced,
            "verification_ran": self.verification_ran,
            "verification_method": self.verification_method,
            "run_recorded": self.run_recorded,
            "recorded_outcomes": list(self.recorded_outcomes),
            "run_passed": self.run_passed,
            "run_reused": self.run_reused,
            "run_flaky": self.run_flaky,
            "run_timed_out": self.run_timed_out,
            "run_scoped_here": self.run_scoped_here,
            "passing": self.passing,
            "state": self.state,
            "not_passing_reason": self.not_passing_reason,
        }


# ---------------------------------------------------------------------------
# Parsing - the ledger of record, never a second table
# ---------------------------------------------------------------------------


def _split_row(line: str) -> List[str]:
    """Split one markdown table row into cells, honouring ``\\|`` escapes."""
    cells: List[str] = []
    current: List[str] = []
    escaped = False
    for char in line.strip():
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "|":
            cells.append("".join(current))
            current = []
            continue
        current.append(char)
    cells.append("".join(current))
    # A markdown row's leading and trailing pipes produce an empty cell each.
    # Dropping exactly those two (not every empty cell) is what keeps a row
    # whose NOTES cell is genuinely empty from shifting its columns.
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


def _evidence_kind(reference: str) -> str:
    """Classify an evidence reference by its SHAPE, not by hand."""
    text = reference.strip()
    if text.startswith("tests/") and text.endswith(".py"):
        return "test"
    if text.startswith("scripts/"):
        return "script"
    if text.startswith("logs/"):
        return "report"
    return "artifact"


def _split_evidence(cell: str) -> List[EvidenceRef]:
    """Return the individual references inside one evidence cell.

    A cell is one or more comma-separated references, and a reference may carry
    a bracketed annotation of the form::

        `tests/test_alpha.py` [passed ; pytest ; 2026-10-06 ; the alpha thing]

    The four fields are positional, in :data:`EVIDENCE_ANNOTATION_FIELDS` order:
    outcome, method, when, and which feature the run verified, separated by
    :data:`EVIDENCE_ANNOTATION_SEPARATOR` - a semicolon, because ``|`` is the
    markdown cell separator and the first draft of this format used it. An ABSENT
    annotation leaves every run field empty, which is what
    ``docs/feature-matrix.md`` does today on every row - and an empty outcome is
    not in :data:`EVIDENCE_OUTCOMES`, so no row can be promoted by declining to
    say what happened.

    A reference containing a ``,`` is not representable, because the comma is the
    reference separator; a Markdown table cell cannot contain one anyway without
    being escaped, and a ledger that needs it should cite two references.

    An unrecognised outcome word is DROPPED rather than coerced. ``"mostly
    fine"`` must not become a pass, and the row falls back to the no-run reason
    so a reader is told the run's verdict was not understood rather than being
    shown a green row.
    """
    text = str(cell or "").strip().strip("`")
    if not text:
        return []
    out: List[EvidenceRef] = []
    for chunk in text.split(","):
        raw = chunk.strip().strip("`").strip()
        if not raw:
            continue
        reference, annotation = _split_annotation(raw)
        # Strip the CODE-SPAN backticks again. ``_split_annotation`` returns the
        # text before the bracket, which for `` `tests/x.py` [passed ; pytest] ``
        # is `` `tests/x.py` `` - so the closing backtick survives and the
        # reference becomes ``tests/x.py` ``, which resolves to nothing. A
        # reference that cannot be opened is a reference nobody can check, and
        # this is why the existence check is compared against the STRIPPED form.
        reference = str(reference or "").strip().strip("`").strip()
        fields = _parse_annotation(annotation)
        outcome = fields["outcome"]
        if outcome and outcome not in EVIDENCE_OUTCOMES:
            fields["outcome"] = ""
            fields["detail"] = (
                f"the run was annotated {outcome!r}, which is not a member of "
                f"{EVIDENCE_OUTCOMES}; it is treated as NO recorded run"
            )
        out.append(
            EvidenceRef(
                reference=reference,
                exists=False,  # replaced by the caller, which knows the root
                kind=_evidence_kind(reference),
                method=fields["method"],
                outcome=fields["outcome"],
                subject=fields["subject"],
                ran_at=fields["ran_at"],
                detail=fields["detail"],
            )
        )
    return out


def _split_annotation(raw: str) -> Tuple[str, str]:
    """Split one reference from its trailing ``[...]`` annotation."""
    if not raw.endswith("]"):
        return raw, ""
    start = raw.find("[")
    if start < 0:
        return raw, ""
    return raw[:start].strip(), raw[start + 1 : -1]


def _parse_annotation(annotation: str) -> Dict[str, str]:
    """Return the four declared annotation fields, empty where unstated."""
    fields: Dict[str, str] = {name: "" for name in EVIDENCE_ANNOTATION_FIELDS}
    fields["detail"] = ""
    parts = [
        part.strip()
        for part in str(annotation or "").split(EVIDENCE_ANNOTATION_SEPARATOR)
    ]
    # strict=False on purpose: a SHORT annotation is the normal case (an author
    # may record only an outcome), and the length check below is what decides
    # what an absent field means. strict=True would raise on the common input
    # to make a rule this function already implements.
    for name, value in zip(EVIDENCE_ANNOTATION_FIELDS, parts, strict=False):
        fields[name] = value
    if len(parts) > len(EVIDENCE_ANNOTATION_FIELDS):
        fields["detail"] = EVIDENCE_ANNOTATION_SEPARATOR.join(
            parts[len(EVIDENCE_ANNOTATION_FIELDS) :]
        )
    return fields


def parse_feature_matrix(
    text: str, *, repo_root: Optional[Path] = None
) -> List[Feature]:
    """Parse capability rows out of the ledger of record.

    Only the ``## User-facing capabilities`` table is read. The other two tables
    in that file are an evidence/gate narrative and a prose limitation list, and
    folding either into "features" would invent rows nobody declared.

    ``repo_root`` enables the existence check. With no root, every reference is
    reported ``exists=False`` and therefore no row can render as passing - the
    fail-closed direction, and the reason a caller cannot get a green list by
    forgetting to pass a root.
    """
    rows: List[Feature] = []
    in_table = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not in_table:
            if line.strip().lower().startswith("## "):
                in_table = (
                    "user-facing capabilities" in line.strip().lower()
                )
            continue
        stripped = line.strip()
        if not stripped.startswith("|"):
            if stripped:
                break  # the table ended
            continue
        cells = _split_row(stripped)
        if len(cells) < 4:
            continue
        name = cells[0]
        if not name or set(name) <= set("-: "):
            continue
        status = cells[1]
        # The header row is skipped by its own header word, not by position, so
        # adding a column above it cannot make a real row invisible.
        if name.lower() == "capability" or status.lower() == "status":
            continue
        refs: List[EvidenceRef] = []
        for reference in _split_evidence(cells[3]):
            exists = False
            if repo_root is not None:
                exists = (Path(repo_root) / reference.reference).exists()
            refs.append(
                EvidenceRef(
                    reference=reference.reference,
                    exists=bool(exists),
                    kind=reference.kind,
                    method=reference.method,
                    outcome=reference.outcome,
                    subject=reference.subject,
                    ran_at=reference.ran_at,
                    detail=reference.detail,
                )
            )
        rows.append(
            Feature(
                name=name,
                declared_status=status,
                notes=cells[2],
                evidence=tuple(refs),
                line_number=number,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


@dataclass
class FeatureLedger:
    """Every declared feature, its derived state, and the current session's."""

    features: Tuple[Feature, ...] = ()
    source: str = ""
    current_feature: str = ""
    source_available: bool = True

    def by_name(self, name: str) -> Optional[Feature]:
        """Return one feature by (case-insensitive, substring-tolerant) name."""
        needle = str(name or "").strip().lower()
        if not needle:
            return None
        for feature in self.features:
            if feature.name.strip().lower() == needle:
                return feature
        for feature in self.features:
            if needle in feature.name.strip().lower():
                return feature
        return None

    def counts(self) -> Dict[str, int]:
        """Return per-state counts. Every feature is counted exactly once."""
        counts = {state: 0 for state in FEATURE_STATES}
        for feature in self.features:
            counts[feature.state] = counts.get(feature.state, 0) + 1
        counts["total"] = len(self.features)
        return counts

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of the whole ledger."""
        return {
            "source": self.source,
            "source_available": self.source_available,
            "current_feature": self.current_feature,
            "counts": self.counts(),
            "features": [feature.to_dict() for feature in self.features],
        }


#: The verification receipt file a producer appends to, read-only, under the
#: artifact root. One JSON object per line, keyed by the evidence reference it
#: speaks for: ``{"reference": "tests/x.py", "outcome": "passed", "method":
#: "pytest", "ran_at": "...", "subject": "the alpha thing"}``.
#:
#: **Nothing in this repository writes this file today**, which is stated in the
#: module docstring rather than left for a reader to discover: it is the seam a
#: verifier needs, not a claim that receipts exist. Its ABSENCE is what makes
#: every row of the shipped ledger unverified, and that is the honest state.
VERIFICATION_RECEIPT_NAME = "feature-verification.jsonl"


def read_verification_receipts(log_root: Any) -> Dict[str, Dict[str, str]]:
    """Return verification receipts from an artifact root, keyed by reference.

    Read-only and fail-closed: an unreadable, torn or malformed receipt file
    contributes NOTHING rather than a default. A receipt that cannot be read is
    the absence of a receipt, and treating it as "passed" because the file
    exists would reintroduce the exact defect this module exists to prevent.

    The last receipt for a reference wins, matching the append-only convention
    every other journal in this repository uses.
    """
    out: Dict[str, Dict[str, str]] = {}
    if log_root is None:
        return out
    path = Path(log_root) / VERIFICATION_RECEIPT_NAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue  # a torn tail is skipped, never half-read
        if not isinstance(parsed, Mapping):
            continue
        reference = str(parsed.get("reference") or "").strip()
        if not reference:
            continue
        record: Dict[str, str] = {}
        for name in ("outcome", "method", "ran_at", "subject", "detail"):
            record[name] = str(parsed.get(name) or "").strip()
        if record["outcome"] and record["outcome"] not in EVIDENCE_OUTCOMES:
            # An unrecognised verdict is dropped, not coerced. See
            # ``_split_evidence`` for why this direction matters.
            record["outcome"] = ""
        out[reference] = record
    return out


def apply_verification(
    rows: Sequence[Feature], receipts: Optional[Mapping[str, Mapping[str, str]]]
) -> List[Feature]:
    """Return the rows with any recorded run overlaid from ``receipts``.

    ``receipts`` is keyed by evidence reference, which is what makes it usable
    by more than one producer: a ledger that annotates its own cell and a
    verifier that appends a receipt reach the same rows by the same key.

    A reference with no receipt keeps whatever the ledger cell declared. A
    receipt never ERASES a declared run by omission - silence is not a retraction
    - but an explicit ``outcome`` on the receipt wins, because the receipt is
    the newer observation.
    """
    if not receipts:
        return list(rows)
    out: List[Feature] = []
    for row in rows:
        overlay: List[EvidenceRef] = []
        changed = False
        for item in row.evidence:
            receipt = receipts.get(item.reference)
            if not isinstance(receipt, Mapping):
                overlay.append(item)
                continue
            changed = True
            overlay.append(
                EvidenceRef(
                    reference=item.reference,
                    exists=item.exists,
                    kind=item.kind,
                    method=str(receipt.get("method") or item.method),
                    outcome=str(receipt.get("outcome") or item.outcome),
                    subject=str(receipt.get("subject") or item.subject),
                    ran_at=str(receipt.get("ran_at") or item.ran_at),
                    detail=str(receipt.get("detail") or item.detail),
                )
            )
        out.append(
            Feature(
                name=row.name,
                declared_status=row.declared_status,
                notes=row.notes,
                evidence=tuple(overlay),
                line_number=row.line_number,
            )
            if changed
            else row
        )
    return out


def load_ledger(
    repo_root: Any = None,
    *,
    current_feature: str = "",
    relpath: str = "",
    verification: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> FeatureLedger:
    """Load the ledger of record and derive every row's verification state.

    Never raises: an absent ledger is a ledger with **zero** features and
    ``source_available=False``, which renders a refusal to answer rather than
    an empty green list. A missing ledger must never read as "nothing is
    outstanding".

    ``verification`` is the recorded-run overlay. With no overlay - which is the
    shipped state, because nothing writes the receipt file - every row that
    cites a reference renders as NOT verified and says why. That is the whole
    point of the change: the ledger of record is a capability claim, and a claim
    is not a receipt.
    """
    root = Path(repo_root or Path.cwd())
    target = root / (relpath or DEFAULT_LEDGER_RELPATH)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return FeatureLedger(
            features=(),
            source=str(target),
            current_feature=str(current_feature or ""),
            source_available=False,
        )
    rows = parse_feature_matrix(text, repo_root=root)
    return FeatureLedger(
        features=tuple(apply_verification(rows, verification)),
        source=str(target),
        current_feature=str(current_feature or ""),
        source_available=True,
    )


# ---------------------------------------------------------------------------
# The render-layer assertion - the one rule that must not move
# ---------------------------------------------------------------------------


def assert_no_unevidenced_passing(
    features: Iterable[Any], *, rendered_lines: Sequence[str] = ()
) -> None:
    """Refuse to render any feature as passing without evidence behind it.

    This is the render-layer assertion the brief calls the single most important
    one in the phase, and it checks the brief's THREE clauses, in order, for
    every row:

    1. the row cites evidence at all;
    2. a run of that evidence was RECORDED - not reused from an unchanged state,
       and not merely declared;
    3. that run names THIS feature as its subject.

    It is called by every renderer in this module, so the rule is not a
    convention a future editor can route around. It checks the projection AND
    the rendered text: a projection that is correct and a renderer that prints
    it anyway is a real failure mode, and a projection-only gate cannot see it.

    ``getattr`` is used throughout rather than attribute access because the
    second half of the check must also work on a stand-in row object - a frozen
    dataclass's ``passing`` is a derived property, so the only honest way to
    plant the defect is a row whose own ``passing`` disagrees with its evidence.

    Raises :class:`LedgerContractError` naming the offending rows. Never
    silently downgrades, because a caller that asked for a check and got a
    quiet "no" would read that as a pass.
    """
    offenders: List[str] = []
    passing_rows: List[str] = []
    for feature in features or ():
        name = str(getattr(feature, "name", "") or "<unnamed>")
        evidenced = bool(getattr(feature, "evidenced", False))
        ran = bool(getattr(feature, "verification_ran", False))
        passing = bool(getattr(feature, "passing", False))
        method = str(getattr(feature, "verification_method", "") or "")
        run_recorded = bool(getattr(feature, "run_recorded", False))
        run_reused = bool(getattr(feature, "run_reused", False))
        run_scoped = bool(getattr(feature, "run_scoped_here", False))
        if passing:
            passing_rows.append(name)
            # Clause 1: evidence exists, and it is observable here.
            if not evidenced:
                offenders.append(f"{name}: claims passing with no evidence")
            if not ran:
                offenders.append(
                    f"{name}: claims passing with no observable verification"
                )
            # Clause 2: a verification METHOD and a RECORDED run, neither
            # reused-from-nothing nor merely declared.
            if not method:
                offenders.append(
                    f"{name}: claims passing with no verification method declared, "
                    "which makes the row a wish rather than a receipt"
                )
            if not run_recorded:
                offenders.append(
                    f"{name}: claims passing with no recorded run of its evidence"
                )
            if run_reused:
                offenders.append(
                    f"{name}: claims passing on a run REUSED from an unchanged state"
                )
            # Clause 3: the run belongs to THIS feature.
            if not run_scoped:
                offenders.append(
                    f"{name}: claims passing on a run that does not name this "
                    "feature as its subject"
                )

    joined = "\n".join(str(line) for line in (rendered_lines or ()))
    if joined:
        for feature in features or ():
            if getattr(feature, "passing", False):
                continue
            name = str(getattr(feature, "name", "") or "")
            if name and name in joined and _renders_passing(name, joined):
                offenders.append(f"{name}: rendered as passing by a non-passing row")

    if offenders:
        raise LedgerContractError(
            "a feature may not render as passing without evidence: "
            + "; ".join(sorted(set(offenders)))
        )


def no_unevidenced_passing(features: Iterable[Any]) -> List[str]:
    """Return every reason these rows may not render as passing, as prose.

    The gate above refuses. This one EXPLAINED, and a surface needs both: a
    refusal with no reason teaches a reader nothing, and the reason is what makes
    the difference between "we could not check" and "we checked and it failed".
    A row that IS passing is absent from the list, which is why the list length
    is the count of rows a reader should not believe yet.
    """
    out: List[str] = []
    for feature in features or ():
        if getattr(feature, "passing", False):
            continue
        name = str(getattr(feature, "name", "") or "<unnamed>")
        reason = str(getattr(feature, "not_passing_reason", "") or "")
        if not reason:
            reason = (
                "this row is not verified and this build cannot say why; the "
                "reason itself is missing, which is UNKNOWN rather than fine"
            )
        out.append(f"{name}: {reason}")
    return out


#: Words that turn a following status word into a DENIAL. Declared as data
#: because the very first version of this check was a bare ``"passing" in
#: lowered``, and the real ledger then failed it with this:
#:
#:     <name>: the ledger declares this row blocked (...); a blocked row is not
#:             a passing row
#:
#: The reason is a correct DENIAL, and the check read it as an ASSERTION - six
#: rows of an honest ledger refused by their own honest explanation. A gate that
#: cannot tell "this passes" from "this is not a passing row" is worse than no
#: gate, because it is believed.
_NEGATION_PREFIXES: Tuple[str, ...] = (
    "not ",
    "no ",
    "never ",
    "cannot ",
    "can not ",
    "is not ",
    "are not ",
    "was not ",
    "were not ",
    "does not ",
    "do not ",
    "did not ",
    "without ",
)


def _asserts_passing(line: str) -> bool:
    """Return whether one rendered line ASSERTS that its row passes.

    Two positive shapes are accepted and a denial of either is not:

    * this module's own marker, ``[pass]``, which appears in prose only as
      ``_MARKERS`` publishes it; and
    * a standalone ``passing`` / ``verified`` token that is not inside a
      negation, so ``NOT verified`` and ``is not a passing row`` are both
      correctly read as denials.
    """
    lowered = str(line or "").lower()
    if "[pass]" in lowered:
        return True
    for token in ("passing", "verified"):
        index = lowered.find(token)
        while index >= 0:
            before = lowered[:index].rstrip()
            negated = any(before.endswith(prefix.strip()) for prefix in _NEGATION_PREFIXES)
            # A token at the start of the line, or directly after a bracket, a
            # colon or a pipe, is a state cell rather than prose.
            lead = before[-1:] if before else ""
            if not negated and (not before or lead in "(:[|>=-,"):
                return True
            index = lowered.find(token, index + 1)
    return False


def _renders_passing(name: str, blob: str) -> bool:
    """Return whether the rendered blob announces ``name`` as passing.

    Narrow on purpose. The state word is this module's own vocabulary, so a
    renderer that shows it beside a row's name is claiming that row passes -
    unless the line denies it, which is what :func:`_asserts_passing` decides.
    """
    for line in blob.splitlines():
        if name not in line:
            continue
        if _asserts_passing(line):
            return True
    return False


# ---------------------------------------------------------------------------
# Coverage summary - prominent, and honest about what the number hides
# ---------------------------------------------------------------------------


def coverage_summary(ledger: FeatureLedger) -> Dict[str, Any]:
    """Return the coverage numbers, including the ones a bar would hide.

    ``verified`` counts rows that may render as passing. ``evidenced`` counts
    rows with evidence present. When ``verified`` and ``evidenced`` disagree the
    summary must SAY SO - the brief's edge case is "40 passing with 2
    evidenced", which a single number reads as healthy.
    """
    counts = ledger.counts()
    verified = counts.get("passing", 0)
    evidenced = sum(1 for feature in ledger.features if feature.evidenced)
    observable = sum(1 for feature in ledger.features if feature.verification_ran)
    unverified_without_evidence = [
        feature.name for feature in ledger.features if not feature.evidenced
    ]
    missing_evidence = [
        feature.name
        for feature in ledger.features
        if feature.evidenced and not feature.verification_ran
    ]
    wishes = [
        feature.name
        for feature in ledger.features
        if feature.evidenced and feature.verification_ran and not feature.run_recorded
    ]
    reused = [feature.name for feature in ledger.features if feature.run_reused]
    flaky = [feature.name for feature in ledger.features if feature.run_flaky]
    timed_out = [feature.name for feature in ledger.features if feature.run_timed_out]
    unscoped = [
        feature.name
        for feature in ledger.features
        if feature.run_passed and not feature.run_scoped_here
    ]
    discrepancy = verified - observable
    # WHY the observable-but-not-verified count is what it is, DERIVED from the
    # rows rather than asserted. Wave 1 hardcoded "the ledger does not declare
    # them implemented", which is false for every row of the shipped ledger -
    # all 31 declare ``Implemented`` - so the honest summary contradicted its
    # own rows one clause apart. A reason computed from the rows cannot go stale.
    reasons: List[str] = []
    if wishes:
        reasons.append(
            f"{len(wishes)} cite a resolvable reference but record no RUN of it"
        )
    if missing_evidence:
        reasons.append(
            f"{len(missing_evidence)} cite evidence absent from this checkout"
        )
    if unverified_without_evidence:
        reasons.append(f"{len(unverified_without_evidence)} cite no evidence at all")
    declared_not_implemented = [
        feature.name
        for feature in ledger.features
        if feature.verification_ran
        and feature.declared_status != "Implemented"
        and not feature.blocked
    ]
    if declared_not_implemented:
        reasons.append(
            f"{len(declared_not_implemented)} are not declared implemented by the ledger"
        )
    if not reasons:
        reasons.append("no reason is derivable, which is itself UNKNOWN")
    return {
        "source": ledger.source,
        "source_available": ledger.source_available,
        "total": int(counts.get("total", 0)),
        "verified": int(verified),
        "evidenced": int(evidenced),
        "verification_observable": int(observable),
        "blocked": int(counts.get("blocked", 0)),
        "failing": int(counts.get("failing", 0)),
        "unverified": int(counts.get("unverified", 0)),
        "verified_minus_observable": int(discrepancy),
        "rows_without_evidence": tuple(unverified_without_evidence),
        "rows_with_missing_evidence": tuple(missing_evidence),
        "observable_but_not_verified": int(discrepancy),
        "rows_with_no_recorded_run": tuple(wishes),
        "rows_with_reused_run": tuple(reused),
        "rows_with_flaky_run": tuple(flaky),
        "rows_with_timed_out_run": tuple(timed_out),
        "rows_whose_run_names_another_feature": tuple(unscoped),
        "not_verified_reasons": tuple(reasons),
        "current_feature": ledger.current_feature,
    }


def summary_line(summary: Mapping[str, Any]) -> str:
    """Return the one-line headline, honest about the difference.

    The first clause is the number a reader wants. The second is the one this
    module exists to make impossible to miss, and it is present whenever the
    two counts disagree **in either direction**:

    * more rows "verified" than have an observable verification is an
      OVERSTATEMENT and is named as such;
    * fewer rows "verified" than have an observable verification is not an
      error - those are rows whose evidence resolves but which the ledger
      declares blocked - and it is stated so a reader is not left wondering
      why the two numbers differ.
    """
    total = int(summary.get("total", 0) or 0)
    verified = int(summary.get("verified", 0) or 0)
    observable = int(summary.get("verification_observable", 0) or 0)
    wishes = len(tuple(summary.get("rows_with_no_recorded_run") or ()))
    if not summary.get("source_available", True):
        return (
            "FEATURE COVERAGE: unavailable - the ledger of record could not be "
            "read, which is NOT the same as 0 features outstanding"
        )
    if total <= 0:
        return (
            "FEATURE COVERAGE: 0 of 0 features - the ledger of record declared "
            "no capability rows, which is a broken ledger, not a finished one"
        )
    head = f"FEATURE COVERAGE: {verified} of {total} features verified"
    if verified > observable:
        head += (
            f" - and {verified - observable} of those have NO observable "
            "verification in this checkout, so the verified count is "
            "OVERSTATED"
        )
    elif verified < observable:
        head += (
            f"; a further {observable - verified} row(s) cite evidence that "
            "resolves here but are NOT counted as verified: "
            + "; ".join(str(item) for item in (summary.get("not_verified_reasons") or ()))
        )
        # The "wish" maxim appears ONLY here, and only when the derived reason
        # block above has not already carried it. The first draft emitted both
        # clauses and said the same thing twice in one headline, which is how a
        # reader learns to skip the headline.
        if (
            wishes
            and not any("no RUN" in str(item) for item in (summary.get("not_verified_reasons") or ()))
        ):
            head += (
                f"; {wishes} row(s) cite a resolvable reference but record "
                "no RUN of it, and a cited file is a wish rather than a "
                "receipt"
            )
    blocked = int(summary.get("blocked", 0) or 0)
    if blocked:
        head += f"; {blocked} blocked (a blocked row is not a passing row)"
    return head


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_MARKERS = {
    "passing": "[pass]",
    "failing": "[FAIL]",
    "unverified": "[unverified]",
    "blocked": "[blocked]",
}


def _line(text: Any) -> str:
    """Return one display line through the ONE sanitise entry point.

    Every rendered value in this module is user- or project-supplied (feature
    names and notes come from a Markdown file a person edits), and the notes
    field carries bold markers that a markup renderer would otherwise interpret.
    """
    return sanitize_text(text)


def feature_lines(ledger: FeatureLedger, *, width: int = 0) -> List[str]:
    """Render the whole ledger as a LIST - never a bar.

    A bar hides which features are done; the brief's requirement is that a
    reader can see the row, so a bar is not a summary of this ledger, it is a
    different (lossier) surface wearing its name.
    """
    assert_statuses_come_from_the_authority()
    out: List[str] = ["FEATURE LEDGER (a list, not a progress bar):"]
    if not ledger.source_available:
        out.append(
            f"  !! the ledger of record could not be read: {sanitize_text(ledger.source)}"
        )
        out.append(
            "     an unreadable ledger reports 0 features; that is a MISSING "
            "RECEIPT, not a finished project"
        )
        assert_no_unevidenced_passing(ledger.features, rendered_lines=out)
        return out
    if not ledger.features:
        out.append(
            "  !! the ledger of record declared no capability rows. An empty "
            "ledger is a broken ledger, not a finished one."
        )
        assert_no_unevidenced_passing(ledger.features, rendered_lines=out)
        return out
    current = (ledger.current_feature or "").strip().lower()
    for feature in ledger.features:
        marker = _MARKERS[feature.state]
        is_current = bool(current) and current in feature.name.strip().lower()
        pointer = "->" if is_current else "  "
        out.append(
            f"  {pointer} {marker} {_line(feature.name)} "
            f"[declared: {_line(feature.declared_status)}]"
        )
        if feature.passing:
            out.append(
                f"       verified by a recorded run: "
                f"{_line(', '.join(feature.recorded_outcomes))} via "
                f"{_line(feature.verification_method or 'unrecorded method')}"
            )
            continue
        out.append(f"       NOT verified: {_line(feature.not_passing_reason)}")
    assert_no_unevidenced_passing(ledger.features, rendered_lines=out)
    return out


def reason_histogram(ledger: FeatureLedger) -> List[Tuple[str, int, str]]:
    """Return ``(reason_class, count, example_name)`` for every unverified row.

    A HISTOGRAM, and that is the whole design. The first draft of this rendered
    every unverified row's full reason sentence on top of the row that had
    already rendered it, so ``feature_lines`` said each reason twice - the
    recorded ``DUPLICATE_EXEMPT`` class, produced by this round's own code. A
    histogram adds what a per-row list cannot: how many rows share each reason,
    which is the thing a reader acts on.
    """
    buckets: Dict[str, List[str]] = {}
    for feature in ledger.features:
        if feature.passing:
            continue
        reason = str(feature.not_passing_reason or "") or (
            "this row is not verified and this build cannot say why"
        )
        buckets.setdefault(reason, []).append(feature.name)
    return [
        (reason, len(names), names[0])
        for reason, names in sorted(buckets.items(), key=lambda item: -len(item[1]))
    ]


def wish_lines(ledger: FeatureLedger) -> List[str]:
    """Name the reasons the unverified rows share, as counts rather than sentences.

    The forward path, rendered as a histogram on the coverage surface. Every
    reason here is stated verbatim beside its own row in ``feature_lines``, so
    repeating the sentences would add nothing; what a histogram adds is the
    SHAPE - "31 of 31 cite a reference nobody ran" is actionable, and eight
    copies of one sentence is not.
    """
    histogram = reason_histogram(ledger)
    if not histogram:
        return [
            "  every declared row is verified by a recorded run of evidence "
            "whose subject is that row"
        ]
    out: List[str] = [f"  {sum(item[1] for item in histogram)} unverified row(s), by reason:"]
    for reason, count, example in histogram:
        out.append(f"    {count} row(s) (e.g. {_line(example)}): {_line(reason)}")
    return out


def feature_detail_lines(ledger: FeatureLedger, name: str) -> List[str]:
    """Render ONE feature's evidence so a reader can click through it.

    Every reference is listed with whether it resolves, because a reference a
    reader cannot follow is the exact thing a ledger is supposed to rule out.
    """
    assert_statuses_come_from_the_authority()
    feature = ledger.by_name(name)
    out: List[str] = ["FEATURE DETAIL"]
    if feature is None:
        out.append(f"  no declared feature matches {sanitize_text(name)!r}")
        return out
    out.append(f"  name: {_line(feature.name)}")
    out.append(f"  declared status: {_line(feature.declared_status)}")
    out.append(f"  rendered state: {feature.state}")
    out.append(f"  evidence cited: {len(feature.evidence)}")
    out.append(f"  verification observable here: {feature.verification_ran}")
    out.append(
        f"  verification method: {_line(feature.verification_method or 'NONE DECLARED')}"
    )
    out.append(
        "  recorded run: "
        + (
            ", ".join(feature.recorded_outcomes)
            if feature.run_recorded
            else "NONE - nothing was executed and recorded"
        )
    )
    out.append(f"  run scoped to this feature: {feature.run_scoped_here}")
    out.append(f"  ledger: {_line(ledger.source)} line {feature.line_number}")
    if feature.notes:
        out.append(f"  notes: {_line(feature.notes)}")
    if not feature.passing:
        out.append(f"  NOT VERIFIED: {_line(feature.not_passing_reason)}")
    if not feature.evidence:
        out.append(
            "  !! NO EVIDENCE: this row cannot be rendered as passing and is not "
            "counted in the verified total"
        )
    else:
        out.append("  evidence:")
        for item in feature.evidence:
            for line in item.line().splitlines():
                out.append(line)
    assert_no_unevidenced_passing(ledger.features, rendered_lines=out)
    return out


def render_coverage_lines(ledger: FeatureLedger) -> List[str]:
    """Render the coverage summary LOUDLY, including what the number hides."""
    summary = coverage_summary(ledger)
    out: List[str] = ["=" * 68, summary_line(summary), "=" * 68]
    if not summary["source_available"]:
        out.append(
            "  reason: the ledger of record could not be read. This is NOT "
            "'no features outstanding'."
        )
        return out
    out.append(
        f"  verified: {summary['verified']}   "
        f"with evidence present: {summary['verification_observable']}   "
        f"with a RECORDED run: {summary['verified']}"
    )
    out.append(
        f"  blocked: {summary['blocked']}   "
        f"failing: {summary['failing']}   "
        f"unverified: {summary['unverified']}"
    )
    if summary["rows_without_evidence"]:
        out.append(
            f"  !! {len(summary['rows_without_evidence'])} row(s) cite NO "
            "evidence and are therefore not counted as verified:"
        )
        for name in summary["rows_without_evidence"]:
            out.append(f"       - {_line(name)}")
    if summary["rows_with_missing_evidence"]:
        out.append(
            f"  !! {len(summary['rows_with_missing_evidence'])} row(s) cite "
            "evidence that is ABSENT from this checkout:"
        )
        for name in summary["rows_with_missing_evidence"]:
            out.append(f"       - {_line(name)}")
    if summary["rows_with_no_recorded_run"]:
        out.append(
            f"  !! {len(summary['rows_with_no_recorded_run'])} row(s) cite a "
            "resolvable reference and record NO RUN of it. A cited file is a "
            "wish, not a receipt, so none of these counts as verified:"
        )
        for name in summary["rows_with_no_recorded_run"]:
            out.append(f"       - {_line(name)}")
    for key, heading in (
        ("rows_with_reused_run", "reused a run from an UNCHANGED state"),
        ("rows_with_flaky_run", "recorded a FLAKY verification"),
        ("rows_with_timed_out_run", "recorded a verification that TIMED OUT"),
        (
            "rows_whose_run_names_another_feature",
            "recorded a run whose subject is a DIFFERENT feature",
        ),
    ):
        names = tuple(summary.get(key) or ())
        if not names:
            continue
        out.append(f"  !! {len(names)} row(s) {heading}:")
        for name in names:
            out.append(f"       - {_line(name)}")
    if summary["verified"] > summary["verification_observable"]:
        out.append(
            "  !! READ THIS: rows are marked verified but NO verification is "
            "observable in this checkout. The verified count is OVERSTATED."
        )
    out.extend(wish_lines(ledger))
    # P4/W2: this sentence was true under Wave 1's rule and became FALSE under
    # this one, which is exactly how a stale reassurance ships. Its condition
    # only checked that evidence existed and resolved; it then asserted that
    # every unverified row was a BLOCKED one. After the rule was narrowed, 25
    # unverified rows were not blocked, so the receipt told a reader that the
    # only thing standing between them and a green list was an owner decision -
    # while the real answer was that nothing was ever run. It now has to earn
    # the claim.
    unverified_all_blocked = all(
        feature.blocked for feature in ledger.features if not feature.passing
    )
    if (
        summary["total"]
        and not summary["rows_without_evidence"]
        and not summary["rows_with_missing_evidence"]
        and unverified_all_blocked
    ):
        out.append(
            "  every row cites evidence and every cited reference resolves in "
            "this checkout; the rows not counted as verified are ones the ledger "
            "declares blocked"
        )
    elif summary["total"] and not summary["rows_with_no_recorded_run"]:
        out.append(
            "  every row cites evidence that resolves here, but a row that was "
            "never RUN is not a verified row; the histogram above is the "
            "complete list of what is missing"
        )
    return out


def current_feature_lines(ledger: FeatureLedger) -> List[str]:
    """Render which feature THIS session is on, or say that nobody declared one.

    The brief's requirement is that the user never has to wonder. Silence reads
    as "nothing is in progress", which is the opposite of the honest answer
    when nobody declared a feature.
    """
    out: List[str] = ["THIS SESSION'S FEATURE"]
    name = (ledger.current_feature or "").strip()
    if not name:
        out.append(
            "  no feature was declared for this session. That is UNKNOWN, which "
            "is not the same as 'nothing is in progress'."
        )
        return out
    feature = ledger.by_name(name)
    if feature is None:
        out.append(
            f"  declared: {_line(name)} - this does NOT match any row in the "
            "ledger of record, so its status cannot be reported"
        )
        return out
    out.append(
        f"  {_MARKERS[feature.state]} {_line(feature.name)} "
        f"[declared: {_line(feature.declared_status)}]"
    )
    out.append(
        f"  verification observable here: {feature.verification_ran}   "
        f"evidence cited: {len(feature.evidence)}   "
        f"recorded run: {', '.join(feature.recorded_outcomes) if feature.run_recorded else 'NONE'}"
    )
    if not feature.passing:
        out.append(f"  NOT verified: {_line(feature.not_passing_reason)}")
    return out


# ---------------------------------------------------------------------------
# Operator surface
# ---------------------------------------------------------------------------


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Print the ledger, its coverage summary, and the current feature."""
    args = list(sys.argv[1:] if argv is None else argv)
    repo = "."
    as_json = "--json" in args
    feature = ""
    if "--repo" in args:
        index = args.index("--repo")
        if index + 1 < len(args):
            repo = args[index + 1]
    if "--feature" in args:
        index = args.index("--feature")
        if index + 1 < len(args):
            feature = args[index + 1]
    ledger = load_ledger(repo, current_feature=feature)
    if as_json:
        payload: Dict[str, Any] = dict(ledger.to_dict())
        payload["coverage"] = coverage_summary(ledger)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    for line in feature_lines(ledger):
        print(line)
    if feature:
        for line in feature_detail_lines(ledger, feature):
            print(line)
    for line in render_coverage_lines(ledger):
        print(line)
    for line in current_feature_lines(ledger):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(_cli())
