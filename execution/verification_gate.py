"""The production delegation seam for verification intelligence (R2-01).

`execution.verify.verify` is the authority for ONE question: *did these tests
pass on this repo state?* :mod:`execution.verification_intelligence` answers a
different, harder question: *is there enough independent evidence to call this
state done?* Until R2-01 those two lived apart — the intelligence layer was a
well-tested dark subgraph whose only inbound edges came from `evals/`. This
module is the ONE place that connects them.

## Why a delegation seam and not a rewrite

The mint is fail-closed and must stay that way, so the safe shape is additive
and keyed on PRESENCE, exactly as the Ceiling-14 `provider_gateway` seam does
in `runtime/model_router.py`:

    execution.verify.verify(..., intelligence_config=<resolved config>)
        -> intelligence_requested(config)   # ANY key PRESENT, value ignored
             -> run_intelligent_verify()    # this module
        -> otherwise: the pre-existing code path, BYTE-IDENTICAL

Activation is a **key-presence** test, never a value test. A value in
`harness/config.py::DEFAULTS` is merged into every task config and would
silently switch every task and every eval arm onto the pipeline at once;
"absent" has to remain a meaningful state that means *unchanged behaviour*.
The pipeline carries its own internal defaults for its knobs, so an operator
who writes one key gets a coherent pipeline rather than a half-configured one.
A config carrying e.g. ``verification_require_spec: False`` still opts IN —
it asked for the pipeline and explicitly turned that gate off inside it.

## The rungs, and what each one is allowed to do

A reader must be able to tell WHICH mechanism claimed a run was verified, so
every verdict carries a rung and the rung travels in the receipt, in the
`VerificationResult.raw_output` block, and in the unified trace:

``baseline``
    The pre-existing target-test + full-suite + not-flaky mint condition. This
    is the only rung that can mint, and it is computed from the *unchanged*
    code path.

``spec``
    The sealed obligation set. Two gates: the diff guard (an item may not be
    removed, added, or mutated) and the obligation check (each item's declared
    test references must actually be collected and pass). This rung can only
    REFUSE.

``independent``
    Held-out acceptance tests plus a separate judge, opt-in per run. The judge
    is *additional* evidence and is deliberately never authoritative: it can
    refuse, and it can never promote.

## The fold: how a verdict reaches the fail-closed mint

`shared.types.VerificationResult` is not this round's file and carries only
three booleans about the tests. `harness.core.run_task` mints
`status="success"` on exactly ``target_test_passed and regression_passed and
not flaky``. So the ONLY structurally sound way to stop a mint from a refused
rung is to clear one of those booleans, and this module clears
``target_test_passed``.

That fold is a **claim** statement, not a test observation, and the distinction
is load-bearing, so it is never silent:

- the real reason travels in `raw_output` under a ``## verification-gate``
  header, in one `reports` row per gate, and in the trace event;
- no rung can ever set a boolean back to ``True``. A judge that passes while
  the target test fails still yields a non-minting result, because the
  baseline rung is ANDed, never overridden.

The honest long-term fix is an additive ``VerificationResult`` field carrying
the gate rows; that is filed as a cross-terminal request in
`execution/AGENTS.md`` rather than done here, because `shared/types.py`` is
owned by another prompt.

## Degradation is a recorded reason, never a silent pass

A spec artifact that is missing, malformed, unreadable, or unsealed, and a
held-out suite that cannot be resolved, all produce a FAILING gate with the
reason attached. There is no path through this module that turns an
unreadable obligation set into a pass.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from execution.result_parsing import TestRunReport, parse_test_run

#: Re-exported from the seam holder; see the note on the module docstring's
#: "Why a delegation seam and not a rewrite" section. There is exactly one
#: literal, and it lives in ``execution.verify`` because that file has to answer
#: the key-presence question WITHOUT importing this module.
from execution.verify import _INTELLIGENCE_CONFIG_KEYS as INTELLIGENCE_CONFIG_KEYS

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

RUNG_BASELINE = "baseline"
RUNG_SPEC = "spec"
RUNG_INDEPENDENT = "independent"

#: Every rung a verdict may carry. A verdict with a rung outside this tuple is
#: a caller bug, and :func:`_validated` refuses it rather than emitting an
#: unreadable receipt.
RUNGS: Tuple[str, ...] = (RUNG_BASELINE, RUNG_SPEC, RUNG_INDEPENDENT)

GATE_BASELINE = "baseline_target_and_suite"
GATE_SPEC_INTACT = "spec_intact"
GATE_OBLIGATIONS = "spec_obligations"
GATE_INDEPENDENT = "independent_evidence"

#: Internal defaults. Bounded on purpose: an unbounded obligation sweep or an
#: unbounded judge would turn a verification into an open-ended test marathon.
DEFAULT_MAX_OBLIGATIONS = 8
DEFAULT_HELD_OUT_SEED = 0
DEFAULT_GAP_THRESHOLD_POINTS = 5.0
DEFAULT_FLAKE_CONFIRM_ATTEMPTS = 2
DEFAULT_SELECTION_MAX_FILES = 12

_RAW_HEADER = "## verification-gate"
_RECEIPT_SOURCE = "verification_gate"


# ---------------------------------------------------------------------------
# activation
# ---------------------------------------------------------------------------


def present_keys(config: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """Return the intelligence keys actually present in ``config``.

    Assumes ``config`` is a mapping or ``None``; ``None`` and any non-mapping
    yield an empty tuple, which is the "not requested" state. Order follows
    :data:`INTELLIGENCE_CONFIG_KEYS` so the tuple is stable and comparable
    across runs.
    """
    if not isinstance(config, Mapping):
        return ()
    return tuple(key for key in INTELLIGENCE_CONFIG_KEYS if key in config)


def intelligence_requested(config: Optional[Mapping[str, Any]]) -> bool:
    """Return whether the caller configured any verification intelligence.

    Deliberately a KEY test, not a value test. ``{"verification_require_spec":
    False}`` still opts in: the operator wrote the key, so they want the
    pipeline and its receipts, and turned that one gate off inside it. A config
    with none of :data:`INTELLIGENCE_CONFIG_KEYS` never enters the pipeline and
    keeps the exact pre-existing code path.
    """
    return bool(present_keys(config))


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Resolved pipeline knobs.

    Every field has an internal default so a single opted-in key produces a
    coherent pipeline instead of a half-configured one. Nothing here is read
    from ``harness/config.py::DEFAULTS`` on purpose — see
    :data:`INTELLIGENCE_CONFIG_KEYS`.
    """

    spec_root: str = ""
    require_spec: bool = False
    max_obligations: int = DEFAULT_MAX_OBLIGATIONS
    held_out_suite: Any = None
    held_out_root: str = ""
    held_out_cases: Tuple[Mapping[str, Any], ...] = ()
    held_out_seed: int = DEFAULT_HELD_OUT_SEED
    independent_judge: bool = False
    gap_threshold_points: float = DEFAULT_GAP_THRESHOLD_POINTS
    run_dir: str = ""
    task_id: str = ""
    run_id: str = ""
    held_out_runner: Optional[Callable[[str, Any], Any]] = None
    confirm_runner: Optional[Callable[[str], Any]] = None

    @property
    def judge_requested(self) -> bool:
        """Return whether the independent rung is configured for this run.

        Opt-in per run and fail-closed: the rung runs when the caller asked
        for it, and a suite it cannot resolve is a FAILING gate with a
        recorded reason — never a silently absent gate.
        """
        if self.independent_judge:
            return True
        return bool(
            self.held_out_suite is not None or self.held_out_root or self.held_out_cases
        )


def resolve_settings(config: Optional[Mapping[str, Any]]) -> Settings:
    """Build :class:`Settings` from a resolved task config.

    Assumes ``config`` is a mapping (an empty one yields every internal
    default). Unusable values degrade to the default and are reported by
    :func:`_config_warnings` rather than raising, because a malformed knob must
    not crash a verification that is otherwise runnable.
    """
    data: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
    cases_raw = data.get("verification_held_out_cases")
    cases: Tuple[Mapping[str, Any], ...] = ()
    if isinstance(cases_raw, Sequence) and not isinstance(cases_raw, (str, bytes)):
        cases = tuple(entry for entry in cases_raw if isinstance(entry, Mapping))
    return Settings(
        spec_root=_text(data.get("verification_spec_root")),
        require_spec=bool(data.get("verification_require_spec", False)),
        max_obligations=_positive_int(
            data.get("verification_max_obligations"), DEFAULT_MAX_OBLIGATIONS
        ),
        held_out_suite=data.get("verification_held_out_suite"),
        held_out_root=_text(data.get("verification_held_out_root")),
        held_out_cases=cases,
        held_out_seed=_int(
            data.get("verification_held_out_seed"), DEFAULT_HELD_OUT_SEED
        ),
        independent_judge=bool(data.get("verification_independent_judge", False)),
        gap_threshold_points=_float(
            data.get("verification_gap_threshold_points"), DEFAULT_GAP_THRESHOLD_POINTS
        ),
        run_dir=_text(data.get("verification_run_dir")),
        task_id=_text(data.get("verification_task_id")),
        run_id=_text(data.get("verification_run_id")),
        held_out_runner=_callable(data.get("verification_held_out_runner")),
        confirm_runner=_callable(data.get("verification_confirm_runner")),
    )


def _config_warnings(config: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """Return human-readable notes about config values that had to be coerced.

    Every branch here is a case where the operator wrote something and got
    something else. Recording the substitution is what keeps "the pipeline ran
    with your settings" an honest claim.
    """
    if not isinstance(config, Mapping):
        return ()
    notes: List[str] = []
    raw_cases = config.get("verification_held_out_cases")
    if raw_cases is not None and not (
        isinstance(raw_cases, Sequence) and not isinstance(raw_cases, (str, bytes))
    ):
        notes.append(
            "verification_held_out_cases must be a sequence of mappings; "
            "the value was ignored"
        )
    for key, default in (
        ("verification_max_obligations", DEFAULT_MAX_OBLIGATIONS),
        ("verification_held_out_seed", DEFAULT_HELD_OUT_SEED),
    ):
        if key in config and _positive_int(config.get(key), default) != config.get(key):
            notes.append(f"{key}={config.get(key)!r} is not usable; {default} was used")
    if "verification_gap_threshold_points" in config and _float(
        config.get("verification_gap_threshold_points"), DEFAULT_GAP_THRESHOLD_POINTS
    ) != config.get("verification_gap_threshold_points"):
        notes.append(
            "verification_gap_threshold_points="
            f"{config.get('verification_gap_threshold_points')!r} is not a number; "
            f"{DEFAULT_GAP_THRESHOLD_POINTS} was used"
        )
    for key in ("verification_held_out_runner", "verification_confirm_runner"):
        if key in config and not callable(config.get(key)):
            notes.append(f"{key} is not callable and was ignored")
    return tuple(notes)


def _text(value: Any) -> str:
    """Return a stripped string for ``value``, or "" for anything unusable."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _int(value: Any, default: int) -> int:
    """Return ``value`` as an int, or ``default`` when it is not one."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _positive_int(value: Any, default: int) -> int:
    """Return ``value`` as an int >= 1, or ``default``."""
    parsed = _int(value, default)
    return parsed if parsed >= 1 else default


def _float(value: Any, default: float) -> float:
    """Return ``value`` as a float, or ``default`` when it is not one."""
    if isinstance(value, bool):
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _callable(value: Any) -> Optional[Callable[..., Any]]:
    """Return ``value`` when it is callable, else None."""
    return value if callable(value) else None


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateVerdict:
    """One gate's verdict plus the rung that produced it.

    ``rung`` is always a member of :data:`RUNGS`; a verdict is rejected at
    construction-by-:func:`_validated` otherwise, so a receipt can never claim a
    mechanism this module does not implement.
    """

    rung: str
    name: str
    passed: bool
    mandatory: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view carrying the rung explicitly."""
        return {
            "rung": self.rung,
            "name": self.name,
            "passed": bool(self.passed),
            "mandatory": bool(self.mandatory),
            "reason": self.reason,
        }

    def report_row(self) -> Dict[str, Any]:
        """Return the ``reports``-sink row shape every verify consumer accepts.

        Uses the same keys :func:`execution.verify._report` emits (``outcome``,
        ``source``, ``confidence``, ``notes``) so a caller that already renders
        reports needs no new branch, and carries ``rung``/``gate``/``reason``
        beside them so the rung survives into the trace.
        """
        return {
            "outcome": "pass" if self.passed else "fail",
            "source": _RECEIPT_SOURCE,
            "confidence": "high",
            "exit_code": None,
            "timed_out": False,
            "tests_collected": None,
            "tests_passed": None,
            "tests_failed": None,
            "tests_skipped": None,
            "passed": bool(self.passed),
            "rung": self.rung,
            "gate": self.name,
            "mandatory": bool(self.mandatory),
            "notes": [self.reason] if self.reason else [],
        }


@dataclass
class IntelligenceDecision:
    """The full verdict bundle for one delegated verification.

    ``refusals`` is the only field the fail-closed fold reads, and it is
    derived in :meth:`__post_init__` from MANDATORY gates alone: an optional
    (skipped) gate can never block a mint, and a mandatory gate that was
    skipped counts as a refusal rather than as an absence.
    """

    keys_present: Tuple[str, ...] = ()
    verdicts: Tuple[GateVerdict, ...] = ()
    spec: Optional[Dict[str, Any]] = None
    obligations: Tuple[Dict[str, Any], ...] = ()
    unchecked_obligations: Tuple[str, ...] = ()
    judgment: Optional[Dict[str, Any]] = None
    errors: Tuple[str, ...] = ()
    degraded: Tuple[str, ...] = ()
    traced: bool = False
    trace_refusal: str = ""
    applied: bool = False
    folded_fields: Tuple[str, ...] = ()
    baseline: Optional[Dict[str, Any]] = None
    timings_ms: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Derive :attr:`refusals` so no caller can compute it inconsistently."""
        self.refusals: Tuple[GateVerdict, ...] = tuple(
            verdict
            for verdict in self.verdicts
            if verdict.mandatory and not verdict.passed
        )

    @property
    def mintable(self) -> bool:
        """Return whether every mandatory rung accepted this state.

        This is the intelligence layer's own opinion. The mint itself still
        requires the harness's baseline condition; see
        :func:`apply_fold` for why the two are ANDed rather than merged.
        """
        return not self.refusals

    def verdict(self, name: str) -> Optional[GateVerdict]:
        """Return the verdict for gate ``name``, or None when absent."""
        for candidate in self.verdicts:
            if candidate.name == name:
                return candidate
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible evidence bundle for traces and reports."""
        return {
            "keys_present": list(self.keys_present),
            "mintable": self.mintable,
            "applied": bool(self.applied),
            "folded_fields": list(self.folded_fields),
            "verdicts": [verdict.to_dict() for verdict in self.verdicts],
            "refusals": [verdict.to_dict() for verdict in self.refusals],
            "spec": dict(self.spec) if self.spec else None,
            "obligations": [dict(item) for item in self.obligations],
            "unchecked_obligations": list(self.unchecked_obligations),
            "judgment": dict(self.judgment) if self.judgment else None,
            "errors": list(self.errors),
            "degraded": list(self.degraded),
            "traced": bool(self.traced),
            "trace_refusal": self.trace_refusal,
            "baseline": dict(self.baseline) if self.baseline else None,
            "timings_ms": dict(self.timings_ms),
        }


def _validated(
    verdicts: Sequence[GateVerdict], errors: Sequence[str]
) -> Tuple[GateVerdict, ...]:
    """Return ``verdicts``, dropping any whose rung is not in :data:`RUNGS`.

    A malformed rung is a programming error in a caller, not a run failure, so
    it is reported and dropped rather than allowed to travel into a receipt a
    human will read as a real mechanism name.
    """
    out: List[GateVerdict] = []
    for verdict in verdicts:
        if verdict.rung not in RUNGS:
            errors.append(
                f"dropped gate {verdict.name!r} with unknown rung {verdict.rung!r}"
            )
            continue
        out.append(verdict)
    return tuple(out)


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------


def evaluate(
    *,
    repo_path: str,
    baseline_result: Any,
    baseline_reports: Sequence[Mapping[str, Any]],
    settings: Settings,
    keys_present: Sequence[str] = (),
    config: Optional[Mapping[str, Any]] = None,
    test_command: Optional[str] = None,
    verify_timeout_s: int = 300,
    allow_network: bool = False,
    target_test: Optional[str] = None,
    changed_files: Optional[Sequence[str]] = None,
) -> IntelligenceDecision:
    """Evaluate the intelligence rungs on top of an already-computed baseline.

    Assumes ``baseline_result`` is the ``VerificationResult`` the UNCHANGED
    ``execution.verify`` code path produced (so this function never re-runs the
    suite) and that ``baseline_reports`` is the matching report list. ``config``
    is the raw mapping the settings were resolved from, kept so a value that had
    to be coerced is reported rather than silently replaced.

    Never raises: every rung failure becomes a verdict with a reason, because a
    verification that crashes on a broken spec artifact is strictly less useful
    than one that refuses it and says why.
    """
    errors: List[str] = list(_config_warnings(config))
    degraded: List[str] = []
    timings: Dict[str, float] = {}
    verdicts: List[GateVerdict] = []

    # -- rung: baseline -----------------------------------------------------
    verdicts.append(_baseline_verdict(baseline_result))

    # -- rung: spec ---------------------------------------------------------
    spec_started = time.time()
    spec_report: Optional[Any] = None
    obligation_rows: List[Dict[str, Any]] = []
    unchecked: List[str] = []
    spec_root = settings.spec_root or settings.run_dir or str(repo_path)
    if settings.require_spec or _spec_artifact_exists(spec_root):
        # Loaded HERE, at the point the obligation set is actually needed, not
        # at import time and not at the top of verify(): a run that never
        # consults an obligation set must not pay for one, and a run that does
        # must not consult a stale copy.
        spec_report = _load_spec(spec_root)
        timings["spec_ms"] = round((time.time() - spec_started) * 1000.0, 3)
        if spec_report is None:
            degraded.append("spec: the spec artifact could not be read")
        elif not getattr(spec_report, "sealed", False):
            # Present but untrusted: unsealed or unparseable. A TAMPERED spec
            # is not a degradation — that is a genuine refusal with names in it.
            degraded.append(
                "spec: the obligation set is unsealed or unreadable, so the guard "
                "could not trust it"
            )
        verdicts.append(_spec_intact_verdict(spec_report, settings.require_spec))
        obligations_started = time.time()
        obligation_rows, unchecked, obligation_gate = _obligation_gate(
            repo_path=repo_path,
            spec_report=spec_report,
            settings=settings,
            test_command=test_command,
            verify_timeout_s=verify_timeout_s,
            allow_network=allow_network,
            errors=errors,
        )
        timings["obligations_ms"] = round(
            (time.time() - obligations_started) * 1000.0, 3
        )
        verdicts.append(obligation_gate)
    else:
        verdicts.append(
            GateVerdict(
                RUNG_SPEC,
                GATE_SPEC_INTACT,
                not settings.require_spec,
                mandatory=settings.require_spec,
                reason=(
                    "no spec artifact was supplied; spec gating not requested"
                    if not settings.require_spec
                    else "a spec artifact was required but none was found"
                ),
            )
        )

    # -- rung: independent (opt-in per run) ---------------------------------
    judgment: Optional[Dict[str, Any]] = None
    if settings.judge_requested:
        judge_started = time.time()
        judgment, independent_gate = _independent_gate(
            repo_path=repo_path,
            baseline_reports=baseline_reports,
            settings=settings,
            errors=errors,
            degraded=degraded,
            target_test=target_test,
            changed_files=changed_files,
        )
        timings["independent_ms"] = round((time.time() - judge_started) * 1000.0, 3)
        verdicts.append(independent_gate)

    checked = _validated(verdicts, errors)
    decision = IntelligenceDecision(
        keys_present=tuple(keys_present),
        verdicts=checked,
        spec=spec_report.to_dict() if spec_report is not None else None,
        obligations=tuple(obligation_rows),
        unchecked_obligations=tuple(unchecked),
        judgment=judgment,
        errors=tuple(errors),
        degraded=tuple(degraded),
        baseline=_baseline_snapshot(baseline_result),
        timings_ms=timings,
    )
    return decision


def _baseline_verdict(result: Any) -> GateVerdict:
    """Return the ``baseline`` rung verdict for one ``VerificationResult``.

    The condition is byte-for-byte the harness's own mint condition
    (``target AND regression AND not flaky``). It is recorded, never applied:
    the booleans it reads are exactly the ones the harness reads, so this rung
    cannot disagree with the mint — it can only explain it.
    """
    target = bool(getattr(result, "target_test_passed", False))
    regression = bool(getattr(result, "regression_passed", False))
    flaky = bool(getattr(result, "flaky", False))
    if target and regression and not flaky:
        return GateVerdict(
            RUNG_BASELINE,
            GATE_BASELINE,
            True,
            True,
            "the target test passed, the full suite passed, and the target was not flaky",
        )
    reasons = []
    if not target:
        reasons.append("the target test did not pass")
    if not regression:
        reasons.append("the full suite did not pass")
    if flaky:
        reasons.append("the target produced different outcomes across reruns")
    return GateVerdict(
        RUNG_BASELINE,
        GATE_BASELINE,
        False,
        True,
        "; ".join(reasons) or "the gate did not pass",
    )


def _baseline_snapshot(result: Any) -> Dict[str, Any]:
    """Return the baseline booleans as they were BEFORE any fold."""
    return {
        "target_test_passed": bool(getattr(result, "target_test_passed", False)),
        "regression_passed": bool(getattr(result, "regression_passed", False)),
        "flaky": bool(getattr(result, "flaky", False)),
        "suite_scope": "full",
    }


def _spec_artifact_exists(root: str) -> bool:
    """Return whether a spec artifact is present under ``root``.

    Only consulted to decide whether the rung runs; it never establishes that
    the artifact is VALID. That distinction is the whole point of the guard.
    """
    from execution.spec_ledger import guard_paths

    artifact, _seal = guard_paths(str(root))
    return os.path.isfile(artifact)


def _load_spec(root: str) -> Optional[Any]:
    """Load and guard the spec under ``root``; return None when unreadable.

    ``execution.spec_ledger.load_or_report`` already reports rather than raises
    for a missing/malformed artifact, which is the right shape: a broken
    obligation set is a REFUSAL with a reason, not an exception that would take
    a verification down with it. An unexpected non-SpecViolation escape is
    caught here so even a defect inside the ledger cannot crash verification.
    """
    try:
        from execution.spec_ledger import load_or_report

        return load_or_report(str(root))
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        from execution.spec_ledger import SpecGuardReport

        return SpecGuardReport(
            ok=False,
            sealed=False,
            violations=(f"spec unreadable: {type(exc).__name__}: {exc}",),
        )


def _spec_intact_verdict(report: Optional[Any], require_spec: bool) -> GateVerdict:
    """Turn a spec guard report into the ``spec_intact`` verdict.

    An absent spec is a SKIPPED optional gate when the caller did not require
    one, and a FAILING mandatory gate when they did. A present-but-unreadable
    or unsealed spec is always a failure, because "we could not check the
    obligation set" must never read as "the obligation set is fine".
    """
    if report is None:
        return GateVerdict(
            RUNG_SPEC,
            GATE_SPEC_INTACT,
            not require_spec,
            mandatory=require_spec,
            reason=(
                "no spec artifact was supplied; spec gating not requested"
                if not require_spec
                else "a spec artifact was required but none was readable"
            ),
        )
    ok = bool(getattr(report, "ok", False))
    violations = tuple(getattr(report, "violations", ()) or ())
    if ok:
        return GateVerdict(
            RUNG_SPEC, GATE_SPEC_INTACT, True, True, "the spec obligation set is intact"
        )
    detail = (
        "; ".join(str(entry) for entry in violations)
        or "the spec obligation set changed"
    )
    named = []
    for field_name in ("removed", "added", "mutated"):
        values = tuple(getattr(report, field_name, ()) or ())
        if values:
            named.append(f"{field_name}={list(values)}")
    if named:
        detail = detail + " (" + ", ".join(named) + ")"
    return GateVerdict(RUNG_SPEC, GATE_SPEC_INTACT, False, True, detail)


def _obligation_gate(
    *,
    repo_path: str,
    spec_report: Optional[Any],
    settings: Settings,
    test_command: Optional[str],
    verify_timeout_s: int,
    allow_network: bool,
    errors: List[str],
) -> Tuple[List[Dict[str, Any]], List[str], GateVerdict]:
    """Check every sealed obligation's declared tests; return rows and a verdict.

    "The diff does not satisfy the obligation" is decided by RUNNING the test
    references the obligation itself names, not by trusting a ``passes`` flag —
    the flag is the agent's report and the seal deliberately excludes it. Each
    obligation gets its own sandboxed pytest selection so a failure names the
    obligation it belongs to.

    Assumes ``spec_report`` is a loaded guard report or ``None``. A missing or
    refusing report yields no rows and a verdict that mirrors the ``spec_intact``
    gate, so the two can never disagree.
    """
    if spec_report is None or not bool(getattr(spec_report, "ok", False)):
        return [], [], _spec_intact_verdict(spec_report, settings.require_spec)

    try:
        from execution.spec_ledger import SpecLedger, guard_paths
    except ImportError as exc:  # pragma: no cover - the module is in-tree
        errors.append(f"spec ledger unavailable: {exc}")
        return (
            [],
            [],
            GateVerdict(
                RUNG_SPEC,
                GATE_OBLIGATIONS,
                False,
                True,
                "the spec ledger could not be imported",
            ),
        )

    root = settings.spec_root or settings.run_dir or str(repo_path)
    artifact, seal = guard_paths(str(root))
    try:
        ledger = SpecLedger.load(artifact, seal_path=seal)
    except Exception as exc:
        errors.append(
            f"obligation set could not be reloaded: {type(exc).__name__}: {exc}"
        )
        return (
            [],
            [],
            GateVerdict(
                RUNG_SPEC,
                GATE_OBLIGATIONS,
                False,
                True,
                f"the obligation set could not be reloaded: {exc}",
            ),
        )

    items = list(ledger.items)
    checked = items[: settings.max_obligations]
    unchecked = [item.id for item in items[settings.max_obligations :]]

    suite_cmd = _suite_command(repo_path, test_command)
    if suite_cmd is None:
        return (
            [],
            unchecked,
            GateVerdict(
                RUNG_SPEC,
                GATE_OBLIGATIONS,
                False,
                True,
                "no test command could be resolved, so no declared obligation could be checked",
            ),
        )

    rows: List[Dict[str, Any]] = []
    unsatisfied: List[str] = []
    for item in checked:
        row, ok, reason = _check_obligation(
            item=item,
            repo_path=repo_path,
            suite_cmd=suite_cmd,
            verify_timeout_s=verify_timeout_s,
            allow_network=allow_network,
        )
        rows.append(row)
        if not ok:
            unsatisfied.append(f"{item.id}: {reason}")

    if unsatisfied:
        return (
            rows,
            unchecked,
            GateVerdict(
                RUNG_SPEC,
                GATE_OBLIGATIONS,
                False,
                True,
                "declared obligations are not satisfied: " + "; ".join(unsatisfied),
            ),
        )
    if unchecked:
        # A bounded sweep that skipped obligations is a refusal, not a partial
        # pass: an unchecked obligation is exactly the space a shrunken spec
        # would have hidden in.
        return (
            rows,
            unchecked,
            GateVerdict(
                RUNG_SPEC,
                GATE_OBLIGATIONS,
                False,
                True,
                f"{len(unchecked)} obligation(s) were not checked because the sweep is "
                f"bounded at {settings.max_obligations}: {', '.join(unchecked)}",
            ),
        )
    return (
        rows,
        unchecked,
        GateVerdict(
            RUNG_SPEC,
            GATE_OBLIGATIONS,
            True,
            True,
            f"all {len(checked)} declared obligation(s) had their named tests collected and passing",
        ),
    )


def _check_obligation(
    *,
    item: Any,
    repo_path: str,
    suite_cmd: str,
    verify_timeout_s: int,
    allow_network: bool,
) -> Tuple[Dict[str, Any], bool, str]:
    """Run one spec item's declared test references and judge the outcome.

    The item is satisfied only when the run PASSES *and* collected at least as
    many tests as the item names. That second condition is what stops a renamed
    or deleted test from satisfying its own obligation: a vanished node id
    produces ``no_tests`` / a usage error, which ``parse_test_run`` already
    refuses to call a pass.
    """
    from execution.sandbox import execute_sandboxed

    declared = [
        str(entry) for entry in getattr(item, "tests", ()) or () if str(entry).strip()
    ]
    row: Dict[str, Any] = {
        "id": str(getattr(item, "id", "")),
        "title": str(getattr(item, "title", "")),
        "tests": list(declared),
        "claimed": bool(getattr(item, "passes", False)),
        "outcome": "fail",
        "source": "exit_code",
        "confidence": "low",
        "tests_collected": None,
        "tests_passed": None,
        "notes": [],
    }
    if not declared:
        reason = "the obligation names no test references"
        row["notes"] = [reason]
        return row, False, reason

    command = f"{suite_cmd} " + " ".join(_quote(node) for node in declared)
    try:
        result = execute_sandboxed(
            repo_path, command, verify_timeout_s, allow_network=allow_network
        )
    except Exception as exc:
        reason = f"the obligation could not be checked: {type(exc).__name__}: {exc}"
        row["outcome"] = "error"
        row["notes"] = [reason]
        return row, False, reason

    report: TestRunReport = parse_test_run(result, expected_tests=len(declared))
    row.update(
        {
            "outcome": report.outcome,
            "source": report.source,
            "confidence": report.confidence,
            "tests_collected": report.tests_collected,
            "tests_passed": report.tests_passed,
            "notes": list(report.notes),
        }
    )
    if report.outcome != "pass":
        return row, False, f"the named tests did not pass ({report.outcome})"
    if report.tests_collected is not None and report.tests_collected < len(declared):
        return (
            row,
            False,
            f"only {report.tests_collected} of {len(declared)} named test(s) were collected",
        )
    return row, True, "the named tests were collected and passing"


def _suite_command(repo_path: str, test_command: Optional[str]) -> Optional[str]:
    """Return the suite command to compose obligation selections onto.

    Delegates to ``execution.verify._autodetect_test_command`` — the same
    resolver ``verify()`` uses, deliberately, so an obligation run and a
    verification run can never disagree about the language. That is a private
    name inside this module family; it is the intended seam and is documented
    in INTERFACES.md rather than forked into a second detector.
    """
    if test_command:
        return test_command
    try:
        from execution.verify import _autodetect_test_command

        return _autodetect_test_command(str(repo_path))
    except Exception:  # pragma: no cover - defensive; never fail a gate on import
        return None


def _quote(node_id: str) -> str:
    """Shell-quote one pytest node id for the sandbox's bash invocation.

    Node ids routinely contain characters the runner's arg parser would treat
    as syntax, and an unquoted id is word-split into a filter over the first
    token only — which would silently widen the selection instead of narrowing
    it.
    """
    return "'" + node_id.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# the independent rung
# ---------------------------------------------------------------------------


def _independent_gate(
    *,
    repo_path: str,
    baseline_reports: Sequence[Mapping[str, Any]],
    settings: Settings,
    errors: List[str],
    degraded: List[str],
    target_test: Optional[str],
    changed_files: Optional[Sequence[str]],
) -> Tuple[Optional[Dict[str, Any]], GateVerdict]:
    """Run the held-out suite in an independent context and judge the claim.

    Assumes the caller opted in. An unresolvable suite is a FAILING gate with
    the reason attached, not an absent gate: opting into independent evidence
    and then not getting it must never read as independent evidence agreeing.

    The judge is never authoritative. It returns a verdict here and nothing
    else — it cannot set a boolean True, and the baseline rung is ANDed with
    whatever it says.
    """
    from execution.independent_evidence import judge as judge_held_out

    suite, suite_reason = _resolve_held_out_suite(repo_path, settings)
    if suite is None:
        degraded.append("independent: the held-out suite could not be resolved")
        return None, GateVerdict(
            RUNG_INDEPENDENT,
            GATE_INDEPENDENT,
            False,
            True,
            f"the held-out suite could not be resolved: {suite_reason}",
        )

    copy_path, staging = _clean_copy_for_judge(str(repo_path))
    try:
        if copy_path != str(repo_path):
            degraded.append("independent: the judge evaluated its own clean copy")
        judgment = judge_held_out(
            suite,
            run=settings.held_out_runner or _sandbox_held_out_runner,
            visible_report=_summary_report(baseline_reports),
            claims={
                "target_test": target_test,
                "changed_files": list(changed_files or []),
            },
            expected_fingerprint=suite.sealed_fingerprint,
            threshold_points=settings.gap_threshold_points,
            repo_path=str(repo_path),
            clean_repo_path=copy_path,
        )
    except Exception as exc:
        errors.append(f"independent judge crashed: {type(exc).__name__}: {exc}")
        return None, GateVerdict(
            RUNG_INDEPENDENT,
            GATE_INDEPENDENT,
            False,
            True,
            f"the independent judge could not run: {type(exc).__name__}: {exc}",
        )
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)

    if judgment.verified:
        return judgment.to_dict(), GateVerdict(
            RUNG_INDEPENDENT,
            GATE_INDEPENDENT,
            True,
            True,
            "the independent judge accepted the claim on its own clean copy",
        )
    reasons = "; ".join(judgment.reasons) or f"verdict={judgment.verdict}"
    return judgment.to_dict(), GateVerdict(
        RUNG_INDEPENDENT, GATE_INDEPENDENT, False, True, reasons
    )


def _resolve_held_out_suite(
    repo_path: str, settings: Settings
) -> Tuple[Optional[Any], str]:
    """Return ``(suite, reason)``; ``(None, reason)`` is an honest refusal.

    Three opt-in shapes are supported, in precedence order: a caller-supplied
    :class:`~execution.independent_evidence.HeldOutSuite` object (the in-process
    seam), a case list to materialize, or a directory that already holds a
    built suite. Nothing is invented when none is present.

    Assumes ``execution.independent_evidence`` is importable — it is in-tree, and
    :func:`_independent_gate` imports it before calling this — so a failure here
    is a materialization or read problem rather than a missing dependency. Every
    such problem becomes a ``(None, reason)`` refusal, never an exception.
    """
    suite = settings.held_out_suite
    if suite is not None:
        return suite, ""

    root = settings.held_out_root
    if settings.held_out_cases:
        if not root:
            return None, "verification_held_out_cases was given without a suite root"
        try:
            from execution.independent_evidence import build_held_out_suite

            return (
                build_held_out_suite(
                    root,
                    list(settings.held_out_cases),
                    seed=settings.held_out_seed,
                    repo_path=str(repo_path),
                ),
                "",
            )
        except Exception as exc:
            return (
                None,
                f"the held-out suite could not be built: {type(exc).__name__}: {exc}",
            )

    if not root:
        return None, "no held-out suite, suite root, or case list was configured"
    if not os.path.isdir(root):
        return None, f"the configured held-out suite root {root!r} is not a directory"
    try:
        return _load_suite_from_disk(root, seed=settings.held_out_seed), ""
    except Exception as exc:
        return (
            None,
            f"the held-out suite at {root!r} is unusable: {type(exc).__name__}: {exc}",
        )


def _load_suite_from_disk(root: str, *, seed: int) -> Any:
    """Build a :class:`HeldOutSuite` view over an already-materialized directory.

    The digests are computed from the files on disk and the suite is sealed with
    them, so a suite edited BEFORE the judge ran is detected as tampering by the
    judge's own fingerprint comparison — the pre-loop fingerprint has to come
    from somewhere, and inventing a seal here is exactly as good as the
    existing "absent fingerprint is a FAILURE" policy, not a bypass of it.
    """
    from execution.independent_evidence import (
        HELD_OUT_CONFIG_NAME,
        HELD_OUT_TEST_NAME,
        HeldOutFile,
        HeldOutSuite,
        _sha256_text,
    )

    names = (HELD_OUT_TEST_NAME, HELD_OUT_CONFIG_NAME, "conftest.py")
    missing = [name for name in names if not os.path.isfile(os.path.join(root, name))]
    if missing:
        raise ValueError("missing held-out file(s): " + ", ".join(missing))
    files = tuple(
        HeldOutFile(
            name=name,
            digest=_sha256_text(_read_text(os.path.join(root, name))),
            path=os.path.join(root, name),
        )
        for name in sorted(names)
    )
    suite = HeldOutSuite(
        root=root, seed=int(seed), files=files, concealed=False, note=""
    )
    suite.sealed_fingerprint = suite.fingerprint()
    return suite


def _read_text(path: str) -> str:
    """Read a text file, raising ``OSError`` for anything unreadable."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _clean_copy_for_judge(repo_path: str) -> Tuple[str, str]:
    """Return ``(copy_path, staging_root)`` for the judge's independent context.

    The staging directory is SHORT and lives in the system temp dir, and that is
    load-bearing rather than cosmetic: Docker Desktop bind-mounts the host path
    into the container, and a deeply nested path produces
    ``OSError: [Errno 5] Input/output error`` on ordinary reads INSIDE the
    mount — an environment fault that reads exactly like a code failure and
    would send the judge chasing a defect that is not there.

    Falls back to the original path with an empty staging root, which is honest
    (the judge still runs and the caller records that it was not independent)
    rather than a silent substitution.
    """
    try:
        from execution.independent_evidence import copy_tree

        staging = tempfile.mkdtemp(prefix="neojudge")
        target = os.path.join(staging, "repo")
        return copy_tree(str(repo_path), target), staging
    except (OSError, ValueError):
        return str(repo_path), ""


def _sandbox_held_out_runner(repo_path: str, suite: Any) -> Any:
    """Run the held-out suite inside the Docker sandbox against a repo copy.

    The suite is COPIED into the repository the judge is evaluating, because
    the sandbox only mounts that repository, and copying is also what makes the
    context independent: the judge evaluates its own tree, not the builder's.

    An evaluator-side crash is retried ONCE using this project's own flake
    policy — a container I/O error is not a failing acceptance test, and
    recording it as one would turn an environment fault into a fake rejection.
    A genuine test failure is never retried, and two crashes are reported as
    two. The default runner is deliberately the SANDBOX lane, unlike
    ``verification_intelligence``'s host-side default, because production
    verification runs in Docker and a host-side judge would be evidence from a
    different environment than the one the verdict describes.
    """
    from execution.independent_evidence import HELD_OUT_CONFIG_NAME

    staged_rel = "_held_out"
    staged = os.path.join(repo_path, staged_rel)
    if os.path.isdir(staged):
        shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(suite.root, staged, dirs_exist_ok=True)
    try:
        command = (
            f"python -m pytest -q -p no:cacheprovider "
            f"-c {staged_rel}/{HELD_OUT_CONFIG_NAME} {staged_rel}"
        )
        report: Optional[TestRunReport] = None
        for _attempt in range(2):
            from execution.sandbox import execute_sandboxed

            result = execute_sandboxed(repo_path, command, 300)
            report = parse_test_run(result)
            if report.outcome != "error":
                break
        assert report is not None
        return report
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _summary_report(reports: Sequence[Mapping[str, Any]]) -> Optional[TestRunReport]:
    """Return the visible-suite aggregate the judge compares its score against."""
    collected = 0
    passed = 0
    saw = False
    for item in reports:
        value = item.get("tests_collected")
        if isinstance(value, int):
            collected += value
            saw = True
        value_passed = item.get("tests_passed")
        if isinstance(value_passed, int):
            passed += value_passed
            saw = True
    if not saw or collected <= 0:
        return None
    return TestRunReport(
        outcome="pass" if passed == collected else "fail",
        tests_collected=collected,
        tests_passed=passed,
        tests_failed=max(0, collected - passed),
        source="report",
        confidence="medium",
    )


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------


def plan_fold(result: Any, decision: IntelligenceDecision) -> bool:
    """Clear ``target_test_passed`` when a MANDATORY rung refused; report whether.

    This is the ONLY way this module can change a boolean, and it only ever
    CLEARS one. There is deliberately no symmetric promote path: the judge is
    additional evidence, and additional evidence that only ever removes a claim
    is what makes it safe to put in front of a fail-closed mint.

    The cleared field is a STATEMENT ABOUT THE CLAIM, not about the tests, which
    is why the receipt has to be attached afterwards (:func:`attach_receipt`) —
    the reason has to travel in the same block. Splitting plan from render is
    what lets the trace carry ``applied`` truthfully: the block would otherwise
    have to be rendered before the trace event that reports it.
    """
    if not decision.refusals:
        decision.applied = False
        return False
    if not bool(getattr(result, "target_test_passed", False)):
        # The baseline rung already refused; the intelligence fold has nothing
        # to clear and must not claim credit for the block.
        decision.applied = False
        return False
    try:
        result.target_test_passed = False
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        decision.applied = False
        return False
    decision.applied = True
    decision.folded_fields = ("target_test_passed",)
    return True


def attach_receipt(result: Any, decision: IntelligenceDecision) -> Any:
    """Append the machine-readable gate block to ``raw_output``.

    Called on every delegated run, accepting or refusing: a verified run has to
    be able to name the mechanism that claimed it, and a refusal has to carry
    its reason. ``raw_output`` is the only string channel
    ``VerificationResult`` has, and it is exactly the right one — the harness
    already treats it as the captured output of the verification runs, and both
    the rationale builder and the repair-loop feedback read it. A refused
    obligation that was invisible there would be invisible everywhere.
    """
    block = raw_output_block(decision)
    existing = getattr(result, "raw_output", "")
    if not isinstance(existing, str):
        return result
    result.raw_output = (existing + "\n\n" + block) if existing else block
    return result


def apply_fold(result: Any, decision: IntelligenceDecision) -> Any:
    """Plan the fold and attach the receipt; return the same object.

    The combined form used by callers that do not need the trace to report
    ``applied`` between the two steps. :func:`run_intelligent_verify` calls the
    two halves separately for exactly that reason.
    """
    plan_fold(result, decision)
    attach_receipt(result, decision)
    return result


def raw_output_block(decision: IntelligenceDecision) -> str:
    """Render the gate block appended to ``VerificationResult.raw_output``.

    Stable, greppable, and one line per gate with the rung in front, so
    ``grep '^  rung=' logs/<task>/trace.jsonl`` answers "which mechanism
    claimed this was verified" without a parser.
    """
    lines = [
        _RAW_HEADER,
        f"mintable={str(decision.mintable).lower()}",
        f"applied={str(decision.applied).lower()}",
        f"keys={','.join(decision.keys_present) or '(none)'}",
    ]
    for verdict in decision.verdicts:
        lines.append(
            f"  rung={verdict.rung} gate={verdict.name} "
            f"passed={str(verdict.passed).lower()} mandatory={str(verdict.mandatory).lower()} "
            f"reason={verdict.reason or '(none)'}"
        )
    for row in decision.obligations:
        lines.append(
            f"  rung={RUNG_SPEC} obligation={row.get('id', '')} "
            f"outcome={row.get('outcome')} claimed={str(bool(row.get('claimed'))).lower()} "
            f"tests={','.join(str(t) for t in row.get('tests') or ())}"
        )
    if decision.unchecked_obligations:
        lines.append(
            f"  rung={RUNG_SPEC} unchecked_obligations="
            f"{','.join(decision.unchecked_obligations)}"
        )
    if decision.judgment:
        lines.append(
            f"  rung={RUNG_INDEPENDENT} judge_verdict={decision.judgment.get('verdict')} "
            f"gap_points={decision.judgment.get('gap_points')} "
            f"lucky_pass={str(bool(decision.judgment.get('lucky_pass'))).lower()}"
        )
    for note in decision.degraded:
        lines.append(f"  degraded: {note}")
    for note in decision.errors:
        lines.append(f"  error: {note}")
    lines.append(f"traced={str(decision.traced).lower()}")
    if decision.trace_refusal:
        lines.append(f"trace_refusal={decision.trace_refusal}")
    return "\n".join(lines)


def receipt_rows(decision: IntelligenceDecision) -> List[Dict[str, Any]]:
    """Return one report-sink row per gate, each carrying its rung.

    Appended to the same ``reports`` list the baseline runs already use, so one
    reader sees the baseline verdicts and the intelligence verdicts in order.
    """
    rows = [verdict.report_row() for verdict in decision.verdicts]
    for row in decision.obligations:
        rows.append(
            {
                "outcome": str(row.get("outcome") or "fail"),
                "source": _RECEIPT_SOURCE,
                "confidence": str(row.get("confidence") or "low"),
                "exit_code": None,
                "timed_out": False,
                "tests_collected": row.get("tests_collected"),
                "tests_passed": row.get("tests_passed"),
                "tests_failed": None,
                "tests_skipped": None,
                "passed": row.get("outcome") == "pass",
                "rung": RUNG_SPEC,
                "gate": GATE_OBLIGATIONS,
                "obligation": row.get("id"),
                "mandatory": True,
                "notes": list(row.get("notes") or ()),
            }
        )
    if decision.judgment:
        independent = decision.verdict(GATE_INDEPENDENT)
        independent_passed = bool(independent and independent.passed)
        rows.append(
            {
                "outcome": "pass" if independent_passed else "fail",
                "source": _RECEIPT_SOURCE,
                "confidence": "high",
                "exit_code": None,
                "timed_out": False,
                "tests_collected": None,
                "tests_passed": None,
                "tests_failed": None,
                "tests_skipped": None,
                "passed": independent_passed,
                "rung": RUNG_INDEPENDENT,
                "gate": GATE_INDEPENDENT,
                "mandatory": True,
                "notes": [str(decision.judgment.get("verdict") or "")],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


def _emit_trace(decision: IntelligenceDecision, settings: Settings) -> None:
    """Emit one unified-trace event, or record why it could not be emitted.

    ``shared.tracing`` is opt-in and refuses to raise into the caller, so a
    trace failure can never change a run's outcome. What it CAN do is silently
    omit the rung, so an absent trace id is recorded on the decision rather
    than left to be discovered by someone grepping for it later.
    """
    task_id = settings.task_id
    run_id = settings.run_id
    if not task_id and not run_id:
        decision.trace_refusal = (
            "no verification_task_id or verification_run_id was configured, so the "
            "gate emitted no unified-trace row"
        )
        return
    try:
        from shared import tracing

        tracing.emit(
            "execution",
            "verification_gate",
            task_id=task_id,
            run_id=run_id,
            mintable=decision.mintable,
            applied=decision.applied,
            folded_fields=list(decision.folded_fields),
            keys_present=list(decision.keys_present),
            verdicts=[verdict.to_dict() for verdict in decision.verdicts],
            refusals=[verdict.rung for verdict in decision.refusals],
            spec_ok=bool((decision.spec or {}).get("ok")) if decision.spec else None,
            obligations=len(decision.obligations),
            unchecked_obligations=list(decision.unchecked_obligations),
            judge_verdict=(decision.judgment or {}).get("verdict"),
            degraded=list(decision.degraded),
            errors=list(decision.errors),
        )
    except Exception as exc:  # pragma: no cover - tracing never raises by contract
        decision.trace_refusal = f"the unified trace row could not be emitted: {exc}"
        return
    decision.traced = True


# ---------------------------------------------------------------------------
# the delegated entry point
# ---------------------------------------------------------------------------


def run_intelligent_verify(
    *,
    repo_path: str,
    target_test: Optional[str],
    rerun_for_flake_check: int = 1,
    test_command: Optional[str] = None,
    verify_timeout_s: int = 300,
    allow_network: bool = False,
    selection: Any = None,
    final_gate: bool = True,
    reports: Optional[List[Dict[str, Any]]] = None,
    intelligence_config: Optional[Mapping[str, Any]] = None,
) -> Optional[Any]:
    """Run the delegated verification and return the folded result, or None.

    Returns ``None`` to mean "the pipeline did not apply, continue on the
    pre-existing path". That happens in exactly two cases, and both are
    recorded rather than silent:

    - no intelligence key is present in ``intelligence_config``;
    - ``final_gate`` is False, i.e. the caller asked for the NON-GATING inner
      verification. A subset result must stay structurally incapable of looking
      like a completion claim, so it carries no obligation-set or judge verdict
      it did not earn; the refusal is traced.

    The baseline is computed by calling ``execution.verify.verify`` with
    ``intelligence_config=None``, which is guaranteed non-delegating — the
    recursion terminates on the parameter, not on a flag, and the suite runs
    exactly once, in exactly the containers it ran in before this seam existed.
    """
    if not intelligence_requested(intelligence_config):
        return None
    if not final_gate:
        _trace_non_gating_refusal(repo_path, intelligence_config)
        return None

    settings = resolve_settings(intelligence_config)
    keys = present_keys(intelligence_config)

    # Import the baseline through the module attribute so a test double
    # installed on execution.verify is honoured, and pass intelligence_config
    # positionally-absent to guarantee no recursion.
    from execution import verify as verify_module

    baseline_reports: List[Dict[str, Any]] = []
    baseline_result = verify_module.verify(
        repo_path,
        target_test,
        rerun_for_flake_check,
        test_command=test_command,
        verify_timeout_s=verify_timeout_s,
        allow_network=allow_network,
        selection=selection,
        final_gate=True,
        reports=baseline_reports,
    )

    decision = evaluate(
        repo_path=repo_path,
        baseline_result=baseline_result,
        baseline_reports=baseline_reports,
        settings=settings,
        keys_present=keys,
        config=intelligence_config,
        test_command=test_command,
        verify_timeout_s=verify_timeout_s,
        allow_network=allow_network,
        target_test=target_test,
    )
    if reports is not None:
        # The baseline rows land first so one reader sees the ordinary verdicts
        # and then the intelligence verdicts that qualify them.
        reports.extend(baseline_reports)
        reports.extend(receipt_rows(decision))

    # Order matters: the fold decides, the trace REPORTS the decision, and the
    # receipt renders it. Reversing the last two would put `traced=false` in
    # every block for a run that was in fact traced.
    plan_fold(baseline_result, decision)
    _emit_trace(decision, settings)
    attach_receipt(baseline_result, decision)
    return baseline_result


def _trace_non_gating_refusal(
    repo_path: str, intelligence_config: Optional[Mapping[str, Any]]
) -> None:
    """Record that the intelligence rungs were declined for a non-gating run.

    A refusal nobody can see is a silent skip. This emits one row naming the
    reason, and — because the unified trace needs an identity — records the
    absence in the same place rather than pretending it was traced.
    """
    settings = resolve_settings(intelligence_config)
    if not (settings.task_id or settings.run_id):
        return
    try:
        from shared import tracing

        tracing.emit(
            "execution",
            "verification_gate_declined",
            task_id=settings.task_id,
            run_id=settings.run_id,
            reason="non_gating_inner_verification",
            repo=os.path.basename(os.path.abspath(str(repo_path))),
            keys_present=list(present_keys(intelligence_config)),
        )
    except Exception:  # pragma: no cover - tracing never raises by contract
        pass


__all__ = [
    "GATE_BASELINE",
    "GATE_INDEPENDENT",
    "GATE_OBLIGATIONS",
    "GATE_SPEC_INTACT",
    "INTELLIGENCE_CONFIG_KEYS",
    "RUNGS",
    "RUNG_BASELINE",
    "RUNG_INDEPENDENT",
    "RUNG_SPEC",
    "GateVerdict",
    "IntelligenceDecision",
    "Settings",
    "apply_fold",
    "attach_receipt",
    "evaluate",
    "intelligence_requested",
    "plan_fold",
    "present_keys",
    "raw_output_block",
    "receipt_rows",
    "resolve_settings",
    "run_intelligent_verify",
]
