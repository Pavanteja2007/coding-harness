"""R2-02 — the repetition layer that makes the flake gate capable of FIRING.

## The defect this module exists to close

One number was carrying two unrelated meanings. ``rerun_for_flake_check``
was simultaneously "how many times to run the target" and "do we look for
flakes at all", so the two collapsed into each other: with the shipped
default of ``1``, :func:`execution.verify.verify` executed exactly one target
run, ``len(set(outcomes)) > 1`` was unsatisfiable, and ``flaky`` was
structurally incapable of being ``True``. A test that passed once and failed
on the third run was reported as a clean, non-flaky pass. The gate was
present in the vocabulary and unreachable in the code.

The fix is to stop conflating them:

* **A baseline runs once.** Its only question is "was this already broken
  before we touched anything?", which one run answers, and it is the most
  expensive place to spend repetitions because it is pure overhead on every
  single task. :data:`DEFAULT_BASELINE_REPETITIONS` is 1 and stays 1.
* **Flake detection is a property of the POST-FIX run** — the run that mints
  a completion claim. Its default is :data:`DEFAULT_POST_FIX_REPETITIONS`,
  which is 2, the smallest number of observations that can distinguish
  "stable" from "not stable". One run cannot; two can.

## The three-valued outcome, preserved

``pass`` / ``fail`` / ``timeout``, detected timeout-first. A timeout is not a
pass and not a flake: it is its own outcome, so a pass-then-hang mix is
:data:`FLAKE_DETECTED` rather than a stable pass, and a run that times out
every single time is :data:`NOT_FLAKY` with ``timed_out=True`` — consistently
broken, not intermittently broken. The label constants are re-exported from
:mod:`execution.result_parsing` rather than re-spelled, so there is exactly
one vocabulary in the tree.

## Why ``not_run`` exists as a third verdict

``flaky=False`` reads as "we checked and it was stable". With one repetition
that is a lie: nothing was checked. :func:`flake_verdict` therefore returns a
THREE-valued ``flake_check``:

===========================  =========  ==============================
``flake_check``             ``flaky``  meaning
===========================  =========  ==============================
:data:`FLAKE_DETECTED`      ``True``   >=1 differing outcome observed
:data:`NOT_FLAKY`           ``False``  >=2 repetitions, all identical
:data:`NOT_RUN`             ``False``  fewer than 2 repetitions observed
===========================  =========  ==============================

``flaky`` stays a plain bool because every existing consumer reads it that
way, and for a single run ``False`` is the same value it has always had (so
nothing is weakened). ``flake_check`` is what a consumer must read to tell
"stable" from "unchecked", and :attr:`FlakeVerdict.detection_possible` is the
convenience form. **A consumer that wants to claim a test was shown to be
stable must require ``detection_possible``**, otherwise it is reporting an
absence of evidence as evidence.

The verdict is also fail-closed against its own caller: the repetition count
that backs it is the number of outcomes actually *observed*, never the number
merely requested. A caller that asks for 3 repetitions but supplies 1 outcome
gets :data:`NOT_RUN`, so a bookkeeping bug can never manufacture a
"not_flaky" verdict out of a single observation.

## Cost

Repetitions are wall-clock linear: every extra repetition is one more full
sandboxed target run at up to ``verify_timeout_s``. That is why the baseline
is pinned at one run, why :data:`MAX_REPETITIONS` is a hard ceiling a caller
can tighten but not exceed by accident, and why the measured cost is reported
rather than guessed — see ``execution/AGENTS.md`` (R2-02) for the measured
wall-time delta on this machine.

## Not wired at the time of writing

This module is the surface, not the call sites. ``execution/verify.py``,
``harness/core.py`` and ``harness/agent_loop.py`` were owned by another
terminal in the same parallel group when this landed, so the wiring is a
filed handoff (``execution/AGENTS.md``, "R2-02 cross-terminal requests")
rather than an applied edit. **Until those three call sites land, the default
configuration still cannot fire this gate** — see the same section. The
honest statement of this round is: the gate is now expressible, testable and
measurable; it is not yet on the live path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from execution.result_parsing import (
    OUTCOME_ERROR,
    OUTCOME_FAIL,
    OUTCOME_NO_TESTS,
    OUTCOME_PASS,
    OUTCOME_TIMEOUT,
    TIMEOUT_EXIT_CODES,
    parse_test_run,
)

#: Grouped by role for reading, sorted for the linter: the outcome vocabulary
#: (re-exported from :mod:`execution.result_parsing`, so there is one
#: vocabulary in the tree), the verdict vocabulary, the repetition policy, the
#: types, then the functions.
__all__ = [
    "DEFAULT_BASELINE_REPETITIONS",
    "DEFAULT_POST_FIX_REPETITIONS",
    "FLAKE_CHECK_VALUES",
    "FLAKE_DETECTED",
    "MAX_REPETITIONS",
    "MIN_REPETITIONS_FOR_DETECTION",
    "NOT_FLAKY",
    "NOT_RUN",
    "OUTCOME_ERROR",
    "OUTCOME_FAIL",
    "OUTCOME_NO_TESTS",
    "OUTCOME_PASS",
    "OUTCOME_TIMEOUT",
    "REPETITION_CONFIG_KEYS",
    "STAGE_BASELINE",
    "STAGE_POST_FIX",
    "TIMEOUT_EXIT_CODES",
    "FlakeObservation",
    "FlakeRun",
    "FlakeVerdict",
    "RepetitionResolution",
    "attach_evidence",
    "classify_run",
    "evaluate_repetitions",
    "evidence_of",
    "flake_verdict",
    "observe_repetitions",
    "outcome_label",
    "render_receipt",
    "repetitions_for_stage",
    "resolve_repetitions",
    "verdict_for",
]


# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

#: At least one repetition produced a different outcome than another.
FLAKE_DETECTED = "flaky_detected"
#: Two or more repetitions, every observed outcome identical.
NOT_FLAKY = "not_flaky"
#: Fewer than MIN_REPETITIONS_FOR_DETECTION repetitions were observed, so
#: stability was never tested. NEVER a claim of stability.
NOT_RUN = "not_run"

#: The complete ``flake_check`` vocabulary, closed so a consumer can reject
#: anything outside it instead of treating an unknown value as stable.
FLAKE_CHECK_VALUES: Tuple[str, ...] = (FLAKE_DETECTED, NOT_FLAKY, NOT_RUN)

#: Two observations are the minimum that can distinguish "stable" from
#: "not stable". One observation cannot, by definition.
MIN_REPETITIONS_FOR_DETECTION = 2

#: Post-fix / final-gate repetitions. 2 is the smallest value that can fire,
#: chosen so the gate is on by default without being a cost event.
DEFAULT_POST_FIX_REPETITIONS = 2

#: Baseline repetitions. A baseline only asks "was this already broken?",
#: which one run answers; the extra runs would be pure per-task overhead.
DEFAULT_BASELINE_REPETITIONS = 1

#: Hard ceiling on repetitions. Wall-clock is linear in this number, and each
#: repetition can cost up to ``verify_timeout_s``, so a mistyped config value
#: cannot buy an arbitrarily long verification. A caller may tighten it.
MAX_REPETITIONS = 10

#: Stage names for :func:`repetitions_for_stage`.
STAGE_BASELINE = "baseline"
STAGE_POST_FIX = "post_fix"

#: Which config key holds the repetition count for each stage.
#:
#: ``baseline_reruns`` predates this module and is read at post-fix call sites
#: today, which is the conflation being fixed. The post-fix stage therefore
#: prefers ``post_fix_reruns`` and falls back to ``baseline_reruns`` so a
#: config that only knows the old key still resolves to one number rather than
#: silently to a different one.
REPETITION_CONFIG_KEYS: Dict[str, str] = {
    STAGE_POST_FIX: "post_fix_reruns",
    STAGE_BASELINE: "baseline_reruns",
}

#: The legacy key, consulted only when the post-fix key is absent.
_LEGACY_REPETITION_KEY = "baseline_reruns"


# --------------------------------------------------------------------------
# outcome labelling
# --------------------------------------------------------------------------


def outcome_label(exit_code: int, timed_out: bool) -> str:
    """Return the three-valued outcome label for one finished test run.

    Assumes ``exit_code`` is the runner's process status and ``timed_out`` is
    True iff the run exceeded its timeout. Timeout is decided FIRST and
    unconditionally — before any pass/fail reasoning and regardless of the
    exit code — because a hung test is neither a pass nor a plain failure.
    The result is exactly one of :data:`OUTCOME_PASS`, :data:`OUTCOME_FAIL`,
    :data:`OUTCOME_TIMEOUT`, :data:`OUTCOME_NO_TESTS` or
    :data:`OUTCOME_ERROR`.

    This is the low-level labeller; :func:`classify_run` is the one a
    verification loop should call, because it routes the non-timeout case
    through the report-first parser.
    """
    if timed_out:
        return OUTCOME_TIMEOUT
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        return OUTCOME_ERROR
    if code in TIMEOUT_EXIT_CODES:
        return OUTCOME_TIMEOUT
    if code == 0:
        return OUTCOME_PASS
    if code == 5:  # pytest's documented "no tests collected"
        return OUTCOME_NO_TESTS
    if code in (2, 3, 4):
        return OUTCOME_ERROR
    return OUTCOME_FAIL


def classify_run(result: Any, *, expected_tests: Optional[int] = None) -> str:
    """Label one ``ExecutionResult`` (or ``TestRunReport``) for the flake gate.

    Assumes ``result`` is a :class:`shared.types.ExecutionResult` as returned
    by :func:`execution.sandbox.execute_sandboxed`, or an already-parsed
    ``TestRunReport``. ``expected_tests`` is forwarded to
    :func:`execution.result_parsing.parse_test_run` and should be 1 for a
    single-target run so a run that collected nothing is ``no_tests`` rather
    than a pass.

    The timeout branch is identical to :func:`outcome_label` and is evaluated
    first, so the two agree by construction on the case that matters most.
    """
    if result is None:
        return OUTCOME_ERROR
    existing = getattr(result, "outcome", None)
    if isinstance(existing, str) and existing:
        # Already a TestRunReport (or something shaped like one).
        if existing == OUTCOME_TIMEOUT or bool(getattr(result, "timed_out", False)):
            return OUTCOME_TIMEOUT
        if existing == OUTCOME_PASS:
            return OUTCOME_PASS
        if existing in (OUTCOME_FAIL, OUTCOME_NO_TESTS, OUTCOME_ERROR):
            return existing
        return OUTCOME_ERROR
    exit_code = getattr(result, "exit_code", 1)
    timed_out = bool(getattr(result, "timed_out", False))
    if timed_out or exit_code in TIMEOUT_EXIT_CODES:
        return OUTCOME_TIMEOUT
    report = parse_test_run(result, expected_tests=expected_tests)
    if report.passed:
        return OUTCOME_PASS
    if report.outcome in (OUTCOME_NO_TESTS, OUTCOME_ERROR):
        return report.outcome
    return OUTCOME_FAIL


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FlakeVerdict:
    """The auditable outcome of a repetition series.

    ``flake_check`` is the three-valued verdict; ``flaky`` is the historical
    boolean projection of it, kept because every existing consumer reads that
    name. ``repetitions`` is the number of outcomes ACTUALLY observed and
    ``observed_outcomes`` is the per-repetition label vector in order, so a
    reviewer can recompute the verdict from the receipt alone.

    Invariant, asserted by :meth:`check`: ``repetitions == len(observed_outcomes)``
    and ``repetitions >= 1`` for any non-``not_run`` verdict, and
    ``not_run`` whenever ``repetitions < MIN_REPETITIONS_FOR_DETECTION``.
    """

    flake_check: str
    repetitions: int
    observed_outcomes: Tuple[str, ...] = ()
    distinct_outcomes: Tuple[str, ...] = ()
    timed_out: bool = False
    requested_repetitions: int = 0
    notes: Tuple[str, ...] = ()

    @property
    def flaky(self) -> bool:
        """True only when at least one differing outcome was observed."""
        return self.flake_check == FLAKE_DETECTED

    @property
    def detection_possible(self) -> bool:
        """True when stability was actually tested (>=2 repetitions observed)."""
        return self.flake_check != NOT_RUN

    @property
    def not_run(self) -> bool:
        """True when the flake check could not be attempted at all."""
        return self.flake_check == NOT_RUN

    def check(self) -> List[str]:
        """Return the reasons this verdict is not self-consistent ([] = fine).

        Exposed so a caller can assert its own receipt rather than trusting a
        construction site it does not own. A :data:`NOT_RUN` verdict is the one
        shape allowed to rest on zero observations, because "nothing was
        observed" is precisely what it reports.
        """
        problems: List[str] = []
        if self.flake_check not in FLAKE_CHECK_VALUES:
            problems.append(f"unknown flake_check {self.flake_check!r}")
        if self.flake_check != NOT_RUN and self.repetitions < 1:
            problems.append("a verdict must rest on at least one observation")
        if self.repetitions != len(self.observed_outcomes):
            problems.append(
                f"repetitions={self.repetitions} but "
                f"{len(self.observed_outcomes)} outcomes were recorded"
            )
        if self.flake_check == NOT_RUN and self.repetitions >= (
            MIN_REPETITIONS_FOR_DETECTION
        ):
            problems.append(
                f"{self.repetitions} repetitions were observed, so the check "
                f"was not 'not_run'"
            )
        if self.flake_check in (FLAKE_DETECTED, NOT_FLAKY) and self.repetitions < (
            MIN_REPETITIONS_FOR_DETECTION
        ):
            problems.append(
                f"{self.repetitions} repetition(s) cannot support {self.flake_check!r}"
            )
        if self.flake_check == FLAKE_DETECTED and len(self.distinct_outcomes) < 2:
            problems.append("flaky_detected with fewer than 2 distinct outcomes")
        if self.flake_check == NOT_FLAKY and len(self.distinct_outcomes) > 1:
            problems.append("not_flaky with 2+ distinct outcomes")
        return problems

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for a trace or evidence record.

        Carries ``repetitions`` and ``observed_outcomes`` so the claim is
        auditable from disk without re-running anything.
        """
        return {
            "flake_check": self.flake_check,
            "flaky": self.flaky,
            "repetitions": int(self.repetitions),
            "requested_repetitions": int(self.requested_repetitions),
            "observed_outcomes": list(self.observed_outcomes),
            "distinct_outcomes": list(self.distinct_outcomes),
            "timed_out": bool(self.timed_out),
            "detection_possible": self.detection_possible,
            "notes": list(self.notes),
        }


def flake_verdict(repetitions: int, outcomes: Sequence[str]) -> FlakeVerdict:
    """Decide the flake verdict for an observed repetition series.

    This is THE public entry point of the module and the function every call
    site (see ``execution/AGENTS.md``) should use instead of computing
    ``len(set(outcomes)) > 1`` inline.

    Assumes ``outcomes`` holds one :data:`OUTCOME_PASS` /
    :data:`OUTCOME_FAIL` / :data:`OUTCOME_TIMEOUT` (or
    :data:`OUTCOME_NO_TESTS` / :data:`OUTCOME_ERROR`) label per repetition, in
    the order the repetitions ran. ``repetitions`` is what the caller intended
    to run.

    The verdict is derived from the OBSERVED count, not the requested one:

    * fewer than :data:`MIN_REPETITIONS_FOR_DETECTION` observed outcomes ->
      :data:`NOT_RUN`. ``flaky`` is ``False``, but nothing was checked, so
      ``flaky: false`` must not be read as stability. This is the whole point:
      a one-run configuration can never claim to have found no flake.
    * two or more, with at least two distinct labels ->
      :data:`FLAKE_DETECTED`, ``flaky=True``. A timeout mixed with a pass or
      a fail is a difference, so a pass-then-hang is flaky, not a stable pass.
    * two or more, all identical -> :data:`NOT_FLAKY`. A run that timed out
      every time is consistently broken rather than intermittently broken, so
      it is ``not_flaky`` with ``timed_out=True`` — never a pass.

    A mismatch between ``repetitions`` and ``len(outcomes)`` is recorded in
    ``notes`` and resolved conservatively in favour of the smaller observed
    count, so a caller bug degrades to :data:`NOT_RUN` instead of inventing
    evidence. Never raises.
    """
    notes: List[str] = []
    labels = tuple(str(value) for value in (outcomes or ()))

    try:
        requested = int(repetitions)
    except (TypeError, ValueError):
        requested = len(labels)
        notes.append(
            f"repetitions={repetitions!r} is not an integer; "
            f"fell back to the {len(labels)} observed outcome(s)"
        )
    if requested < 0:
        notes.append(f"negative repetitions={requested} treated as absent")
        requested = len(labels)

    observed = len(labels)
    if observed == 0:
        return FlakeVerdict(
            flake_check=NOT_RUN,
            repetitions=0,
            observed_outcomes=(),
            distinct_outcomes=(),
            timed_out=False,
            requested_repetitions=requested,
            notes=tuple([*notes, "no outcome was observed, so nothing was checked"]),
        )
    if requested != observed:
        notes.append(
            f"{requested} repetition(s) were requested but {observed} outcome(s) "
            f"were observed; the verdict rests on the observed count"
        )

    # Observed count wins: a missing observation can only ever reduce what we
    # know, never manufacture a clean bill of health.
    effective = observed
    distinct: Tuple[str, ...] = ()
    seen: List[str] = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    distinct = tuple(seen)
    timed_out = OUTCOME_TIMEOUT in labels

    if effective < MIN_REPETITIONS_FOR_DETECTION:
        notes.append(
            f"only {effective} repetition(s) observed; flake detection requires "
            f">= {MIN_REPETITIONS_FOR_DETECTION}, so stability was NOT tested"
        )
        return FlakeVerdict(
            flake_check=NOT_RUN,
            repetitions=effective,
            observed_outcomes=labels,
            distinct_outcomes=distinct,
            timed_out=timed_out,
            requested_repetitions=requested,
            notes=tuple(notes),
        )

    if len(distinct) > 1:
        return FlakeVerdict(
            flake_check=FLAKE_DETECTED,
            repetitions=effective,
            observed_outcomes=labels,
            distinct_outcomes=distinct,
            timed_out=timed_out,
            requested_repetitions=requested,
            notes=tuple(notes),
        )
    return FlakeVerdict(
        flake_check=NOT_FLAKY,
        repetitions=effective,
        observed_outcomes=labels,
        distinct_outcomes=distinct,
        timed_out=timed_out,
        requested_repetitions=requested,
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------
# observation: actually running the repetitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FlakeObservation:
    """What the repetitions produced, before any verdict is drawn.

    ``results`` keeps the raw per-repetition objects (an
    ``ExecutionResult``, a ``TestRunReport``, or whatever the caller's runner
    returned) so a caller can still build its own ``raw_output`` transcript
    after the fact. ``elapsed_s`` is the measured wall time of the whole
    series, which is the number a cost claim needs.
    """

    repetitions: int
    observed_outcomes: Tuple[str, ...] = ()
    results: Tuple[Any, ...] = ()
    elapsed_s: float = 0.0
    per_repetition_s: Tuple[float, ...] = ()
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible cost/outcome summary (no raw output)."""
        return {
            "repetitions": int(self.repetitions),
            "observed_outcomes": list(self.observed_outcomes),
            "elapsed_s": round(float(self.elapsed_s), 4),
            "per_repetition_s": [round(float(v), 4) for v in self.per_repetition_s],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class FlakeRun:
    """A completed repetition series: the observation plus its verdict."""

    verdict: FlakeVerdict
    observation: FlakeObservation

    @property
    def flaky(self) -> bool:
        """Convenience projection of ``verdict.flaky``."""
        return self.verdict.flaky

    @property
    def last_result(self) -> Any:
        """The final repetition's raw result (what a single-run caller read).

        Preserves the historical "the target result is the LAST run" rule
        exactly, so switching a call site to this module cannot change which
        run decides ``target_test_passed``.
        """
        return self.observation.results[-1] if self.observation.results else None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt combining cost and verdict."""
        payload = self.verdict.to_dict()
        payload.update(
            {
                "elapsed_s": round(float(self.observation.elapsed_s), 4),
                "per_repetition_s": [
                    round(float(v), 4) for v in self.observation.per_repetition_s
                ],
            }
        )
        return payload


def observe_repetitions(
    run_once: Callable[[int], Any],
    repetitions: int,
    *,
    label: Optional[Callable[[Any], str]] = None,
    expected_tests: Optional[int] = None,
) -> FlakeObservation:
    """Run ``run_once`` ``repetitions`` times and record what each run produced.

    Assumes ``run_once`` is a zero-argument callable that performs ONE target
    run and returns an ``ExecutionResult`` (or anything
    :func:`classify_run` understands). It is called with the repetition index
    so a caller can vary a per-run detail (a log line, a seed) if it wants,
    but the index is otherwise unused. ``label`` overrides the classifier and
    must return one of the outcome constants; it defaults to
    ``classify_run(result, expected_tests=expected_tests)``.

    ``repetitions`` is clamped to at least 1: a repetition series with zero
    runs has no outcomes and could only ever be :data:`NOT_RUN`. A
    ``run_once`` that raises is recorded as :data:`OUTCOME_ERROR` for that
    repetition and the series continues, because one broken repetition is
    itself evidence the caller needs, and raising out of here would lose the
    repetitions that already ran. Never raises.
    """
    labeller = label or (
        lambda value: classify_run(value, expected_tests=expected_tests)
    )
    notes: List[str] = []
    try:
        count = int(repetitions)
    except (TypeError, ValueError):
        notes.append(f"repetitions={repetitions!r} is not an integer; used 1")
        count = 1
    if count < 1:
        notes.append(f"repetitions={count} clamped to 1; a series needs one run")
        count = 1

    outcomes: List[str] = []
    results: List[Any] = []
    durations: List[float] = []
    for index in range(count):
        started = time.monotonic()
        try:
            result = run_once(index)
            computed = str(labeller(result))
        except Exception as exc:  # never lose the runs that already happened
            result = None
            computed = OUTCOME_ERROR
            notes.append(f"repetition {index + 1} raised {type(exc).__name__}: {exc}")
        durations.append(time.monotonic() - started)
        results.append(result)
        outcomes.append(computed)

    return FlakeObservation(
        repetitions=count,
        observed_outcomes=tuple(outcomes),
        results=tuple(results),
        elapsed_s=float(sum(durations)),
        per_repetition_s=tuple(durations),
        notes=tuple(notes),
    )


def verdict_for(observation: FlakeObservation) -> FlakeVerdict:
    """Return the :func:`flake_verdict` for an already-performed series.

    Assumes ``observation`` came from :func:`observe_repetitions`. The
    observation's own notes are merged into the verdict's, so a repetition
    that raised is visible in the receipt and not only in the caller.
    """
    verdict = flake_verdict(observation.repetitions, observation.observed_outcomes)
    if not observation.notes:
        return verdict
    return FlakeVerdict(
        flake_check=verdict.flake_check,
        repetitions=verdict.repetitions,
        observed_outcomes=verdict.observed_outcomes,
        distinct_outcomes=verdict.distinct_outcomes,
        timed_out=verdict.timed_out,
        requested_repetitions=verdict.requested_repetitions,
        notes=tuple(list(verdict.notes) + list(observation.notes)),
    )


def evaluate_repetitions(
    run_once: Callable[[int], Any],
    repetitions: int,
    *,
    label: Optional[Callable[[Any], str]] = None,
    expected_tests: Optional[int] = None,
) -> FlakeRun:
    """Run the series and return its observation together with its verdict.

    This is the single call a verification loop needs:

    >>> run = evaluate_repetitions(run_once, 2)          # doctest: +SKIP
    >>> run.verdict.flake_check, run.last_result          # doctest: +SKIP
    ('not_flaky', ExecutionResult(...))

    Assumes the same things as :func:`observe_repetitions` and
    :func:`flake_verdict`. ``run.last_result`` is still the final repetition,
    so a call site keeps the documented "the last target run decides
    ``target_test_passed``" rule while gaining a real flake verdict.
    """
    observation = observe_repetitions(
        run_once,
        repetitions,
        label=label,
        expected_tests=expected_tests,
    )
    return FlakeRun(verdict=verdict_for(observation), observation=observation)


# --------------------------------------------------------------------------
# repetition policy: how many runs, and who decided
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RepetitionResolution:
    """How many repetitions to run, and the provenance of that number.

    ``stage`` is the pipeline stage the number is for; ``source`` names where
    it came from (``"default"``, ``"config:<key>"``, ``"clamped"``,
    ``"invalid"``) so a receipt can say whether the value was chosen or
    inherited. ``notes`` carries the human-readable reasons.
    """

    stage: str
    repetitions: int
    source: str
    requested: Any = None
    ceiling: int = MAX_REPETITIONS
    notes: Tuple[str, ...] = ()

    @property
    def detection_possible(self) -> bool:
        """True when this repetition count can detect a flake at all."""
        return self.repetitions >= MIN_REPETITIONS_FOR_DETECTION

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible policy receipt."""
        return {
            "stage": self.stage,
            "repetitions": int(self.repetitions),
            "requested": self.requested,
            "source": self.source,
            "ceiling": int(self.ceiling),
            "detection_possible": self.detection_possible,
            "notes": list(self.notes),
        }


def resolve_repetitions(
    value: Any,
    *,
    stage: str = STAGE_POST_FIX,
    default: int = DEFAULT_POST_FIX_REPETITIONS,
    ceiling: int = MAX_REPETITIONS,
) -> RepetitionResolution:
    """Turn a configured repetition count into the number of runs to perform.

    Assumes ``value`` is whatever came out of ``Task.config`` for the
    repetition key: it may be absent (``None``), an int, or an int-shaped
    string. Absent is resolved by KEY MEANING, not by truthiness — ``None``
    means "not configured" and takes ``default``, while an explicit ``0`` or
    ``1`` is a deliberate "one run, no detection" and is honoured as such.
    A truthiness check would collapse those two cases and silently re-enable
    detection a caller had switched off.

    ``0`` and ``1`` both resolve to 1 run (matching the historical
    ``rerun_for_flake_check`` contract) and produce a :data:`NOT_RUN` verdict
    once observed. ``bool`` is rejected rather than coerced, because
    ``int(True) == 1`` would silently disable the gate on a typo. The result
    is clamped down to ``ceiling``: wall-clock is linear in this number and
    each repetition can cost a full ``verify_timeout_s``, so a mistyped
    config cannot buy an unbounded verification — and the clamp is recorded in
    ``notes`` and ``source``, never applied silently.

    Raises ``ValueError`` for a value that is not an int, an int-shaped
    string, or ``None``. That is a configuration bug, not a runtime
    condition, and a loud failure is the correct response to it.
    """
    notes: List[str] = []

    try:
        cap = int(ceiling)
    except (TypeError, ValueError):
        cap = MAX_REPETITIONS
    if cap < 1:
        notes.append(
            f"ceiling={ceiling!r} is not a positive int; used {MAX_REPETITIONS}"
        )
        cap = MAX_REPETITIONS
    cap = min(cap, MAX_REPETITIONS)

    if value is None:
        try:
            count = int(default)
        except (TypeError, ValueError):
            count = DEFAULT_POST_FIX_REPETITIONS
        count = max(1, min(count, cap))
        return RepetitionResolution(
            stage=stage,
            repetitions=count,
            source="default",
            requested=None,
            ceiling=cap,
            notes=tuple(notes),
        )

    if isinstance(value, bool):
        raise ValueError(
            f"{stage} repetitions must be an int, not a bool "
            f"(got {value!r}); int(True) == 1 would silently disable flake "
            f"detection"
        )
    if isinstance(value, str):
        try:
            requested = int(value.strip())
        except (TypeError, ValueError):
            raise ValueError(
                f"{stage} repetitions must be an int-shaped string, got {value!r}"
            ) from None
    elif isinstance(value, int):
        requested = int(value)
    else:
        try:
            requested = int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"{stage} repetitions must be an int, got "
                f"{type(value).__name__}: {value!r}"
            ) from None
        notes.append(f"coerced {type(value).__name__} to int for repetitions")

    if requested < 0:
        notes.append(f"negative repetitions={requested} treated as unset")
        return resolve_repetitions(None, stage=stage, default=default, ceiling=cap)

    source = "config"
    if requested > cap:
        notes.append(
            f"repetitions={requested} exceeded the ceiling {cap} and was "
            f"clamped; each repetition can cost a full verify timeout"
        )
        requested = cap
        source = "clamped"
    # 0 and 1 both mean "a single run" under the historical contract.
    return RepetitionResolution(
        stage=stage,
        repetitions=max(1, requested),
        source=source,
        requested=value,
        ceiling=cap,
        notes=tuple(notes),
    )


def repetitions_for_stage(
    stage: str,
    config: Optional[Mapping[str, Any]] = None,
    *,
    ceiling: Any = None,
) -> RepetitionResolution:
    """Resolve the repetition count for ``stage`` from a task config mapping.

    Assumes ``config`` is the merged ``Task.config`` (DEFAULTS plus the
    caller's overrides) and may be ``None``. Lookup is by KEY PRESENCE:

    * ``STAGE_POST_FIX`` reads ``post_fix_reruns``; when that key is absent
      it falls back to the legacy ``baseline_reruns`` so a config written
      before this split still resolves to one number rather than silently to
      a different one.
    * ``STAGE_BASELINE`` reads ``baseline_reruns``.

    An absent key resolves to the stage default —
    :data:`DEFAULT_POST_FIX_REPETITIONS` (2, the gate can fire) for the
    post-fix stage and :data:`DEFAULT_BASELINE_REPETITIONS` (1, cheap) for
    the baseline — so the split is real even for a config that sets neither.

    ``ceiling``, when given, overrides :data:`MAX_REPETITIONS`.

    Raises ``ValueError`` for an unknown ``stage`` and propagates
    :func:`resolve_repetitions`' refusal of a non-int configured value: a
    malformed knob is a bug and is reported, not rounded.
    """
    if stage not in REPETITION_CONFIG_KEYS:
        raise ValueError(
            f"unknown repetition stage {stage!r}; expected one of "
            f"{sorted(REPETITION_CONFIG_KEYS)}"
        )
    default = (
        DEFAULT_BASELINE_REPETITIONS
        if stage == STAGE_BASELINE
        else DEFAULT_POST_FIX_REPETITIONS
    )
    values: Mapping[str, Any] = config or {}
    key = REPETITION_CONFIG_KEYS[stage]
    value = values.get(key, None)
    source_key = key
    if value is None and stage == STAGE_POST_FIX:
        legacy = values.get(_LEGACY_REPETITION_KEY, None)
        if legacy is not None:
            value = legacy
            source_key = _LEGACY_REPETITION_KEY
    resolution = resolve_repetitions(
        value,
        stage=stage,
        default=default,
        ceiling=MAX_REPETITIONS if ceiling is None else ceiling,
    )
    if source_key != key:
        return RepetitionResolution(
            stage=resolution.stage,
            repetitions=resolution.repetitions,
            source=f"config:{source_key} (legacy key; {key} absent)",
            requested=resolution.requested,
            ceiling=resolution.ceiling,
            notes=resolution.notes,
        )
    if resolution.source == "default":
        return resolution
    return RepetitionResolution(
        stage=resolution.stage,
        repetitions=resolution.repetitions,
        source=f"config:{source_key}",
        requested=resolution.requested,
        ceiling=resolution.ceiling,
        notes=resolution.notes,
    )


# --------------------------------------------------------------------------
# attaching the evidence where a consumer can see it
# --------------------------------------------------------------------------

#: Attribute names :func:`attach_evidence` writes onto a ``VerificationResult``.
#: They are additive: ``shared.types.VerificationResult`` is owned by another
#: module, so this writes instance attributes rather than changing the
#: dataclass, and every historical field keeps its exact meaning.
EVIDENCE_ATTRS = ("flake_check", "repetitions", "observed_outcomes", "flake_evidence")


def attach_evidence(result: Any, verdict: FlakeVerdict) -> Any:
    """Record ``verdict`` on a ``VerificationResult`` and return it.

    Assumes ``result`` is a :class:`shared.types.VerificationResult` (or any
    object that accepts attribute assignment) and ``verdict`` is the verdict
    for the repetition series that produced it. Writes the additive
    attributes ``flake_check``, ``repetitions``, ``observed_outcomes`` and
    ``flake_evidence`` (the full JSON-compatible receipt), then sets the
    historical ``flaky`` field to ``verdict.flaky``.

    ``flaky`` for a :data:`NOT_RUN` verdict is ``False`` — the same value it
    has always had for a single run, so no existing consumer changes
    behaviour — while ``flake_check`` is what distinguishes "we checked and
    it was stable" from "we did not check". A consumer that intends to claim
    stability must read ``flake_check`` / ``detection_possible``; this function
    does not decide that for it and never downgrades a true ``flaky``.

    Returns ``result`` unchanged if it does not accept attributes, with the
    receipt still available from the returned ``FlakeRun`` the caller holds.
    """
    payload = verdict.to_dict()
    for name, value in (
        ("flake_check", verdict.flake_check),
        ("repetitions", int(verdict.repetitions)),
        ("observed_outcomes", list(verdict.observed_outcomes)),
        ("flake_evidence", payload),
    ):
        try:
            setattr(result, name, value)
        except (AttributeError, TypeError):
            return result
    try:
        result.flaky = verdict.flaky
    except (AttributeError, TypeError):
        pass
    return result


def evidence_of(result: Any) -> Optional[FlakeVerdict]:
    """Rebuild the :class:`FlakeVerdict` attached by :func:`attach_evidence`.

    Assumes ``result`` is a ``VerificationResult`` that went through
    :func:`attach_evidence`. Returns ``None`` when it did not — an absent
    receipt is reported as absent, never as "not flaky". A consumer can
    therefore tell a legacy single-run result (``None``) from a checked one.
    """
    if result is None:
        return None
    payload = getattr(result, "flake_evidence", None)
    if (
        isinstance(payload, Mapping)
        and payload.get("flake_check") in FLAKE_CHECK_VALUES
    ):
        return FlakeVerdict(
            flake_check=str(payload["flake_check"]),
            repetitions=int(payload.get("repetitions", 0) or 0),
            observed_outcomes=tuple(payload.get("observed_outcomes") or ()),
            distinct_outcomes=tuple(payload.get("distinct_outcomes") or ()),
            timed_out=bool(payload.get("timed_out", False)),
            requested_repetitions=int(payload.get("requested_repetitions", 0) or 0),
            notes=tuple(payload.get("notes") or ()),
        )
    check = getattr(result, "flake_check", None)
    if check in FLAKE_CHECK_VALUES:
        return flake_verdict(
            int(getattr(result, "repetitions", 0) or 0),
            list(getattr(result, "observed_outcomes", ()) or ()),
        )
    return None


def render_receipt(verdict: FlakeVerdict) -> str:
    """Render one bounded, human-readable line for a trace or log.

    Assumes ``verdict`` is a :class:`FlakeVerdict`. The line always names the
    verdict AND the repetition count, so a reader can see at a glance whether
    stability was tested: ``flake_check=not_run repetitions=1`` and
    ``flake_check=not_flaky repetitions=3`` must never be confusable. Never
    raises.
    """
    try:
        observed = ",".join(verdict.observed_outcomes) or "-"
        return (
            f"flake_check={verdict.flake_check} "
            f"repetitions={verdict.repetitions} "
            f"outcomes=[{observed}]" + (" timed_out" if verdict.timed_out else "")
        )
    except Exception:  # a receipt must never break the caller that renders it
        return "flake_check=unavailable"
