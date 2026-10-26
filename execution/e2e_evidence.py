"""P4/W2 T2.W2.2 — a verification result that cannot be MISREAD.

Wave 1 built the mechanism and this round makes the result self-describing
enough that **a reader — human or agent — cannot read it wrongly**, which is a
different problem from the one Wave 1 solved. Wave 1's question was "does the
thing work?"; this one is "when you read the answer, can you tell what you are
holding?"

--------------------------------------------------------------------------
THE RULE
--------------------------------------------------------------------------

**A verification result must be sufficient, on its own, to know exactly what
was checked, on what, and how.** No additional lookup, no re-derivation, no
trusting the caller's context. That is what
:func:`execution.verification_evidence.ledger_evidence` already demands of the
unit half, and :class:`E2EEvidence` is the same thing for the end-to-end half.

Six consequences, each one a refusal rather than a convenience:

1. **Every result names what ran, on which commit, with which selection.**
   :class:`EvidenceAnchor`. A reader who cannot name the commit does not have a
   result, they have a rumour.

2. **Five states, never collapsed.** ``passed`` / ``failed`` / ``blocked`` /
   ``not_run`` / ``untrustworthy``. :func:`assert_states_not_collapsed` is the
   executable form and it is a *refusal*, because a duplicated member makes a
   "closed five-tuple" silently smaller.

3. **A result from a different commit is not evidence for this one.**
   :class:`CrossCommitRefused`. **Structural, not advisory** — the anchor is
   bound at construction and :func:`assert_same_commit` raises. There is no
   flag a caller can set to waive it, because a waivable cross-commit check is
   one nobody sets.

4. **A reused result names what it was reused from and is marked distinct from
   a fresh run.** :func:`execution.e2e_trust.reuse_of` builds the mark and this
   module carries it. The mark's ``fresh_evidence`` is hard-coded ``False`` —
   the only value the producer can emit — so "reuse saves a re-run but never
   marks a feature passing" is a property of the code, not of a convention.

5. **Coverage is reported, and ``0`` is never a pass.**
   :class:`CoverageReceipt`. Zero coverage is ``not_run``, and it says so by
   name rather than rendering as ``0%`` next to a green row. A reader scanning
   a percentage column cannot tell "we ran nothing" from "we ran everything and
   covered nothing", and that confusion is the whole failure.

6. **The object is sufficient for T5's ledger gate with no additional lookup.**
   :func:`ledger_gate` composes this module's end-to-end half with
   :func:`execution.verification_evidence.ledger_evidence`'s unit half — a
   DELEGATION, not a second set of eight requirements. Two provenance gates in
   one tree would be two answers to "may this feature be marked passing", which
   is the defect class this repository keeps paying for.

--------------------------------------------------------------------------
WHY THE ANCHOR IS BUILT HERE AND NOT INHERITED
--------------------------------------------------------------------------

:func:`execution.e2e_trust.read_commit` reads ``.git/HEAD`` by filesystem and
never spawns ``git``. That is deliberate for this module too: a commit is part
of what every evidence object must carry, so a reader that required a
subprocess to produce one would make the anchor optional in exactly the
environment where it matters most — a container with no ``git`` on PATH.

An **unresolvable** commit is published as ``status="unknown"`` with its
reason. It is never invented and never inherited from a caller default, because
a fabricated anchor is worse than a missing one: it makes evidence from two
different trees look like evidence about one.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from execution import e2e_verify as ev
from execution.e2e_trust import (
    CommitAnchor,
    assert_five_states_distinct,
    read_commit,
    reuse_of,
)
from execution.e2e_verify import (
    E2E_BLOCKED,
    E2E_FAILED,
    E2E_NOT_RUN,
    E2E_PASSED,
    E2E_UNTRUSTWORTHY,
    E2EReport,
    E2EResult,
)

__all__ = [
    "COVERAGE_ZERO_MEANS",
    "E2E_EVIDENCE_SCHEMA_VERSION",
    "E2E_PROVABILITY",
    "ENVIRONMENT_FIELDS",
    "FIVE_STATES",
    "STATE_COLLAPSE_NOTE",
    "CoverageReceipt",
    "CrossCommitRefused",
    "E2EEvidence",
    "E2ELedgerVerdict",
    "EvidenceAnchor",
    "Selection",
    "assert_same_commit",
    "assert_states_not_collapsed",
    "attach_evidence",
    "build_evidence",
    "build_reuse_evidence",
    "environment_fingerprint",
    "evidence_of",
    "ledger_gate",
    "render_evidence",
]


E2E_EVIDENCE_SCHEMA_VERSION = 2

#: The five states, in the order a reader should read them: decided-and-good,
#: decided-against, could-not-check, never-attempted, and ran-but-void.
#:
#: The order is the pedagogy. ``untrustworthy`` is LAST because it is the one
#: that looks most like a pass from a distance, and putting it next to
#: ``passed`` in the reader's eye would be the collapse this module forbids.
FIVE_STATES: Tuple[str, str, str, str, str] = (
    E2E_PASSED,
    E2E_FAILED,
    E2E_BLOCKED,
    E2E_NOT_RUN,
    E2E_UNTRUSTWORTHY,
)

#: What a coverage fraction of zero MEANS. Published as a constant because it is
#: the single most misreadable number in the system and rendering it as ``0%``
#: beside a green row is precisely the failure this module exists to prevent.
COVERAGE_ZERO_MEANS = "not_run"

STATE_COLLAPSE_NOTE = (
    "five states, never collapsed. 'blocked' is not 'failed' (I could not check "
    "vs it is broken). 'not_run' is not 'passed' (nobody looked). "
    "'untrustworthy' is not 'passed' AND not 'failed' (the check ran and its "
    "verdict is void). Collapsing any pair of these converts an honest gap into "
    "a false pass, or sends a repair loop after a working tree."
)

#: The environment facts a reuse decision must match. Three groups, because
#: :mod:`execution.verify_cache` already covers the first two for the tree:
#: content digest covers the SOURCE, declaration digest covers the DECLARED
#: TESTS, and this fingerprint covers everything else that could change an
#: answer without changing a byte of the repository — the image, the boundary,
#: the runner, the network posture.
ENVIRONMENT_FIELDS: Tuple[str, ...] = (
    "image",
    "boundary",
    "runner",
    "network",
)


class CrossCommitRefused(RuntimeError):
    """A measurement of one commit was offered as evidence for another.

    Deliberately NOT a ``ValueError`` and NOT a ``PermissionError``. A caller
    with a broad ``except ValueError`` around its evidence assembly would
    swallow this and carry on with evidence about the wrong tree, and a caller
    with a broad ``except PermissionError`` would treat it as an approval
    question. Neither is what this is.
    """


# --------------------------------------------------------------------------
# The environment half of a content address
# --------------------------------------------------------------------------


def environment_fingerprint(
    *,
    image: str = "unknown",
    boundary: str = "unknown",
    runner: str = "unknown",
    network: str = "unknown",
) -> Dict[str, Any]:
    """Digest the environment facts a reuse decision has to match.

    ``unknown`` is a published value, not an absence, and it is deliberately
    the DEFAULT of every field. A caller that knows nothing must produce a
    fingerprint no other unknown caller shares — because two runs whose
    environments are both unknown have NOT been shown to match, and treating
    them as equal is the stale-serve bug
    :mod:`execution.verify_cache` is named after.

    So the digest carries the literal ``unknown`` text AND the caller is
    expected to publish which fields it actually measured. The honest reading of
    "both unknown" is "comparable only if you accept unknown == unknown", and
    :func:`ledger_gate` refuses a reused row whose environment was never
    measured.
    """
    fields = {
        "image": str(image or "unknown"),
        "boundary": str(boundary or "unknown"),
        "runner": str(runner or "unknown"),
        "network": str(network or "unknown"),
    }
    unmeasured = tuple(
        name for name in ENVIRONMENT_FIELDS if fields[name] == "unknown"
    )
    payload = "\x1f".join(f"{name}={fields[name]}" for name in ENVIRONMENT_FIELDS)
    return {
        "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        "fields": fields,
        "unmeasured_fields": unmeasured,
        "fully_measured": not unmeasured,
        "note": (
            "'unknown' is a value, not an absence. A fingerprint whose fields "
            "are unknown has NOT been shown to match another, which is why "
            "ledger_gate refuses a reused row on unmeasured_fields"
        ),
    }


# --------------------------------------------------------------------------
# Selection and coverage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """WHICH verification ran. Never inferred from a boolean.

    Four separate counts, and the distinction is the point:

    ``declared``    what the repository said it had
    ``attempted``  what this run tried
    ``executed``   what actually produced an observation
    ``settled``    what produced a verdict about the feature

    A run where ``declared=3, attempted=3, executed=0`` is the shape that reads
    as "verified" in a summary and is not. Four numbers make it impossible.
    """

    declared: int
    attempted: int
    executed: int
    settled: int
    check_ids: Tuple[str, ...] = ()
    not_executed: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("declared", self.declared),
            ("attempted", self.attempted),
            ("executed", self.executed),
            ("settled", self.settled),
        ):
            if int(value) < 0:
                raise ValueError(f"{name} cannot be negative: {value!r}")
        if int(self.attempted) > int(self.declared):
            raise ValueError(
                f"attempted ({self.attempted}) exceeds declared ({self.declared}); "
                f"a run cannot have attempted more checks than the repository "
                f"declared, so one of the two numbers is wrong"
            )
        if int(self.executed) > int(self.attempted):
            raise ValueError(
                f"executed ({self.executed}) exceeds attempted ({self.attempted}); "
                f"a check that produced no observation cannot have been attempted"
            )

    @property
    def fraction(self) -> Optional[float]:
        """Executed over declared, or ``None`` when nothing was declared.

        ``None`` rather than ``0.0`` for the absent case, for the same reason
        :mod:`execution.verification_evidence` returns ``None`` for the coverage
        of nothing: zero reads as "we ran everything and covered nothing",
        which is a different claim from "we ran nothing".
        """
        if int(self.declared) <= 0:
            return None
        return round(float(self.executed) / float(self.declared), 4)

    @property
    def complete(self) -> bool:
        """Every DECLARED check executed."""
        return int(self.declared) > 0 and int(self.executed) == int(self.declared)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "declared": int(self.declared),
            "attempted": int(self.attempted),
            "executed": int(self.executed),
            "settled": int(self.settled),
            "check_ids": list(self.check_ids),
            "not_executed": list(self.not_executed),
            "reasons": list(self.reasons),
            "fraction": self.fraction,
            "complete": bool(self.complete),
        }


@dataclass(frozen=True)
class CoverageReceipt:
    """What fraction of the declared verification actually executed.

    **Zero coverage is ``not_run``, never a pass.** :attr:`verdict` returns the
    literal string ``"not_run"`` for it, so a caller that renders
    ``receipt.fraction`` next to ``receipt.verdict`` cannot pair ``0.0`` with
    ``passed``.
    """

    selection: Selection
    policy: str = ""
    policy_reason: str = ""
    spec_status: str = ""

    @property
    def fraction(self) -> Optional[float]:
        return self.selection.fraction

    @property
    def verdict(self) -> str:
        """``complete`` / ``partial`` / ``not_run`` — never ``passed``.

        The vocabulary is deliberately disjoint from the state vocabulary: this
        answers "how much ran", not "what did it find". A reader who conflates
        ``partial`` with ``passed`` is the failure this receipt exists for, so
        there is no value here that could be mistaken for either.
        """
        if self.selection.fraction is None:
            return COVERAGE_ZERO_MEANS
        if self.selection.complete:
            return "complete"
        if int(self.selection.executed) > 0:
            return "partial"
        return COVERAGE_ZERO_MEANS

    @property
    def may_report_passing(self) -> bool:
        """Only a COMPLETE coverage receipt may sit beside a passing claim."""
        return self.verdict == "complete"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "fraction": self.fraction,
            "selection": self.selection.to_dict(),
            "policy": self.policy,
            "policy_reason": self.policy_reason,
            "spec_status": self.spec_status,
            "zero_coverage_means": COVERAGE_ZERO_MEANS,
            "may_report_passing": bool(self.may_report_passing),
            "note": (
                "a fraction of 0 is NOT_MEASURED work, not a coverage number. "
                "It is reported as `not_run` so it can never be rendered beside "
                "a passing claim and read as 'we ran everything and covered "
                "nothing'"
            ),
        }


# --------------------------------------------------------------------------
# The anchor
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceAnchor:
    """What ran, on what, and how — the three questions a reader must be able
    to answer before trusting a number."""

    what_ran: Tuple[str, ...] = ()
    commit: CommitAnchor = field(default_factory=lambda: CommitAnchor("", "unknown"))
    selection: Selection = field(default_factory=lambda: Selection(0, 0, 0, 0))
    declaration_digest: str = ""
    content_digest: str = ""
    environment: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    repo_path: str = ""
    started_at: float = 0.0
    duration_s: float = 0.0
    tool_versions: Dict[str, str] = field(default_factory=dict)

    @property
    def commit_sha(self) -> str:
        return self.commit.commit

    @property
    def commit_known(self) -> bool:
        return self.commit.known

    @property
    def self_describing(self) -> bool:
        """Whether a reader could name what ran, on what, and how.

        Three separate conditions, and **none of them is satisfied by a
        default**. An empty ``what_ran`` means the receipt does not say what it
        measured; an unresolvable commit means it does not say what tree; an
        unmeasured environment means it cannot say a reused row is comparable.
        """
        return (
            bool(self.what_ran)
            and bool(self.commit.commit)
            and bool(self.selection.declared)
            and bool(self.environment.get("digest"))
        )

    @property
    def unself_describing(self) -> Tuple[str, ...]:
        """Every reason the receipt is not sufficient, named."""
        missing: List[str] = []
        if not self.what_ran:
            missing.append("what_ran: the receipt does not name what it measured")
        if not self.commit.commit:
            missing.append(
                f"commit: {self.commit.status} — {self.commit.reason or 'unresolved'}"
            )
        if not int(self.selection.declared):
            missing.append("selection: nothing was declared, so nothing can be covered")
        if not self.environment.get("digest"):
            missing.append("environment: no fingerprint, so reuse cannot be compared")
        elif not self.environment.get("fully_measured"):
            missing.append(
                "environment: unmeasured field(s) "
                + ", ".join(self.environment.get("unmeasured_fields") or ())
            )
        return tuple(missing)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "what_ran": list(self.what_ran),
            "commit": self.commit.to_dict(),
            "commit_sha": self.commit_sha,
            "commit_known": bool(self.commit_known),
            "selection": self.selection.to_dict(),
            "declaration_digest": self.declaration_digest,
            "content_digest": self.content_digest,
            "environment": dict(self.environment),
            "session_id": self.session_id,
            "repo_path": self.repo_path,
            "started_at": float(self.started_at or 0.0),
            "duration_s": round(float(self.duration_s or 0.0), 4),
            "tool_versions": dict(self.tool_versions),
            "self_describing": bool(self.self_describing),
            "not_self_describing_because": list(self.unself_describing),
        }


def assert_same_commit(evidence: Any, commit: str, *, field_name: str = "commit") -> str:
    """Refuse evidence from a different commit. **Structural.**

    The rule is one sentence: *a measurement of commit A cannot speak about
    commit B.* This function is its executable form and it RAISES
    :class:`CrossCommitRefused` on any mismatch, including the case where the
    evidence has no resolvable commit at all — an unanchored measurement is not
    a wildcard, and treating it as one is how a result from an arbitrary tree
    becomes evidence for whichever tree is being reviewed.

    Three cases, all refusals:

    * ``evidence.commit == commit`` — accepted, and the sha is returned;
    * both empty — accepted, because a repository with no commit yet cannot
      disagree with itself, and the anchor's ``status`` is what says so;
    * anything else, including one side empty — refused, naming both.

    There is deliberately **no ``force=True``**. A waivable cross-commit check
    is one nobody sets.

    **The first version read the wrong attribute and refused everything,
    including the control.** The anchor's ``commit`` field is a
    :class:`~execution.e2e_trust.CommitAnchor` *object*, so ``str(...)`` on it
    produced ``"CommitAnchor(commit='52b0ec…', status='resolved', …)"`` — a
    string that matches no sha, so the equality never held and every call
    raised. The attack driver's control arm is what caught it, and that is
    exactly what a control arm is for: a cross-commit check that refuses
    everything is indistinguishable from a working one by its refusals alone.

    So the sha is read from the ``commit_sha`` PROPERTY, and a non-string there
    is treated as a defect rather than stringified — because a silently
    stringified object is a refusal that can never be satisfied.
    """
    anchor = getattr(evidence, "anchor", None)
    have = str(getattr(anchor, "commit_sha", "") or "")
    want = str(commit or "")
    if have and (len(have) != 40 or not all(c in "0123456789abcdef" for c in have)):
        raise CrossCommitRefused(
            f"the evidence's commit anchor holds {have[:16]!r}, which is not a "
            f"40-hex sha, so it cannot be compared. An unreadable anchor is "
            f"refused rather than stringified: a commit that cannot be read is "
            f"not evidence about any particular tree."
        )
    if have == want:
        return have
    raise CrossCommitRefused(
        f"evidence from commit {have[:12] or '(unresolved)'} is not evidence for "
        f"commit {want[:12] or '(unresolved)'}. A measurement of one tree cannot "
        f"be reused for another: the feature, the check, or the declaration may "
        f"all have changed, and nothing in the receipt can establish that they "
        f"have not. Re-run against {want[:12] or 'the tree under review'}. "
        f"(execution.e2e_evidence.assert_same_commit)"
    )


def assert_states_not_collapsed(rows: Sequence[Any]) -> Tuple[str, int]:
    """Require the five states to be five DISTINCT values and report the tally.

    Two separate checks, because they fail differently:

    * the **vocabulary** is exactly the published five and has no duplicate
      member — delegated to
      :func:`execution.e2e_trust.assert_five_states_distinct`, which also
      refuses a duplicated member outright (a duplicated member makes a "closed
      five-tuple" silently smaller);
    * the **rows** only carry members of it. A row carrying ``"skipped"`` or
      ``"unknown"`` is a collapse in the making, and refusing it here means the
      vocabulary cannot grow by accretion.

    Returns the tally as a tuple of ``(state, count)`` so the caller can render
    it. The tally is published on every evidence object for the same reason the
    states are: a reader counting ``failed`` rows must not be able to conclude
    that "no ``failed`` row" means "nothing went wrong".
    """
    states = assert_five_states_distinct()
    tally: Dict[str, int] = {name: 0 for name in states}
    for row in rows or ():
        state = str(getattr(row, "state", "") or "")
        if state not in tally:
            raise AssertionError(
                f"a row reports state {state!r}, which is not one of the five "
                f"({', '.join(states)}). A state outside the closed vocabulary is "
                f"how the five collapse into six, and a sixth is one no reader "
                f"was given a meaning for."
            )
        tally[state] += 1
    return tuple((name, tally[name]) for name in states)


# --------------------------------------------------------------------------
# The evidence object
# --------------------------------------------------------------------------


@dataclass
class E2EEvidence:
    """Everything T5's ledger gate needs, with no additional lookup.

    The shape is the brief's rule 6 turned into a dataclass. Six blocks, each
    answering one question a reader must be able to answer:

    ===============  =========================================================
    ``anchor``      what ran, on which commit, with which selection
    ``rows``        every check's outcome, with its reason and its own receipt
    ``coverage``    what fraction of the declared verification executed
    ``reuse``       what this was reused from, or ``{}`` for a fresh run
    ``tally``       the five states, counted
    ``ledger``      the composed verdict, end-to-end half plus delegated unit half
    ===============  =========================================================
    """

    anchor: EvidenceAnchor
    rows: Tuple[E2EResult, ...] = ()
    coverage: CoverageReceipt = field(default_factory=lambda: CoverageReceipt(Selection(0, 0, 0, 0)))
    verdict: str = ""
    policy: Dict[str, Any] = field(default_factory=dict)
    spec_status: str = ""
    spec_reason: str = ""
    reuse: Dict[str, Any] = field(default_factory=dict)
    ledger: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = E2E_EVIDENCE_SCHEMA_VERSION

    # -- readers -----------------------------------------------------------

    @property
    def fresh(self) -> bool:
        """Whether a run happened for THIS tree in THIS process.

        ``False`` for a reused row, and ``False`` for a row whose reuse mark is
        absent — the same fail-closed reading as
        :func:`execution.verify_cache.can_mark_passing`. A builder that forgot
        to state freshness must not produce a result a ledger will accept.
        """
        if self.reuse.get("reused"):
            return False
        return bool(self.reuse.get("fresh_evidence", False))

    @property
    def tally(self) -> Tuple[Tuple[str, int], ...]:
        return assert_states_not_collapsed(self.rows)

    @property
    def may_mark_feature_passing(self) -> bool:
        """Whether a ledger may mark this feature ``passing``.

        Delegates to the composed verdict in :attr:`ledger` when one was built,
        because this property must not be a second implementation of "may this
        pass". With no ledger verdict it withholds, which is the safe direction.
        """
        if not self.ledger:
            return False
        return bool(self.ledger.get("provable"))

    @property
    def suggested_state(self) -> str:
        """A hint for T5's vocabulary. Never the word ``passing`` on its own."""
        if not self.ledger:
            return "unmeasured"
        return str(self.ledger.get("suggested_state") or "evidence_incomplete")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "anchor": self.anchor.to_dict(),
            "rows": [row.to_dict() for row in self.rows],
            "coverage": self.coverage.to_dict(),
            "verdict": self.verdict,
            "policy": dict(self.policy),
            "spec_status": self.spec_status,
            "spec_reason": self.spec_reason,
            "reuse": dict(self.reuse),
            "ledger": dict(self.ledger),
            "tally": {name: count for name, count in self.tally},
            "five_states": list(FIVE_STATES),
            "state_collapse_note": STATE_COLLAPSE_NOTE,
            "fresh": bool(self.fresh),
            "may_mark_feature_passing": bool(self.may_mark_feature_passing),
            "suggested_state": self.suggested_state,
            "self_describing": bool(self.anchor.self_describing),
            "not_self_describing_because": list(self.anchor.unself_describing),
        }

    def render(self) -> List[str]:
        """The lines a surface prints. Every one of the six blocks, in order."""
        anchor = self.anchor
        lines = ["end-to-end verification evidence"]
        lines.append(f"  verdict      : {self.verdict or 'unknown'}")
        lines.append(
            f"  commit       : {anchor.commit_sha[:12] or '(unresolved)'} "
            f"[{anchor.commit.status}]"
        )
        if anchor.commit.ref:
            lines.append(f"                ref {anchor.commit.ref}")
        lines.append(f"  ran          : {', '.join(anchor.what_ran) or '(nothing recorded)'}")
        lines.append(f"  selection    : {self.coverage.verdict} ({self.coverage.fraction})")
        lines.append(
            f"                declared={self.coverage.selection.declared} "
            f"attempted={self.coverage.selection.attempted} "
            f"executed={self.coverage.selection.executed} "
            f"settled={self.coverage.selection.settled}"
        )
        lines.append(f"  fresh        : {self.fresh}")
        if self.reuse.get("reused"):
            lines.append(f"  reused from  : {self.reuse.get('summary')}")
        else:
            lines.append("  reused from  : (a fresh run; nothing was reused)")
        tally = ", ".join(f"{name}={count}" for name, count in self.tally)
        lines.append(f"  tally        : {tally}")
        lines.append(
            f"  ledger       : may mark passing = {self.may_mark_feature_passing} "
            f"(suggested {self.suggested_state})"
        )
        missing = anchor.unself_describing
        if missing:
            lines.append("  NOT SUFFICIENT ON ITS OWN:")
            for reason in missing:
                lines.append(f"    - {reason}")
        return lines


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def _selection_from(report: E2EReport) -> Selection:
    declared = 0
    if report.spec_status == ev.SPEC_FOUND or report.declared:
        declared = len(report.results)
    attempted = sum(1 for row in report.results if row.ran)
    executed = sum(1 for row in report.results if row.repetitions > 0)
    settled = sum(1 for row in report.results if row.settled)
    not_executed = tuple(
        row.check_id for row in report.results if int(row.repetitions) <= 0
    )
    return Selection(
        declared=int(declared),
        attempted=int(attempted),
        executed=int(executed),
        settled=int(settled),
        check_ids=tuple(row.check_id for row in report.results),
        not_executed=not_executed,
        reasons=tuple(
            f"{row.check_id}: {row.reason}" for row in report.results if int(row.repetitions) <= 0
        ),
    )


def build_evidence(
    report: Optional[E2EReport],
    *,
    repo_path: str = "",
    content_digest: str = "",
    declaration_digest: str = "",
    session_id: str = "",
    environment: Optional[Mapping[str, Any]] = None,
    reuse: Optional[Mapping[str, Any]] = None,
    started_at: float = 0.0,
    tool_versions: Optional[Mapping[str, str]] = None,
    ledger: Optional[Mapping[str, Any]] = None,
) -> E2EEvidence:
    """Assemble the self-describing evidence for one report.

    Reads the commit by FILESYSTEM (:func:`execution.e2e_trust.read_commit`) and
    never inherits a caller-supplied one, so the anchor cannot be a value the
    caller got wrong in the direction that matters.

    ``environment`` defaults to ``{}`` rather than to a plausible fingerprint:
    an absent fingerprint means a reused row is not comparable, and
    :func:`ledger_gate` refuses on it. Inventing one would make every reuse
    decision look justified.
    """
    assert_five_states_distinct()
    rows = tuple(report.results) if report is not None else ()
    selection = _selection_from(report) if report is not None else Selection(0, 0, 0, 0)
    anchor = EvidenceAnchor(
        what_ran=tuple(row.check_id for row in rows),
        commit=read_commit(str(repo_path or "")),
        selection=selection,
        declaration_digest=str(declaration_digest or ""),
        content_digest=str(content_digest or ""),
        environment=dict(environment or {}),
        session_id=str(session_id or (report.session_id if report is not None else "")),
        repo_path=str(repo_path or ""),
        started_at=float(started_at or 0.0),
        duration_s=float(report.total_duration_s if report is not None else 0.0),
        tool_versions=dict(tool_versions or {}),
    )
    coverage = CoverageReceipt(
        selection=selection,
        policy=report.policy.policy if report is not None else "",
        policy_reason=report.policy.reason if report is not None else "",
        spec_status=report.spec_status if report is not None else "",
    )
    return E2EEvidence(
        anchor=anchor,
        rows=rows,
        coverage=coverage,
        verdict=report.verdict if report is not None else "no_report",
        policy=report.policy.to_dict() if report is not None else {},
        spec_status=report.spec_status if report is not None else "",
        spec_reason=report.spec_reason if report is not None else "",
        reuse=dict(reuse or {}),
        ledger=dict(ledger or {}),
    )


def build_reuse_evidence(
    report: Optional[E2EReport],
    *,
    repo_path: str = "",
    content_digest: str,
    stored_at: Any = None,
    matched_by: str = "",
    feature_id: str = "",
    environment: Optional[Mapping[str, Any]] = None,
) -> E2EEvidence:
    """The reused variant: the same object shape, marked distinctly.

    Deliberately a SEPARATE constructor rather than a flag on
    :func:`build_evidence`. The point of the brief's requirement 4 is that a
    reused result is *marked distinct* from a fresh run, and the strongest form
    of "distinct" is that it is built by a differently named function — so a
    reader of the call site sees which kind of object is being produced without
    reading the arguments.

    The mark itself comes from :func:`execution.e2e_trust.reuse_of`, whose
    ``fresh_evidence`` is hard-coded ``False``. That is what makes
    "reuse may save a re-run but never marks a feature passing" structural: the
    producer cannot emit the other value.
    """
    mark = reuse_of(
        content_digest=content_digest,
        stored_at=stored_at,
        matched_by=matched_by,
        commit=read_commit(str(repo_path or "")).commit,
        feature_id=feature_id,
        state=report.verdict if report is not None else "",
    )
    return build_evidence(
        report,
        repo_path=repo_path,
        content_digest=content_digest,
        session_id=report.session_id if report is not None else "",
        environment=environment,
        reuse=mark,
    )


# --------------------------------------------------------------------------
# The ledger gate — composed, not a second set of requirements
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class E2ELedgerVerdict:
    """The composed answer for T5: this module's half plus the delegated one."""

    provable: bool
    suggested_state: str
    e2e_met: Tuple[str, ...]
    e2e_unmet: Tuple[str, ...]
    e2e_reasons: Tuple[str, ...]
    unit_met: Tuple[str, ...] = ()
    unit_unmet: Tuple[str, ...] = ()
    unit_reasons: Tuple[str, ...] = ()
    cross_commit_checked: bool = False
    commit: str = ""

    @property
    def met(self) -> Tuple[str, ...]:
        """Both halves' requirements, namespaced so they cannot be confused."""
        return tuple(f"e2e:{name}" for name in self.e2e_met) + tuple(
            f"unit:{name}" for name in self.unit_met
        )

    @property
    def unmet(self) -> Tuple[str, ...]:
        return tuple(f"e2e:{name}" for name in self.e2e_unmet) + tuple(
            f"unit:{name}" for name in self.unit_unmet
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provable": bool(self.provable),
            "suggested_state": self.suggested_state,
            "met": list(self.met),
            "unmet": list(self.unmet),
            "e2e_met": list(self.e2e_met),
            "e2e_unmet": list(self.e2e_unmet),
            "e2e_reasons": list(self.e2e_reasons),
            "unit_met": list(self.unit_met),
            "unit_unmet": list(self.unit_unmet),
            "unit_reasons": list(self.unit_reasons),
            "cross_commit_checked": bool(self.cross_commit_checked),
            "commit": self.commit,
            "note": (
                "the unit half is DELEGATED to "
                "execution.verification_evidence.ledger_evidence, which already "
                "owns those eight requirements. This module adds only the "
                "end-to-end half and the cross-commit binding; it does not "
                "restate the unit requirements, because a second set of them is "
                "a second answer to 'may this feature be marked passing'"
            ),
        }


#: The end-to-end half's requirements. Published with a question each, so a
#: refusal is actionable rather than a boolean.
E2E_PROVABILITY: Tuple[Tuple[str, str], ...] = (
    (
        "e2e_fresh",
        "Did an end-to-end run happen for THIS commit of THIS tree, now?",
    ),
    (
        "e2e_committed",
        "Is the measurement anchored to a resolvable commit?",
    ),
    (
        "e2e_coverage_complete",
        "Did every DECLARED end-to-end check actually execute?",
    ),
    (
        "e2e_state_decided",
        "Is every declared check `passed`, with none blocked, not_run or "
        "untrustworthy?",
    ),
    (
        "e2e_environment_measured",
        "Was the environment fully measured, so a reused row is comparable?",
    ),
)


def ledger_gate(
    evidence: Any,
    *,
    result: Any = None,
    commit: str = "",
) -> E2ELedgerVerdict:
    """The composed ledger gate. **This is requirement 6.**

    Two halves and neither is reimplemented:

    * the **end-to-end half** is this module's five requirements, derived here;
    * the **unit half** is delegated to
      :func:`execution.verification_evidence.ledger_evidence`, which has owned
      those eight since Wave 1. A second set of unit requirements would be a
      second authority in the one place a second authority is most expensive.

    ``commit`` is the commit the caller is asking about. When supplied it is
    bound **structurally** via :func:`assert_same_commit`, which raises rather
    than returning a verdict — a cross-commit mismatch is a programming error
    in the caller, not a judgement call, and returning a verdict for it would
    invite a caller to ignore it.
    """
    ev_obj = evidence_of(evidence)
    if ev_obj is None:
        raise TypeError(
            "ledger_gate needs an E2EEvidence or a result carrying one; "
            "refusing to answer for an object with no evidence rather than "
            "inventing an empty verdict that reads as unmeasured"
        )
    if commit:
        assert_same_commit(ev_obj, commit)

    met: List[str] = []
    unmet: List[str] = []
    reasons: List[str] = []

    if ev_obj.fresh:
        met.append("e2e_fresh")
    else:
        unmet.append("e2e_fresh")
        reasons.append(
            "no end-to-end run happened for this state of this tree now; "
            + (
                str(ev_obj.reuse.get("summary") or "the row was reused")
                if ev_obj.reuse.get("reused")
                else "no freshness was declared, and a builder that does not "
                "state freshness must not produce evidence a gate accepts"
            )
        )

    if ev_obj.anchor.commit_known:
        met.append("e2e_committed")
    else:
        unmet.append("e2e_committed")
        reasons.append(
            f"the measurement is anchored to commit status "
            f"{ev_obj.anchor.commit.status!r}: "
            f"{ev_obj.anchor.commit.reason or 'unresolved'}. Evidence with no "
            f"commit is not evidence about a specific tree"
        )

    if ev_obj.coverage.may_report_passing:
        met.append("e2e_coverage_complete")
    else:
        unmet.append("e2e_coverage_complete")
        reasons.append(
            f"end-to-end coverage is {ev_obj.coverage.verdict!r} "
            f"({ev_obj.coverage.fraction}): declared="
            f"{ev_obj.coverage.selection.declared} executed="
            f"{ev_obj.coverage.selection.executed}. A fraction of 0 is "
            f"{COVERAGE_ZERO_MEANS}, never a pass"
        )

    undecided = [row for row in ev_obj.rows if not row.settled]
    if not ev_obj.rows:
        unmet.append("e2e_state_decided")
        reasons.append("no declared end-to-end check produced any row")
    elif undecided:
        unmet.append("e2e_state_decided")
        reasons.append(
            f"{len(undecided)} of {len(ev_obj.rows)} declared check(s) did not "
            f"settle: "
            + ", ".join(f"{row.check_id}={row.state}" for row in undecided[:6])
        )
    else:
        met.append("e2e_state_decided")

    unmeasured = list(ev_obj.anchor.environment.get("unmeasured_fields") or ())
    if ev_obj.anchor.environment.get("fully_measured"):
        met.append("e2e_environment_measured")
    elif ev_obj.anchor.environment:
        unmet.append("e2e_environment_measured")
        reasons.append(
            f"environment field(s) {', '.join(unmeasured)} were never measured, "
            f"so a reused row is not demonstrably comparable to this one"
        )
    else:
        unmet.append("e2e_environment_measured")
        reasons.append(
            "no environment fingerprint at all. 'Both unknown' is not 'the same', "
            "so nothing here can authorise reuse"
        )

    unit_met: Tuple[str, ...] = ()
    unit_unmet: Tuple[str, ...] = ()
    unit_reasons: Tuple[str, ...] = ()
    suggested = "evidence_complete" if not unmet else "evidence_incomplete"
    if result is not None:
        from execution.verification_evidence import ledger_evidence

        delegated = ledger_evidence(result)
        unit_met = tuple(delegated.met)
        unit_unmet = tuple(delegated.unmet)
        unit_reasons = tuple(delegated.reasons)
        if unit_unmet:
            suggested = str(delegated.suggested_state)
        elif not met:
            suggested = "unmeasured"
    elif not met and not unmet:  # pragma: no cover - unreachable guard
        suggested = "unmeasured"

    provable = not unmet and not unit_unmet
    if provable:
        suggested = "evidence_complete"
    elif "e2e_state_decided" in unmet and suggested == "evidence_complete":
        suggested = "unverified_behaviour"

    return E2ELedgerVerdict(
        provable=bool(provable),
        suggested_state=suggested,
        e2e_met=tuple(met),
        e2e_unmet=tuple(unmet),
        e2e_reasons=tuple(reasons),
        unit_met=unit_met,
        unit_unmet=unit_unmet,
        unit_reasons=unit_reasons,
        cross_commit_checked=bool(commit),
        commit=str(commit or ev_obj.anchor.commit_sha),
    )


# --------------------------------------------------------------------------
# Reading one off a result, the flake_gate.precedent
# --------------------------------------------------------------------------


def attach_evidence(result: Any, evidence: E2EEvidence) -> Any:
    """Attach to a ``VerificationResult`` as an ADDITIVE INSTANCE ATTRIBUTE.

    The :func:`execution.flake_gate.attach_evidence` precedent, and for the same
    reason: ``shared/types.py`` is another terminal's file, so the receipt rides
    an attribute rather than a dataclass field. The cost is that
    ``runtime/serialize.py`` does not round-trip it, so a replayed journal loses
    the evidence — filed, not worked around.
    """
    try:
        result.e2e_evidence = evidence
    except Exception:  # pragma: no cover - a slotted result is not fatal
        return result
    return result


def evidence_of(result: Any) -> Optional[E2EEvidence]:
    """Read the evidence off a result, or ``None`` when there is none.

    ``None`` rather than an empty :class:`E2EEvidence`, because "no evidence was
    attached" and "evidence was attached and measured nothing" are different
    facts and :func:`ledger_gate` must refuse the first rather than treat it as
    the second.
    """
    if isinstance(result, E2EEvidence):
        # An evidence object IS the evidence. Without this arm,
        # `ledger_gate(evidence)` refused the very object it was written for,
        # and the duplicated `isinstance` that replaced it was the scar.
        return result
    found = getattr(result, "e2e_evidence", None)
    return found if isinstance(found, E2EEvidence) else None


def render_evidence(evidence: Any) -> str:
    """Render an evidence object — or a result carrying one — as lines."""
    found = evidence_of(evidence)
    if found is None:
        if isinstance(evidence, E2EEvidence):
            found = evidence
        else:
            return (
                "no end-to-end evidence is attached to this result. That is NOT "
                "a passing verification: it means nobody ran a check that could "
                "fail, and the feature's behaviour is unverified."
            )
    return "\n".join(found.render())


def now() -> float:  # pragma: no cover - a named seam for the pins
    """One clock reader, so a pin can freeze time instead of mocking `time`."""
    return time.time()
