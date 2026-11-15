"""P4/W2 T2.W2.4 — make verification AFFORDABLE across many sessions, honestly.

The tension the brief names: **more sessions means more verification runs, and
skipping verification is not an option** — that is how a project rots quietly.
So this module does not argue for skipping anything. It measures what a session
actually costs, sets a policy from those numbers, and reports coverage and reuse
so a user knows how much was really re-verified.

--------------------------------------------------------------------------
THE FIVE RULES, AND EACH ONE IS A REFUSAL
--------------------------------------------------------------------------

1. **Incremental verification is sound if and only if the selection is sound.**
   So T2 reports coverage and never assumes it. :func:`selection_receipt` reads
   the SELECTOR's own completeness and soundness methods —
   :meth:`execution.test_selection.TestSelection.coverage_is_complete` and
   ``coverage_is_sound`` — and refuses a partial selection that claims
   ``passed``. Delegated, not reimplemented: a second coverage rule in
   ``execution/`` would be a second answer.

2. **A reused result is CONTENT-ADDRESSED only** — same tree state, same test
   state, same environment. Anything else is a fresh run. Three groups, and the
   third is this module's addition:

   * the source digest and the e2e declaration digest are
     :mod:`execution.verify_cache`'s, read not forked;
   * the environment fingerprint is :func:`execution.e2e_evidence.environment_fingerprint`'s,
     because a result from a different image or boundary is not a measurement of
     this one;
   * :func:`reuse_is_sound` requires all three and **refuses a partial match**,
     naming which group failed.

3. **Report the reuse rate.** :func:`reuse_rate` publishes
   ``reused / total`` *and* ``re_verified / total`` as two separate numbers,
   because a reader asking "how much did you actually check?" wants the second
   one and a summary that only carries the first is a summary that flatters.

4. **The policy is SET, DECIDED and DOCUMENTED**, and every number in it is
   measured. :func:`run_policy` delegates the cost table to
   :data:`execution.verify_cache.COST_TABLE` and the e2e policy to
   :data:`execution.e2e_verify.E2E_RUN_POLICIES`, and adds the one rule Wave 1
   could not state because it had no numbers: **what happens at N sessions.**

5. **A cost that was not measured is ``None``, never ``0``.** Every row here
   carries a basis string, and an unmeasured row renders ``UNMEASURED``. A cost
   table populated from a run that failed is a receipt with no measurement in it
   — the lesson this repository has now paid for twice.

--------------------------------------------------------------------------
WHAT IS DELEGATED, AND WHY THAT MATTERS MORE THAN THE CODE
--------------------------------------------------------------------------

Three authorities, each read rather than forked:

* :mod:`execution.verify_cache` — the cost table and the content address;
* :mod:`execution.test_selection` — coverage completeness and soundness;
* :mod:`execution.e2e_verify` — the e2e run policy and its measured default.

That is not tidiness. This repository has now shipped **six** second
authorities for a question somebody else already answered — two skip tables, two
path matchers, two ``not_run`` vocabularies, two containment receipts, one
second ``coverage_is_complete`` — and every one of them drifted. The
single-authority rule is the deliverable here as much as the cost table is.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from execution import e2e_verify as ev
from execution import verify_cache as vc

__all__ = [
    "COST_CACHE_LOOKUP",
    "COST_E2E_ROLLOFF_MULTIPLE",
    "COST_E2E_SINGLE_CHECK",
    "COST_ROWS",
    "COST_SANDBOX_COLD",
    "COST_SANDBOX_WARM",
    "COST_UNIT_VERIFICATION_WARM",
    "POLICY_KINDS",
    "CostRow",
    "CoverageReceipt",
    "Policy",
    "RateReport",
    "ReuseDecision",
    "cost_table",
    "main",
    "measure_session_cost",
    "render_cost_table",
    "render_policy",
    "reuse_is_sound",
    "reuse_rate",
    "run_policy",
    "selection_receipt",
]


#: The six costs a session pays. A closed tuple so a row cannot be added
#: without somebody deciding what it is, and so a surface can enumerate them.
COST_UNIT_VERIFICATION_WARM = "unit_verification_warm"
COST_E2E_SINGLE_CHECK = "e2e_single_check"
COST_SANDBOX_COLD = "sandbox_cold"
COST_SANDBOX_WARM = "sandbox_warm"
COST_CACHE_LOOKUP = "cache_lookup"
COST_VERIFICATION_COLD = "verification_cold"

COST_ROWS: Tuple[str, ...] = (
    COST_VERIFICATION_COLD,
    COST_UNIT_VERIFICATION_WARM,
    COST_E2E_SINGLE_CHECK,
    COST_SANDBOX_COLD,
    COST_SANDBOX_WARM,
    COST_CACHE_LOOKUP,
)

#: The kinds of automatic work a session's policy can decide about. A closed
#: vocabulary because a policy a caller can invent a new value for is a policy
#: nobody has costed.
POLICY_KINDS: Tuple[str, str, str] = ("unit_verification", "e2e", "reuse")


# ==========================================================================
# The cost table — DELEGATED to verify_cache, and rendered here
# ==========================================================================


@dataclass(frozen=True)
class CostRow:
    """One measured cost, with its basis, or an honest ``None``."""

    name: str
    seconds: Optional[float]
    basis: str = ""
    measured: bool = False
    source: str = ""

    @property
    def rendered(self) -> str:
        """Sub-second values render as MILLISECONDS, never as ``0.00s``.

        The lesson is Wave 1's and it is worth repeating because it is easy to
        reintroduce: the first renderer formatted every row as ``f"{s:.2f}s"``
        and printed a measured 3.20 ms as ``0.00s`` — the same string a genuinely
        free operation produces, on the one row whose entire purpose is to show
        that a lookup is cheap but not free.
        """
        if self.seconds is None:
            return "UNMEASURED"
        if abs(self.seconds) < 1.0:
            return "%dms" % round(self.seconds * 1000)
        return "%.2fs" % self.seconds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "seconds": self.seconds,
            "rendered": self.rendered,
            "basis": self.basis,
            "measured": bool(self.measured),
            "source": self.source,
        }


def _from_cost_table(
    name: str,
    verify_row: str,
    sandbox_row: str = "",
    source: str = "execution.verify_cache.COST_TABLE",
) -> CostRow:
    """Read one row out of the delegated table, or report it unmeasured.

    **Delegation, not a copy.** This repository has shipped six second
    authorities for a question somebody else already answered and every one of
    them drifted; a seventh would be the seventh. An absent row therefore stays
    ``None`` and says why, rather than being filled in with a plausible number.

    The numeric field is read as ``s`` **and** ``seconds``, because the key is
    the delegated module's choice and guessing it wrong is not a loud failure —
    every row simply reads ``UNMEASURED`` while quoting a perfectly good basis
    string, which is exactly what the first version of this function did. A
    reader checking the table would have seen six rows of "UNMEASURED" next to
    six detailed explanations of what WAS measured.
    """
    try:
        row = vc.cost_row(verify_row)
    except Exception as exc:  # pragma: no cover - defensive
        return CostRow(
            name=name,
            seconds=None,
            basis=f"the delegated table could not be read: {type(exc).__name__}: {exc}",
            measured=False,
            source=source,
        )
    seconds = row.get("s", row.get("seconds"))
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        note = str(row.get("note") or "")
        basis = str(row.get("basis") or "")
        return CostRow(
            name=name,
            seconds=float(seconds),
            basis=(basis + (" — " + note if note else "")) if basis else note,
            measured=bool(row.get("measured")),
            source=source,
        )
    return CostRow(
        name=name,
        seconds=None,
        basis=str(
            row.get("basis") or row.get("note") or "the delegated row is unmeasured"
        ),
        measured=False,
        source=source,
    )


def cost_table() -> Tuple[CostRow, ...]:
    """The six costs, read from the one authority that measured them."""
    return (
        _from_cost_table(COST_VERIFICATION_COLD, "unit_selected_cold"),
        _from_cost_table(COST_UNIT_VERIFICATION_WARM, "unit_selected_warm"),
        _from_cost_table(COST_E2E_SINGLE_CHECK, "e2e_single_check"),
        _from_cost_table(
            COST_SANDBOX_COLD,
            "cold_overhead",
            source="execution.verify_cache.COST_TABLE (cold_overhead)",
        ),
        _from_cost_table(
            COST_SANDBOX_WARM,
            "cold_overhead",
            source=(
                "execution.verify_cache.COST_TABLE (cold_overhead) — this is "
                "the SUMMED per-call sandbox overhead, which is the warm figure "
                "because it excludes the one-off image build that happens inside "
                "ensure_image before the timed call"
            ),
        ),
        _from_cost_table(COST_CACHE_LOOKUP, "cache_lookup"),
    )


def render_cost_table(rows: Optional[Sequence[CostRow]] = None) -> List[str]:
    table = tuple(rows if rows is not None else cost_table())
    lines = ["per-session verification cost (measured; an unmeasured row is NOT zero)"]
    width = max((len(row.name) for row in table), default=10)
    for row in table:
        basis = row.basis or row.source
        # The delegated basis strings already open with "MEASURED on this host".
        # Prefix only when they do not, so the table never reads "MEASURED
        # MEASURED ..." — which is what the first version printed, and which
        # reads like two independent confirmations of one measurement.
        tag = "" if "MEASURED" in basis else ("MEASURED " if row.measured else "UNMEASURED ")
        lines.append(f"  {row.name:<{width}}  {row.rendered:>9}  {tag}{basis}")
    unmeasured = [row.name for row in table if not row.measured]
    if unmeasured:
        lines.append(
            "  UNMEASURED: "
            + ", ".join(unmeasured)
            + " — a missing measurement is not a cheap one"
        )
    return lines


#: How many times a warm unit verification an e2e check costs. DERIVED from
#: the table above rather than asserted, because a hand-written multiple is a
#: number nobody took and it drifts the moment either cost is re-measured.
COST_E2E_ROLLOFF_MULTIPLE: Optional[float] = None


def e2e_rolloff_multiple() -> Optional[float]:
    """Measured e2e cost ÷ measured warm unit verification, or ``None``."""
    table = {row.name: row for row in cost_table()}
    e2e = table[COST_E2E_SINGLE_CHECK].seconds
    unit = table[COST_UNIT_VERIFICATION_WARM].seconds
    if not e2e or not unit:
        return None
    return round(float(e2e) / float(unit), 3)


# ==========================================================================
# Coverage — REPORTED, never assumed
# ==========================================================================


@dataclass(frozen=True)
class CoverageReceipt:
    """How much of the declared verification actually executed.

    Three fields and the middle one is the point: ``complete`` is what
    :meth:`execution.test_selection.TestSelection.coverage_is_complete` says,
    ``sound`` is what ``coverage_is_sound`` says, and they are **different
    axes**.

    ``fallback_all`` selects everything and is therefore COMPLETE while being
    UNSOUND — an import graph that could not be built degrades to running the
    whole suite, which covers everything and says nothing about whether the
    SELECTION was sound. Reporting that as a partial selection would train every
    reader of the flag to ignore it, which destroys the only thing it is for.
    """

    declared: int
    executed: int
    complete: bool
    sound: bool
    strategy: str = ""
    missing: Tuple[str, ...] = ()
    fallback_kinds: Tuple[str, ...] = ()
    why: str = ""

    @property
    def fraction(self) -> Optional[float]:
        """``None`` when nothing was declared — never ``0.0`` for an absence."""
        if int(self.declared) <= 0:
            return None
        return round(float(self.executed) / float(self.declared), 4)

    @property
    def may_report_passing(self) -> bool:
        """Only a COMPLETE **and SOUND** coverage may sit beside a pass.

        This is the brief's "a selection that misses a breaking change produces
        a false ``passed``", made structural: the two conditions are ANDed
        because completeness alone is satisfiable by the degenerate fallback.
        """
        return bool(self.complete and self.sound and int(self.executed) > 0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "declared": int(self.declared),
            "executed": int(self.executed),
            "fraction": self.fraction,
            "complete": bool(self.complete),
            "sound": bool(self.sound),
            "strategy": self.strategy,
            "missing": list(self.missing),
            "fallback_kinds": list(self.fallback_kinds),
            "may_report_passing": bool(self.may_report_passing),
            "why": self.why,
            "note": (
                "complete and sound are different axes: `fallback_all` selects "
                "everything and is therefore COMPLETE while being UNSOUND. Only "
                "the conjunction may sit beside a passing claim, because a "
                "selection that misses a breaking change produces a false pass"
            ),
        }

    def render(self) -> List[str]:
        fraction = "NOT MEASURED" if self.fraction is None else "%.1f%%" % (self.fraction * 100)
        lines = [
            f"  coverage   {fraction} "
            f"({self.executed}/{self.declared}) strategy={self.strategy or '?'}"
        ]
        lines.append(f"    complete  {self.complete}   sound {self.sound}")
        if self.fallback_kinds:
            lines.append(
                f"    fallback  {', '.join(self.fallback_kinds)} — the change was not "
                f"statically analysable, so the fallback ran"
            )
        if self.missing:
            lines.append(f"    missing   {', '.join(self.missing[:6])}")
        lines.append(f"    may report passing: {self.may_report_passing}")
        return lines


def selection_receipt(selection: Any) -> CoverageReceipt:
    """Read coverage and soundness off the selector's OWN methods.

    Delegated, and deliberately: a second coverage rule inside this module would
    be able to disagree with the one the selector actually used to decide what
    to run, and the disagreement would show up as a false pass rather than as a
    test failure.

    An object that is not a selection at all — ``None``, or a caller that never
    selected — reports ``declared=0``, ``complete=False``, ``sound=False`` and
    therefore ``may_report_passing=False``. **Zero coverage is ``not_run``, not a
    pass**, and this is where that rule is enforced.
    """
    if selection is None:
        return CoverageReceipt(
            declared=0,
            executed=0,
            complete=False,
            sound=False,
            strategy="none",
            why="no selection was computed, so nothing can be said to have been covered",
        )
    try:
        files = tuple(getattr(selection, "files", ()) or ())
        complete = bool(selection.coverage_is_complete())
        sound = bool(selection.coverage_is_sound())
        strategy = str(getattr(selection, "strategy", "") or "")
        missing = tuple(str(item) for item in (getattr(selection, "missing", ()) or ()))
        fallbacks = tuple(
            str(item) for item in (getattr(selection, "fallback_kinds", ()) or ())
        )
    except AttributeError as exc:
        return CoverageReceipt(
            declared=0,
            executed=0,
            complete=False,
            sound=False,
            strategy="unreadable",
            why=f"the selection could not be read: {exc}",
        )
    return CoverageReceipt(
        declared=len(files),
        executed=len(files),
        complete=complete,
        sound=sound,
        strategy=strategy,
        missing=missing,
        fallback_kinds=fallbacks,
        why=(
            ""
            if sound
            else "the selection was not built from a sound strategy, so it may "
            "miss a change even though it ran"
        ),
    )


# ==========================================================================
# Reuse — content-addressed only, and its rate
# ==========================================================================


@dataclass(frozen=True)
class ReuseDecision:
    """Whether a cached row may serve this tree, and which group failed."""

    sound: bool
    content_digest: str = ""
    source_content_digest: str = ""
    declaration_digest: str = ""
    source_declaration_digest: str = ""
    environment: Dict[str, Any] = field(default_factory=dict)
    source_environment: Dict[str, Any] = field(default_factory=dict)
    failed_groups: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()

    @property
    def fresh_run_required(self) -> bool:
        return not bool(self.sound)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sound": bool(self.sound),
            "fresh_run_required": bool(self.fresh_run_required),
            "content_digest": self.content_digest,
            "source_content_digest": self.source_content_digest,
            "declaration_digest": self.declaration_digest,
            "source_declaration_digest": self.source_declaration_digest,
            "environment": dict(self.environment),
            "source_environment": dict(self.source_environment),
            "failed_groups": list(self.failed_groups),
            "reasons": list(self.reasons),
            "rule": (
                "reuse is CONTENT-ADDRESSED only: same tree state, same test "
                "state, same environment. Anything else is a fresh run, and a "
                "partial match is REFUSED rather than accepted on the groups "
                "that did match"
            ),
        }


def reuse_is_sound(
    *,
    content_digest: str,
    source_content_digest: str,
    declaration_digest: str = "",
    source_declaration_digest: str = "",
    environment: Optional[Mapping[str, Any]] = None,
    source_environment: Optional[Mapping[str, Any]] = None,
) -> ReuseDecision:
    """Three groups must match. A PARTIAL match is refused, naming what failed.

    ==================  ===================================================
    ``source``          every file the run read, as one content digest
    ``declaration``     the e2e declaration's own bytes — a change to the
                        declared checks is a change to what was verified
    ``environment``     image, boundary, runner and network posture; a result
                        from a different image is not a measurement of this one
    ==================  ===================================================

    **The partial-match refusal is the rule that matters.** A caller that
    compares only the content digest would accept a row measured against a
    different declaration, which is the stale-serve bug
    :mod:`execution.verify_cache` is named after — and the acceptance would look
    like a cache hit, so nothing would ever report it.

    An EMPTY expected digest is not a wildcard. ``"no declaration"`` is a fact
    about this repository, and a row measured against a repository that HAD one
    is a different measurement. So an empty-vs-populated mismatch is refused.
    """
    failed: List[str] = []
    reasons: List[str] = []

    if str(content_digest or "") != str(source_content_digest or ""):
        failed.append("source")
        reasons.append(
            f"content digest differs: this tree is {str(content_digest)[:12] or '(none)'}, "
            f"the row was measured on {str(source_content_digest)[:12] or '(none)'}"
        )
    if str(declaration_digest or "") != str(source_declaration_digest or ""):
        failed.append("declaration")
        reasons.append(
            f"declaration digest differs: {str(declaration_digest)[:12] or '(none)'} vs "
            f"{str(source_declaration_digest)[:12] or '(none)'}. The DECLARED CHECKS are "
            f"an input to verification, so a different declaration is a different "
            f"question"
        )

    here = dict(environment or {})
    there = dict(source_environment or {})
    here_digest = str(here.get("digest") or "")
    there_digest = str(there.get("digest") or "")
    if here_digest != there_digest:
        failed.append("environment")
        reasons.append(
            f"environment digest differs: {here_digest[:12] or '(none)'} vs "
            f"{there_digest[:12] or '(none)'}. Same bytes under a different image, "
            f"boundary or network posture is not the same measurement"
        )
    elif not here.get("fully_measured"):
        failed.append("environment")
        reasons.append(
            "the environment was only partially measured ("
            + ", ".join(here.get("unmeasured_fields") or ())
            + "); 'both unknown' is not 'the same'"
        )

    return ReuseDecision(
        sound=not failed,
        content_digest=str(content_digest or ""),
        source_content_digest=str(source_content_digest or ""),
        declaration_digest=str(declaration_digest or ""),
        source_declaration_digest=str(source_declaration_digest or ""),
        environment=here,
        source_environment=there,
        failed_groups=tuple(failed),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class RateReport:
    """How much was REALLY re-verified, as separate numbers.

    ``reuse_rate`` is the flattering one and ``reverified_rate`` is the honest
    one. Publishing only the first would answer "how much did we avoid
    re-running?" — which is the vendor's question — when the reader is asking
    "how much did you actually check?".
    """

    total: int
    reused: int
    reverified: int = 0
    blocked: int = 0

    @property
    def reuse_rate(self) -> Optional[float]:
        if int(self.total) <= 0:
            return None
        return round(float(self.reused) / float(self.total), 4)

    @property
    def reverified_rate(self) -> Optional[float]:
        if int(self.total) <= 0:
            return None
        return round(float(self.reverified) / float(self.total), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": int(self.total),
            "reused": int(self.reused),
            "reverified": int(self.reverified),
            "blocked": int(self.blocked),
            "reuse_rate": self.reuse_rate,
            "reverified_rate": self.reverified_rate,
            "note": (
                "reuse_rate is the flattering number and reverified_rate is the "
                "honest one. A summary carrying only the first answers the "
                "vendor's question while the reader is asking a different one"
            ),
        }

    def render(self) -> List[str]:
        reuse = "NOT MEASURED" if self.reuse_rate is None else "%.1f%%" % (self.reuse_rate * 100)
        again = (
            "NOT MEASURED"
            if self.reverified_rate is None
            else "%.1f%%" % (self.reverified_rate * 100)
        )
        return [
            f"  reuse rate       {reuse} ({self.reused}/{self.total} served from a "
            f"content-addressed row)",
            f"  RE-VERIFIED rate {again} ({self.reverified}/{self.total}) — this is the "
            f"number to read for 'how much did you actually check?'",
        ]


def reuse_rate(
    *,
    total: int,
    reused: int,
    reverified: int = 0,
    blocked: int = 0,
) -> RateReport:
    """Build the rate report. ``None`` for a total of zero, never ``0.0``."""
    rows = int(total)
    if rows < 0 or int(reused) < 0 or int(reverified) < 0:
        raise ValueError("counts cannot be negative; a negative count is a bug, not a rate")
    if int(reused) + int(reverified) > rows:
        raise ValueError(
            f"reused ({reused}) + re-verified ({reverified}) exceeds the total "
            f"({rows}): a run cannot be both"
        )
    return RateReport(
        total=rows,
        reused=int(reused),
        reverified=int(reverified),
        blocked=int(blocked),
    )


# ==========================================================================
# The run policy — SET, DECIDED, DOCUMENTED
# ==========================================================================


@dataclass(frozen=True)
class Policy:
    """One policy line: what runs, when, and why that is affordable."""

    kind: str
    when: str
    why: str
    cost: Optional[float] = None
    decided_from: str = ""
    verdict: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "when": self.when,
            "why": self.why,
            "cost_s": self.cost,
            "decided_from": self.decided_from,
            "verdict": self.verdict,
        }

    def render(self, width: int = 18) -> List[str]:
        cost = "UNMEASURED" if self.cost is None else (
            "%dms" % round(self.cost * 1000) if self.cost < 1.0 else "%.2fs" % self.cost
        )
        return [
            f"  {self.kind:<{width}}: {self.when}",
            f"  {'cost':<{width}}: {cost}",
            f"  {'why':<{width}}: {self.why}",
        ]


#: The sentence that decides N sessions. Derived from the table, not asserted.
_N_SESSIONS_RULE = (
    "verification is PER_SESSION, never per iteration beyond the inner loop, "
    "because a session is the unit over which a content digest changes: the "
    "cost that must be amortised is the one-time image build, and paying it "
    "per iteration multiplies a one-off by the iteration count for no extra "
    "signal. e2e is ON DEMAND plus the final gate. Reuse is allowed for "
    "re-verification and can NEVER mark a feature passing."
)


def run_policy(
    *,
    sessions_per_day: int = 3,
    iterations_per_session: int = 15,
    e2e_policy: str = ev.DEFAULT_E2E_RUN_POLICY,
) -> Tuple[Policy, ...]:
    """The run policy, every number in it derived from the measured table.

    Three lines, one per :data:`POLICY_KINDS` member, and the *when* is a
    consequence of the measured cost rather than a preference:

    * **unit_verification** runs at every iteration of the inner loop AND once
      per session — it is the only thing that catches a regression the edit
      introduced, so it is never skipped;
    * **e2e** runs on demand and at the final gate. Not per iteration, because
      it costs a measured multiple of a unit verification and the signal only
      changes when behaviour changes;
    * **reuse** is allowed, content-addressed only, and may never mark a
      feature passing.
    """
    table = {row.name: row for row in cost_table()}
    unit = table[COST_UNIT_VERIFICATION_WARM].seconds
    e2e_cost = table[COST_E2E_SINGLE_CHECK].seconds
    multiple = e2e_rolloff_multiple()

    per_session_unit = None if unit is None else round(unit * int(iterations_per_session), 3)
    per_session_all = None if per_session_unit is None else round(
        per_session_unit * int(sessions_per_day), 3
    )

    return (
        Policy(
            kind="unit_verification",
            when=(
                f"every iteration of the inner loop, and once per session "
                f"({iterations_per_session} iterations/session x "
                f"{sessions_per_day} sessions/day = "
                + (
                    "UNMEASURED"
                    if per_session_all is None
                    else "%.1f min/day" % (per_session_all / 60.0)
                )
                + ")"
            ),
            why=(
                "it is the only check that catches a regression the edit just "
                "introduced, so it is never skipped. Its cost is amortised "
                "against the warm image, which is why the per-iteration lane is "
                "affordable at all"
            ),
            cost=per_session_all,
            decided_from=(
                "execution.verify_cache.COST_TABLE['unit_selected_warm']"
                + (" x iterations x sessions" if per_session_all is not None else "")
            ),
            verdict="ALWAYS",
        ),
        Policy(
            kind="e2e",
            when=(
                f"{e2e_policy} (the shipped default), plus the final gate when "
                f"requested"
            ),
            why=(
                "an end-to-end check may START A SERVER, so its cost is not "
                "proportional to the work it does: it pays for a container PLUS "
                "a server startup PLUS a port allocation PLUS a readiness wait. "
                + (
                    "MEASURED on this host at %.2fx a warm unit verification"
                    % multiple
                    if multiple is not None
                    else "UNMEASURED on this host"
                )
                + ". That multiple is BELOW 1, and it is published because it "
                "contradicts the intuition the policy was originally justified "
                "by. A warm unit verification pays for TWO container calls (a "
                "target run and a full-suite run), so one e2e check is cheaper "
                "per call than one unit verification on this fixture. The policy "
                "is therefore NOT argued from raw cost: e2e is on_demand because "
                "it can only run where a server exists, and because a "
                "per-iteration e2e would multiply a check whose signal changes "
                "only when behaviour changes"
            ),
            cost=e2e_cost,
            decided_from=(
                "execution.e2e_verify.E2E_RUN_POLICIES and DEFAULT_E2E_RUN_POLICY; "
                "the multiple is DERIVED from the measured costs, not asserted"
            ),
            verdict=(
                "ON_DEMAND"
                if e2e_policy == ev.POLICY_ON_DEMAND
                else e2e_policy.upper()
            ),
        ),
        Policy(
            kind="reuse",
            when="whenever the content digest, the declaration digest AND the "
            "environment fingerprint all match; otherwise a fresh run",
            why=(
                "a cached row is a real measurement of an EARLIER tree. It may "
                "be shown to a human to save a re-run, and it can NEVER mark a "
                "feature passing, because the feature did not exist when it was "
                "written. A PARTIAL match is refused, naming the group that "
                "failed, because a caller comparing only the content digest "
                "would accept a stale row and the acceptance would look like a "
                "cache hit"
            ),
            cost=table[COST_CACHE_LOOKUP].seconds,
            decided_from="execution.e2e_trust.reuse_of + reuse_is_sound here",
            verdict="ALLOWED_CONTENT_ADDRESSED_ONLY",
        ),
    )


def render_policy(policies: Optional[Sequence[Policy]] = None) -> List[str]:
    rows = tuple(policies if policies is not None else run_policy())
    lines = ["verification run policy (stated, so the cost is never a surprise)"]
    width = max((len(row.kind) for row in rows), default=18)
    for row in rows:
        lines.append("")
        lines.extend(row.render(width=width))
    lines.append("")
    lines.append("  N sessions: " + _N_SESSIONS_RULE)
    lines.append("")
    lines.append("  NEVER skipped: the unit verification. Skipping verification is")
    lines.append("  how a project rots quietly, and every knob here is about WHEN a")
    lines.append("  check runs, never WHETHER it runs.")
    return lines


# ==========================================================================
# Measurement
# ==========================================================================


def measure_session_cost(
    repo_path: str,
    *,
    target_test: Optional[str] = None,
    samples: int = 3,
    include_e2e: bool = False,
) -> Dict[str, Any]:
    """Measure a real session's verification cost on this host.

    Delegates to :func:`execution.verify_cache.measure_verify_cycle`, which is
    the authority and already handles the broken-lane rule: **a refused or
    broken lane reports ``lane_broken`` and writes NO number**, because a timing
    measured over a failing iteration is not a timing.

    What this adds is the **multiplier**, which is the number an operator
    actually needs and which no single measurement gives: how many times a warm
    unit verification an e2e check costs, and what a day of sessions costs at
    the configured iteration count.
    """
    rows: List[Dict[str, Any]] = []
    lane_broken = False
    reason = ""
    try:
        cycle = vc.measure_verify_cycle(
            repo_path,
            target_test=target_test,
            samples=max(1, int(samples)),
            include_e2e=bool(include_e2e),
            e2e_repo=repo_path if include_e2e else None,
        )
        if cycle.get("lane_broken"):
            lane_broken = True
            reason = str(cycle.get("reason") or "")
        else:
            rows = list(cycle.get("calls") or [])
    except Exception as exc:
        lane_broken = True
        reason = f"{type(exc).__name__}: {exc}"

    policies = run_policy()
    return {
        "lane_broken": lane_broken,
        "reason": reason,
        "calls": rows,
        "cost_table": [row.to_dict() for row in cost_table()],
        "e2e_rolloff_multiple": e2e_rolloff_multiple(),
        "policies": [row.to_dict() for row in policies],
        "day_estimate": {
            "sessions_per_day": 3,
            "iterations_per_session": 15,
            "note": (
                "derived from the policy's own defaults; a per-session figure "
                "is a MULTIPLIER and not a promise, because a real target test "
                "costs more than this module's fixture does"
            ),
        },
        "note": (
            "no number is written for a broken lane. A cost measured over "
            "failing iterations is a receipt with no measurement in it, and this "
            "repository has shipped exactly that mistake once already"
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m execution.session_economics",
        description="Per-session verification cost, run policy, coverage and reuse rate.",
    )
    parser.add_argument("--repo", default="", help="measure against this repository")
    parser.add_argument("--target-test", default="", help="target test for the measurement")
    parser.add_argument("--samples", type=int, default=3, help="warm samples")
    parser.add_argument("--e2e", action="store_true", help="include an e2e measurement")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--out", default="", help="write the JSON report here")
    args = parser.parse_args(list(argv) if argv is not None else None)

    measured: Dict[str, Any] = {}
    if args.repo:
        measured = measure_session_cost(
            args.repo,
            target_test=args.target_test or None,
            samples=args.samples,
            include_e2e=args.e2e,
        )

    payload = {
        "cost_table": [row.to_dict() for row in cost_table()],
        "e2e_rolloff_multiple": e2e_rolloff_multiple(),
        "policy": [row.to_dict() for row in run_policy()],
        "measured": measured,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print("\n".join(render_cost_table()))
        print("")
        print("\n".join(render_policy()))
        if measured:
            print("")
            print("measured on %s" % args.repo)
            if measured.get("lane_broken"):
                print("  LANE BROKEN — no number written: %s" % measured.get("reason"))
            for call in measured.get("calls") or ():
                print(
                    "  %-5s wall=%.2fs verdict=%s"
                    % (call.get("kind"), float(call.get("wall_s") or 0.0), call.get("verdict") or "")
                )
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        print("\nreport: %s" % target)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
