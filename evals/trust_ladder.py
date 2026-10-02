"""The Daily Trust Ladder - the project's primary metric, as a scorecard.

WHAT THIS IS
------------
The product goal is *"an agent I can use daily and genuinely trust on real
repositories."* That needs an executable definition, because trust that is
not measured is a feeling. Ten rungs, one per guarantee a user would
actually state:

===  ==================================================================
 #   Guarantee
===  ==================================================================
 1   Never mutates outside declared roots without approval
 2   Never reports success without verifier evidence
 3   Never silently loses or corrupts an edit
 4   Recovers from a crash mid-edit
 5   Recovers from a hung tool / doom loop
 6   Stays inside budget
 7   Understands the project on day 2
 8   Stays coherent past 50 turns          <- Phase 1 (executable)
 9   Fast enough to use daily              <- Phase 1 (executable)
10   You can always see what it did and why <- Phase 1 (executable)
===  ==================================================================

THE FOUR STATUSES, AND WHY THERE IS NO FIFTH
--------------------------------------------
``pass`` | ``fail`` | ``blocked`` (+ reason) | ``not_implemented`` (+ owner)

``skip`` is absent on purpose and **cannot be constructed**: in a summary
table a skip renders exactly like a pass, so a blocked lane, an unconfigured
provider and a green run all become the same glyph. ``_status`` rejects the
string outright, and ``tests/test_trust_ladder.py`` pins that.

WHICH RUNGS ARE REAL
--------------------
Rungs #8, #9 and #10 are **measured**, by the probes in
``evals/trust_ladder_rungs.py``, and each is capable of reporting ``fail``.
The other seven are **carried**, from gates and suites that already measure
them or record why they cannot run - and every carried row says which, so a
reader is never left to guess whether a row was executed.

The honest consequence, stated at the top of every report: a carried rung is
a *pointer*, not a measurement. Only #8, #9 and #10 were re-measured here,
and the three of them are red on the current tree. That is the finding.

WHAT THIS DOES NOT ESTABLISH
----------------------------
:data:`NOT_ESTABLISHED` is carried in the JSON, printed by the CLI, and
asserted non-empty. The most important entry: **every model call in every
rung is a scripted double.** No live provider was reachable (T3 recorded
``ServiceUnavailableError: No available channel`` on 3/3 live completions),
so no number here is a claim about model quality, model cost, or model
latency.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# the vocabulary -- closed, and `skip` is not in it
# --------------------------------------------------------------------------

PASS = "pass"
FAIL = "fail"
BLOCKED = "blocked"
NOT_IMPLEMENTED = "not_implemented"

STATUSES: Tuple[str, ...] = (PASS, FAIL, BLOCKED, NOT_IMPLEMENTED)

#: Verdicts the ladder itself emits. Distinct from ``STATUSES``: the ladder's
#: own health is a different question from any single rung's status.
LADDER_MEASURED = "MEASURED_RED"
LADDER_MEASURED_GREEN = "MEASURED_GREEN"
LADDER_PARTIAL = "PARTIAL"

_FORBIDDEN: Tuple[str, ...] = (
    "skip",
    "skipped",
    "xfail",
    "pending",
    "todo",
    "n/a",
    "na",
)


class LadderError(RuntimeError):
    """Raised when a rung cannot be stated honestly.

    In practice: a ``blocked`` rung with no reason, or a ``not_implemented``
    rung with no owning phase. Both are refused **at construction**, so the
    dishonest row cannot be written rather than merely discouraged.
    """


def _status(value: str) -> str:
    """Validate one status string. Refuses ``skip`` and every near-synonym."""
    text = str(value or "").strip()
    if text.lower() in _FORBIDDEN:
        raise LadderError(
            f"{value!r} is not a permitted ladder status. A skip is "
            "indistinguishable from a pass in a summary table. Use `blocked` "
            "with the exact reason, or `not_implemented` with the owning phase."
        )
    if text not in STATUSES:
        raise LadderError(f"{value!r} is not in the closed vocabulary {STATUSES!r}")
    return text


# --------------------------------------------------------------------------
# the rung
# --------------------------------------------------------------------------


@dataclass
class Rung:
    """One row of the ladder.

    :param number: 1-10, the stable identifier a phase prompt cites.
    :param guarantee: the user's sentence, in the user's words.
    :param status: one of :data:`STATUSES`, validated on construction.
    :param detail: the finding, in prose. For ``blocked`` this MUST carry the
        exact reason; for ``not_implemented`` the owning phase is mandatory.
    :param owner: who closes it, ``T1 / P1.1`` style.
    :param evidence: the command, file or probe that produced the row.
    :param mode: ``"measured"`` when this ladder executed the check, or
        ``"carried"`` when it points at another gate. A carried row is a
        pointer; saying so is the difference between a scorecard and a
        decoration.
    :param measured: the raw measurement document, when there is one.
    """

    number: int
    guarantee: str
    status: str
    detail: str
    owner: str = ""
    evidence: str = ""
    mode: str = "carried"
    blocking: bool = True
    measured: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = _status(self.status)
        if not str(self.guarantee).strip():
            raise LadderError(f"rung {self.number}: a rung needs a guarantee")
        if not str(self.detail).strip():
            raise LadderError(
                f"rung {self.number}: a rung with no detail is an assertion. "
                "Say what happened."
            )
        if self.status == BLOCKED and not str(self.detail).strip():
            raise LadderError(f"rung {self.number}: blocked must carry the reason")
        if self.status == NOT_IMPLEMENTED and not str(self.owner).strip():
            raise LadderError(
                f"rung {self.number}: not_implemented must name the OWNING "
                "PHASE in `owner`, so a reader knows whether to wait or to build"
            )
        if self.mode not in ("measured", "carried"):
            raise LadderError(
                f"rung {self.number}: mode must be 'measured' or 'carried', got "
                f"{self.mode!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "number": self.number,
            "id": f"rung_{self.number:02d}",
            "guarantee": self.guarantee,
            "status": self.status,
            "mode": self.mode,
            "detail": self.detail,
            "owner": self.owner,
            "evidence": self.evidence,
            "blocking": self.blocking,
        }
        if self.measured:
            out["measured"] = self.measured
        return out


def blocked(number: int, guarantee: str, reason: str, **kw: Any) -> Rung:
    """A rung that could not run. ``reason`` is mandatory and non-empty."""
    if not str(reason).strip():
        raise LadderError(f"rung {number}: blocked() requires the exact reason")
    return Rung(number, guarantee, BLOCKED, reason, **kw)


def not_implemented(number: int, guarantee: str, owning_phase: str, **kw: Any) -> Rung:
    """A rung that does not exist yet. ``owning_phase`` is mandatory.

    Any ``reason=`` keyword is APPENDED to the detail rather than discarded.
    The first version of this function accepted a ``reason`` and threw it
    away, so a caller could write three sentences explaining why rung #7 has
    no check and the report printed only "does not exist yet" -- which reads
    as a stub rather than as a recorded gap. A ``not_implemented`` rung is the
    one row whose whole job is to carry its reason.
    """
    if not str(owning_phase).strip():
        raise LadderError(f"rung {number}: not_implemented() requires the owning phase")
    kw.setdefault("owner", owning_phase)
    detail = f"does not exist yet; owned by {owning_phase}"
    reason = str(kw.pop("reason", "") or "").strip()
    if reason:
        detail = f"{detail}. {reason}"
    return Rung(number, guarantee, NOT_IMPLEMENTED, detail, **kw)


# --------------------------------------------------------------------------
# the three measured rungs
# --------------------------------------------------------------------------

R8_GUARANTEE = "Stays coherent past 50 turns"
R9_GUARANTEE = "Fast enough to use daily"
R10_GUARANTEE = "You can always see what it did and why"


def rung8(*, probe: Optional[Callable[[], Any]] = None, **probe_kw: Any) -> Rung:
    """Rung #8, measured. Never green unless every sub-check held.

    The sub-checks, and why each exists:
    ``>=50 turns of monotonic progress``
        "The run stops" is the HANDLED failure. The unhandled one is silent
        degradation: redoing completed work while still reporting progress.
    ``constraint decay (plant turn 2, check turn 45)``
        The corpus documents compliance falling 73% -> 33% between turn 5 and
        turn 16. A run can complete 50 turns perfectly and still have
        forgotten the rule it was given, which is what this checks.
    ``turn cap declared >= 50 AND reported before it binds``
        A cap the user cannot see coming is not a budget, it is a cliff.
    """
    from evals import trust_ladder_rungs as probes

    try:
        result = probe() if probe else probes.probe_rung8(**probe_kw)
    except Exception as exc:
        return blocked(
            8,
            R8_GUARANTEE,
            f"the probe did not produce a measurement: {type(exc).__name__}: {exc}",
            owner="T5 (the probe itself is broken; not a product verdict)",
            evidence="evals.trust_ladder_rungs.probe_rung8",
            mode="measured",
        )

    doc = result.to_dict()
    if result.failures:
        return Rung(
            8,
            R8_GUARANTEE,
            FAIL,
            "ran and broke. " + " | ".join(result.failures),
            owner="T1 / P1.1 (turn caps + agent-loop constraint re-injection)",
            evidence="evals.trust_ladder_rungs.probe_rung8",
            mode="measured",
            measured=doc,
        )
    return Rung(
        8,
        R8_GUARANTEE,
        PASS,
        f"ran and held: {result.turns_executed} turns, {result.edits_applied} "
        f"distinct real edits, cap {result.declared_turn_cap} declared and "
        f"announced, and the turn-{2} constraint was still governing at turn 45 "
        f"(visible on {result.constraint_visible_turns} turns).",
        owner="T1",
        evidence="evals.trust_ladder_rungs.probe_rung8",
        mode="measured",
        measured=doc,
    )


def rung9(*, probe: Optional[Callable[[], Any]] = None, **probe_kw: Any) -> Rung:
    """Rung #9, measured. Every timing carries machine + window size.

    The control arm matters as much as the number: without it, "177 s" could
    be a slow machine or a slow walk, and those have different owners.
    """
    from evals import trust_ladder_rungs as probes

    try:
        result = probe() if probe else probes.probe_rung9(**probe_kw)
    except Exception as exc:
        return blocked(
            9,
            R9_GUARANTEE,
            f"the probe did not produce a measurement: {type(exc).__name__}: {exc}",
            owner="T5 (the probe itself is broken; not a product verdict)",
            evidence="evals.trust_ladder_rungs.probe_rung9",
            mode="measured",
        )

    doc = result.to_dict()
    lines = [t.describe() for t in result.timings]
    unavailable = [t for t in result.timings if not t.available]
    if result.failures:
        return Rung(
            9,
            R9_GUARANTEE,
            FAIL,
            "ran and broke.\n  - "
            + "\n  - ".join(result.failures)
            + "\n  measured:\n  - "
            + "\n  - ".join(lines),
            owner="T1 / P1.1 (harness/retrieval.py::_SKIP_DIRS) + T3 / P1.3 (import path)",
            evidence="evals.trust_ladder_rungs.probe_rung9",
            mode="measured",
            measured=doc,
        )
    if unavailable:
        # The guarantee is a CONJUNCTION of four budgets. A conjunct that could
        # not be measured has not been shown to hold, so the rung is `blocked`
        # with the exact reason -- never `pass`. This is the one place in this
        # file where a missing measurement could otherwise have produced a
        # green row, and it is asserted by
        # `tests/test_trust_ladder.py::test_rung9_cannot_pass_with_an_unavailable_metric`.
        return blocked(
            9,
            R9_GUARANTEE,
            "part of the guarantee could not be MEASURED, so the rung is "
            "blocked rather than green. "
            + " | ".join(f"{t.name}: {t.reason}" for t in unavailable),
            owner="T5 / the measurement lane (the probe's own inputs, not a "
            "product verdict)",
            evidence="evals.trust_ladder_rungs.probe_rung9",
            mode="measured",
        )
    return Rung(
        9,
        R9_GUARANTEE,
        PASS,
        "ran and held:\n  - " + "\n  - ".join(lines),
        owner="T1 / T3",
        evidence="evals.trust_ladder_rungs.probe_rung9",
        mode="measured",
        measured=doc,
    )


def rung10(*, probe: Optional[Callable[[], Any]] = None, **probe_kw: Any) -> Rung:
    """Rung #10, measured, link by link.

    A rung that says "visibility is bad" is not a rung. This one names WHICH
    link dropped the information, because the four links have four different
    owners and four different fixes.
    """
    from evals import trust_ladder_rungs as probes

    try:
        result = probe() if probe else probes.probe_rung10(**probe_kw)
    except Exception as exc:
        return blocked(
            10,
            R10_GUARANTEE,
            f"the probe did not produce a measurement: {type(exc).__name__}: {exc}",
            owner="T5 (the probe itself is broken; not a product verdict)",
            evidence="evals.trust_ladder_rungs.probe_rung10",
            mode="measured",
        )

    doc = result.to_dict()
    if result.failures:
        return Rung(
            10,
            R10_GUARANTEE,
            FAIL,
            "ran and broke.\n  - " + "\n  - ".join(result.failures),
            owner="T4 / P1.4 (cli/runview.py, cli/tui_components.py)",
            evidence="evals.trust_ladder_rungs.probe_rung10",
            mode="measured",
            measured=doc,
        )
    return Rung(
        10,
        R10_GUARANTEE,
        PASS,
        f"ran and held: all four links carry the run, {result.event_count} "
        f"events -> {result.facts_key_count} run facts -> a rendered card, and "
        f"a truncated search and an absent cost are both visibly marked.",
        owner="T4",
        evidence="evals.trust_ladder_rungs.probe_rung10",
        mode="measured",
        measured=doc,
    )


# --------------------------------------------------------------------------
# the carried rungs
# --------------------------------------------------------------------------
#
# These point at a gate or a suite that owns the measurement. They are NOT
# re-measured here and the row says so. A carried row is a pointer; the
# `evidence` field names where the number is, and `mode="carried"` means a
# reader must go there for the number rather than take it from here.
#
# Rung 7 is the one carried row that is `not_implemented`, and it is the only
# rung in the ladder with no gate behind it at all.


def _g0_row(root: Path) -> Dict[str, Any]:
    """Read the G0 gate's report, if one has been published. Never probes."""
    candidate = Path(root) / "logs" / "gates" / "g0_report.json"
    if candidate.is_file():
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _g0_rung(g0: Dict[str, Any], rung_id: str) -> Optional[Dict[str, Any]]:
    for row in g0.get("rungs") or []:
        if row.get("id") == rung_id:
            return row
    return None


def carried_rows(root: Path) -> List[Rung]:
    """Rungs #1-#7, each naming the gate that owns its measurement."""
    g0 = _g0_row(root)
    out: List[Rung] = []

    def from_g0(
        number: int,
        guarantee: str,
        rung_id: str,
        owner: str,
        fallback_owner: str,
    ) -> Rung:
        row = _g0_rung(g0, rung_id)
        if row is None:
            return blocked(
                number,
                guarantee,
                f"no published G0 report carries a `{rung_id}` row, so this "
                f"rung's measurement is not available here. Run "
                f"`python -m evals.gates.P0` and publish "
                f"logs/gates/g0_report.json. This is reported as blocked, not "
                f"as a pass and not as zero.",
                owner=fallback_owner,
                evidence="logs/gates/g0_report.json",
            )
        status = _status(str(row.get("status") or BLOCKED))
        detail = (
            str(row.get("detail") or "").strip() or "the gate row carried no detail"
        )
        return Rung(
            number,
            guarantee,
            status,
            detail,
            owner=str(row.get("owner") or owner),
            evidence=f"evals.gates.P0::{rung_id} (published G0 report)",
            mode="carried",
            blocking=bool(row.get("blocking", True)),
        )

    out.append(
        from_g0(
            1,
            "Never mutates outside declared roots without approval",
            "docker_daemon_reachable",
            "T2 / P2",
            "T2 / P2",
        )
    )
    out.append(
        from_g0(
            3,
            "Never silently loses or corrupts an edit",
            "known_failing_pin_registry",
            "T1 / P2",
            "T1 / P2",
        )
    )
    out.append(
        from_g0(
            4,
            "Recovers from a crash mid-edit",
            "full_suite_tests_dir",
            "T1 / P2",
            "T1 / P2",
        )
    )
    out.append(
        from_g0(
            5,
            "Recovers from a hung tool / doom loop",
            "full_suite_module_local",
            "T1 / T2",
            "T1 / T2",
        )
    )
    out.append(
        from_g0(
            6,
            "Stays inside budget",
            "security_blockers",
            "T3 / P2",
            "T3 / P2",
        )
    )
    # Rung 2 is the verifier gate. It is pinned by a source-level test rather
    # than by a runtime lane, so it is carried from the pin registry's own
    # green statement rather than from a G0 row.
    out.append(
        Rung(
            2,
            "Never reports success without verifier evidence",
            PASS,
            "carried: `harness/agent_loop_step.py` contains no string literal "
            "equal to `success` outside its docstrings, enforced by a "
            "source-level pin, and the mint site is a declared verifier "
            "returning clean evidence. See harness/test_mint_site_pins.py.",
            owner="T1 (untouchable; DOCTRINE.md §2)",
            evidence="harness/test_mint_site_pins.py",
            mode="carried",
        )
    )
    out.append(
        not_implemented(
            7,
            "Understands the project on day 2",
            "T1 / P3a (memory recall across sessions)",
            reason=(
                "no gate measures cross-session recall yet; the only "
                "measurement that would answer it is the P3a long-session "
                "eval, which does not exist. This is `not_implemented`, NOT a "
                "pass: a guarantee with no check is exactly the gap a ladder "
                "exists to make visible."
            ),
        )
    )
    out.sort(key=lambda r: r.number)
    return out


# --------------------------------------------------------------------------
# what this does not establish
# --------------------------------------------------------------------------

#: Printed by the CLI and carried in the JSON, because a verdict read without
#: its limits is a different claim from the one being made.
NOT_ESTABLISHED: Tuple[str, ...] = (
    "EVERY MODEL CALL IN EVERY RUNG IS A SCRIPTED DOUBLE. No live provider "
    "was reachable: T3 recorded `ServiceUnavailableError: No available "
    "channel` on 3/3 live completions. Nothing in this ladder measures model "
    "quality, model cost, or model latency. It measures the SHELL, the "
    "retrieval path, and the projection chain.",
    "Only rungs #8, #9 and #10 were MEASURED by this run. Rungs #1-#6 are "
    "CARRIED - they point at a published G0 row or a source-level pin, and a "
    "carried row is a pointer, not a measurement. A carried row that has no "
    "published G0 report behind it is reported `blocked`, never `pass`.",
    "Rung #9's retrieval number is measured on THIS repository on THIS "
    "machine, and the machine is named with it. It is a claim about one "
    "checkout, not about the product on an arbitrary host. The ladder also "
    "runs a CONTROL arm -- the same walk over a repository whose noise all "
    "sits in directories the skip list covers -- and a PER-PHASE attribution, "
    "so a ratio is never reported as a diagnosis on its own. A control that "
    "does not isolate the bottleneck yields NO claimed cause, and the report "
    "says so.",
    "A p95 is reported only when the window is at least "
    "MIN_SAMPLES_FOR_P95 observations. Below that the headline is labelled a "
    "median and the percentile is WITHHELD, with the window printed. A p95 "
    "over 3 samples is not a p95 and this ladder will not print one.",
    "No Docker lane and no live-provider lane was run by this ladder. Where a "
    "row says `blocked`, the exact reason is in the row.",
)


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------


def ladder_report(
    root: Path = REPO_ROOT,
    *,
    run_measured: bool = True,
    measured_only: bool = False,
    probe8: Optional[Callable[[], Any]] = None,
    probe9: Optional[Callable[[], Any]] = None,
    probe10: Optional[Callable[[], Any]] = None,
    probe8_kw: Optional[Dict[str, Any]] = None,
    probe9_kw: Optional[Dict[str, Any]] = None,
    probe10_kw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the ten-rung report.

    :param run_measured: set False to build the seven carried rows only. The
        measured rungs are then reported ``not_implemented`` with the owning
        phase named - never ``pass``, because a probe that did not run has
        measured nothing.
    :param measured_only: build only the three measured rungs, with the rest
        reported as omitted. Used by the fast CI lane.
    :param probe8_kw / probe9_kw / probe10_kw: per-probe keyword arguments.
        Separate dictionaries rather than one ``**kwargs``, because the three
        probes take different options and forwarding one probe's option to
        another turned a measured rung into a ``blocked`` probe-crash on the
        first run of this file.
    """
    root = Path(root)
    rungs: List[Rung] = []
    if not measured_only:
        rungs.extend(carried_rows(root))

    if run_measured:
        rungs.append(rung8(probe=probe8, **(probe8_kw or {})))
        rungs.append(rung9(probe=probe9, **(probe9_kw or {})))
        rungs.append(rung10(probe=probe10, **(probe10_kw or {})))
    else:
        # `mode="carried"`, NOT "measured": these probes did not run, so
        # listing them under `measured_rungs` would put an unexecuted check on
        # a list headed MEASURED. A rung that was not run has measured nothing,
        # and `not_implemented` + owning phase is the honest row for it.
        for number, guarantee, owner in (
            (8, R8_GUARANTEE, "T1 / P1.1"),
            (9, R9_GUARANTEE, "T1 + T3 / P1.1 + P1.3"),
            (10, R10_GUARANTEE, "T4 / P1.4"),
        ):
            rungs.append(
                not_implemented(
                    number,
                    guarantee,
                    f"{owner} -- the executable probe in "
                    f"evals/trust_ladder_rungs.py was NOT run for this report",
                    evidence="evals.trust_ladder_rungs",
                    mode="carried",
                )
            )
    rungs.sort(key=lambda r: r.number)
    numbers = [r.number for r in rungs]
    if numbers != list(range(1, 11)) and not measured_only:
        raise LadderError(f"the ladder must carry all ten rungs; got {numbers!r}")

    blocking_fail = [r for r in rungs if r.blocking and r.status == FAIL]
    measured_rows = [r for r in rungs if r.mode == "measured"]
    measured_fail = [r for r in measured_rows if r.status == FAIL]
    blocked_rows = [r for r in rungs if r.status == BLOCKED]
    if measured_fail:
        verdict = LADDER_MEASURED
    elif measured_rows:
        verdict = LADDER_MEASURED_GREEN
    else:
        verdict = LADDER_PARTIAL

    return {
        "schema_version": 1,
        "ladder": "daily-trust-ladder",
        "generated_at": round(time.time(), 3),
        "statuses": list(STATUSES),
        "verdict": verdict,
        "verdict_meaning": (
            "MEASURED_RED = at least one rung was MEASURED and it broke. This "
            "is the designed outcome for a tree whose guarantees are not yet "
            "met, and it is NOT the same as a failure of the measurement."
        ),
        "counts": {
            PASS: sum(1 for r in rungs if r.status == PASS),
            FAIL: sum(1 for r in rungs if r.status == FAIL),
            BLOCKED: len(blocked_rows),
            NOT_IMPLEMENTED: sum(1 for r in rungs if r.status == NOT_IMPLEMENTED),
        },
        "rungs": [r.to_dict() for r in rungs],
        "measured_rungs": [r.number for r in measured_rows],
        "carried_rungs": [r.number for r in rungs if r.mode == "carried"],
        "blocking_failures": [
            {"number": r.number, "id": f"rung_{r.number:02d}", "owner": r.owner}
            for r in blocking_fail
        ],
        "what_this_does_not_establish": list(NOT_ESTABLISHED),
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

_GLYPH = {PASS: "pass  ", FAIL: "FAIL  ", BLOCKED: "BLOCK ", NOT_IMPLEMENTED: "NOTIMPL"}


def render(report: Dict[str, Any]) -> str:
    """Render the ladder as the table a reader of a red CI run needs."""
    lines: List[str] = [
        f"Daily Trust Ladder: {report['verdict']}",
        "",
        # The vocabulary is printed in the header, not just used in the rows,
        # so a reader is told up front that `skip` is not one of the options.
        "status vocabulary: " + " | ".join(report["statuses"]),
        "mode: MEASURED = this run executed the check; CARRIED = the row "
        "points at another gate (a pointer, not a number).",
        "",
        f"{'#':>3} {'status':8} {'mode':9} guarantee",
        "-" * 104,
    ]
    for row in report["rungs"]:
        lines.append(
            f"{row['number']:>3} {_GLYPH[row['status']]:8} "
            f"{row['mode']:9} {row['guarantee']}"
        )
        for chunk in str(row["detail"]).splitlines():
            if chunk.strip():
                lines.append(f"{'':>3} {'':8} {'':9}   {chunk.strip()}")
        if row.get("owner"):
            lines.append(f"{'':>3} {'':8} {'':9}   owner: {row['owner']}")
    counts = report["counts"]
    lines += [
        "",
        "counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()),
        "measured here: " + ", ".join(str(n) for n in report["measured_rungs"]),
        "carried:       " + ", ".join(str(n) for n in report["carried_rungs"]),
        "",
        "WHAT THIS LADDER DOES NOT ESTABLISH:",
    ]
    for item in report["what_this_does_not_establish"]:
        lines.append(f"  * {item}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the ladder. Exit 2 when a MEASURED rung is red.

    A ``blocked`` or ``not_implemented`` row does NOT by itself make the exit
    code 2: blocked is an honest report, not a failure, and the point of the
    vocabulary is that a blocked lane is VISIBLE rather than disguised. The
    decision of whether a blocked row may ship is the reader's, made from a
    table that says so.
    """
    parser = argparse.ArgumentParser(
        prog="python -m evals.trust_ladder",
        description="The Daily Trust Ladder. Ten rungs, four statuses, never skip.",
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument(
        "--carried-only",
        action="store_true",
        help="build the seven carried rungs without running the three probes",
    )
    parser.add_argument(
        "--measured-only",
        action="store_true",
        help="build only the three measured rungs (the fast CI lane)",
    )
    parser.add_argument(
        "--no-retrieval",
        action="store_true",
        help="skip the retrieval walk in rung #9 (it is the slow probe)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = ladder_report(
        Path(args.root),
        run_measured=not args.carried_only,
        measured_only=bool(args.measured_only),
        # Only rung #9 takes a retrieval switch, and it goes to rung #9 alone.
        probe9_kw={"run_retrieval": not args.no_retrieval},
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 2 if report["verdict"] == LADDER_MEASURED else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
