"""Verification intelligence: the gate that decides, not the model (Ceiling 08).

`execution.verify` answers "did these tests pass on this state". This module
answers the harder question — "is there enough independent evidence to call this
done" — by composing five mechanisms that are individually useless and jointly
hard to fool:

1. :mod:`execution.spec_ledger`      the machine-checkable obligation set
2. :mod:`execution.test_selection`   the cheap inner gate for the repair loop
3. :mod:`execution.independent_evidence` held-out tests + a separate judge
4. :mod:`execution.flake`            clean-environment failure confirmation
5. :mod:`execution.result_parsing`   report-first, exit-code-second verdicts

The mint rule, stated once and enforced structurally:

    Only the FINAL GATE can mint success. The final gate is the full
    autodetected suite, it is never skipped because an inner run was green, and
    it additionally requires an intact spec, a non-flaky target, and — when a
    held-out suite is configured — an independent judge that did not detect a
    lucky pass or tampering.

:meth:`VerificationOutcome.mint_success` is the only success-shaped method on
this object, it returns ``False`` unless the final gate actually ran and passed,
and it never consults a caller-supplied claim. :attr:`final_result` refuses to
return a gating result at all when the final gate did not run, so a caller
cannot reach a `VerificationResult` that says "regression passed" for a subset
run by accident.

:func:`run_verification` composes the whole thing. It is honest about absence:
when no spec artifact exists, no held-out suite is configured, or the final gate
cannot run, the corresponding gate is reported ``skipped`` with a reason rather
than passing silently — and a *skipped mandatory gate* fails the outcome.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from execution import verify as _verify_module
from execution.flake import (
    CLASSIFICATION_UNCONFIRMED,
    FlakeAssessment,
    FlakeLedger,
    assess_failure,
)
from execution.independent_evidence import (
    DEFAULT_GAP_THRESHOLD_POINTS,
    HeldOutSuite,
    Judgment,
    premature_completion,
)
from execution.independent_evidence import (
    judge as judge_held_out,
)
from execution.result_parsing import TestRunReport
from execution.spec_ledger import SpecGuardReport, load_or_report
from execution.test_selection import TestSelection, save_selection, select_tests
from shared.types import VerificationResult

GATE_SPEC_INTACT = "spec_intact"
GATE_SPEC_CLAIMED = "spec_claimed"
GATE_TARGET = "target_passed"
GATE_FULL_SUITE = "full_suite_passed"
GATE_NOT_FLAKY = "not_flaky"
GATE_INDEPENDENT = "independent_evidence"
GATE_NO_PREMATURE = "no_premature_completion"

#: Gates that must be satisfied (not merely reported) for a verified outcome.
MANDATORY_GATES: Tuple[str, ...] = (
    GATE_TARGET,
    GATE_FULL_SUITE,
    GATE_NOT_FLAKY,
    GATE_NO_PREMATURE,
)

STATUS_VERIFIED = "verified"
STATUS_FAILED = "failed"
STATUS_INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict, with the reason it reached it."""

    name: str
    passed: bool
    mandatory: bool
    reason: str = ""
    skipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view."""
        return {
            "name": self.name,
            "passed": bool(self.passed),
            "mandatory": bool(self.mandatory),
            "skipped": bool(self.skipped),
            "reason": self.reason,
        }


@dataclass
class VerificationOutcome:
    """The full evidence bundle for one task state, plus the mint decision."""

    status: str
    result: Optional[VerificationResult]
    inner_result: Optional[VerificationResult] = None
    final_gate_ran: bool = False
    suite_scope: str = "full"
    gates: Tuple[GateResult, ...] = ()
    reports: Tuple[Dict[str, Any], ...] = ()
    selection: Optional[TestSelection] = None
    spec: Optional[SpecGuardReport] = None
    flake: Optional[FlakeAssessment] = None
    flake_ledger: Optional[FlakeLedger] = None
    judgment: Optional[Judgment] = None
    premature: Mapping[str, Any] = field(default_factory=dict)
    timings_ms: Mapping[str, float] = field(default_factory=dict)
    errors: Tuple[str, ...] = ()

    # -- the mint rule ----------------------------------------------------

    def mint_success(self) -> bool:
        """Return whether this outcome may be reported as verified success.

        False unless the final gate RAN, the regression scope was the full
        suite, and EVERY gate marked mandatory passed. A mandatory gate that was
        skipped counts as a failure. Nothing here consults a caller-supplied
        claim: the verdict is computed from gate results alone.
        """
        if not (self.final_gate_ran and self.result is not None):
            return False
        if self.suite_scope != "full":
            return False
        for gate in self.gates:
            if gate.mandatory and not gate.passed:
                return False
        return not (self.judgment is not None and not self.judgment.verified)

    def final_result(self) -> VerificationResult:
        """Return the gating result, refusing when the final gate did not run.

        Raising rather than returning a subset result is deliberate: a caller
        that reaches for a `VerificationResult` to decide "did it pass" cannot
        accidentally read a non-gating inner result.
        """
        if not self.final_gate_ran or self.result is None:
            raise RuntimeError(
                "final gate did not run; there is no gating VerificationResult"
            )
        return self.result

    def gate(self, name: str) -> Optional[GateResult]:
        """Return one gate by name, or None."""
        for candidate in self.gates:
            if candidate.name == name:
                return candidate
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible evidence bundle for traces and reports."""
        return {
            "status": self.status,
            "final_gate_ran": bool(self.final_gate_ran),
            "suite_scope": self.suite_scope,
            "mint_success": self.mint_success(),
            "gates": [gate.to_dict() for gate in self.gates],
            "reports": [dict(item) for item in self.reports],
            "selection": self.selection.to_dict() if self.selection else None,
            "spec": self.spec.to_dict() if self.spec else None,
            "flake": self.flake.to_dict() if self.flake else None,
            "flake_ledger": self.flake_ledger.to_dict() if self.flake_ledger else None,
            "judgment": self.judgment.to_dict() if self.judgment else None,
            "premature": dict(self.premature),
            "timings_ms": dict(self.timings_ms),
            "errors": list(self.errors),
            "result": None
            if self.result is None
            else {
                "target_test_passed": bool(self.result.target_test_passed),
                "baseline_passed": bool(self.result.baseline_passed),
                "regression_passed": bool(self.result.regression_passed),
                "flaky": bool(self.result.flaky),
                "has_raw_output": bool(self.result.raw_output),
            },
            "inner_result": None
            if self.inner_result is None
            else {
                "target_test_passed": bool(self.inner_result.target_test_passed),
                "regression_passed": bool(self.inner_result.regression_passed),
                "flaky": bool(self.inner_result.flaky),
                "non_gating": True,
            },
        }


def run_verification(
    repo_path: str,
    *,
    target_test: Optional[str] = None,
    changed_files: Optional[Sequence[str]] = None,
    test_command: Optional[str] = None,
    verify_timeout_s: int = 300,
    rerun_for_flake_check: int = 2,
    run_dir: str = "",
    spec_root: str = "",
    require_spec: bool = False,
    held_out: Optional[HeldOutSuite] = None,
    held_out_run: Optional[Callable[[str, HeldOutSuite], Any]] = None,
    held_out_fingerprint: str = "",
    gap_threshold_points: float = DEFAULT_GAP_THRESHOLD_POINTS,
    flake_confirm_attempts: int = 2,
    confirm_runner: Optional[Callable[[str], Any]] = None,
    allow_network: bool = False,
    max_selection_files: int = 12,
    claimed_complete: bool = False,
) -> VerificationOutcome:
    """Run the full verification-intelligence gate for one repo state.

    Order matters and is deliberate:

    1. the SPEC guard runs first and cheaply, so an agent that deleted an
       obligation never reaches the expensive stages;
    2. the INCREMENTAL selection is computed and persisted, then the inner
       (non-gating) verification runs, because that is the loop's fast feedback;
    3. the FINAL GATE runs the FULL suite and is the only minting path;
    4. a failing target is CONFIRMED in a clean environment before it becomes
       an edit instruction, and the unconfirmed case is reported, not fixed;
    5. the independent judge runs LAST, on its own clean copy, with the held-out
       suite revealed.

    ``confirm_runner`` is called with a clean repo copy and must return something
    :func:`execution.result_parsing.parse_test_run` accepts. Without it, a
    failing target is honestly reported ``unconfirmed`` and becomes no edit
    instruction — which is the documented policy, not a missing feature.
    """
    errors: List[str] = []
    reports: List[Dict[str, Any]] = []
    timings: Dict[str, float] = {}

    # -- 1. spec guard ----------------------------------------------------
    spec_report: Optional[SpecGuardReport] = None
    spec_root = spec_root or (run_dir or repo_path)
    if require_spec or _spec_exists(spec_root):
        spec_report = load_or_report(spec_root)
    spec_gate = _spec_gate(spec_report, require_spec=require_spec)

    # -- 2. incremental selection + inner verification -------------------
    selection: Optional[TestSelection] = None
    inner_result: Optional[VerificationResult] = None
    inner_started = time.time()
    try:
        selection_started = time.time()
        selection = select_tests(
            repo_path,
            list(changed_files or []),
            max_files=max_selection_files,
            target_test=target_test,
        )
        timings["selection_ms"] = round((time.time() - selection_started) * 1000.0, 3)
        if run_dir:
            from execution.test_selection import default_selection_path

            try:
                save_selection(default_selection_path(run_dir), selection)
            except OSError as exc:
                errors.append(f"selection could not be persisted: {exc}")
        inner_started_inner = time.time()
        inner_result, selection = _verify_module.inner_verify(
            repo_path,
            list(changed_files or []),
            target_test=target_test,
            selection=selection,
            test_command=test_command,
            verify_timeout_s=verify_timeout_s,
            rerun_for_flake_check=rerun_for_flake_check,
            allow_network=allow_network,
            reports=reports,
        )
        timings["inner_verify_ms"] = round(
            (time.time() - inner_started_inner) * 1000.0, 3
        )
    except Exception as exc:
        errors.append(f"inner verification unavailable: {type(exc).__name__}: {exc}")
    timings["inner_ms"] = round((time.time() - inner_started) * 1000.0, 3)

    # -- 3. final gate: FULL suite, never the selection ------------------
    final_started = time.time()
    final_result: Optional[VerificationResult] = None
    final_reports: List[Dict[str, Any]] = []
    try:
        final_result = _verify_module.verify(
            repo_path,
            target_test,
            rerun_for_flake_check,
            test_command=test_command,
            verify_timeout_s=verify_timeout_s,
            allow_network=allow_network,
            selection=selection,
            final_gate=True,
            reports=final_reports,
        )
    except Exception as exc:
        errors.append(f"final gate could not run: {type(exc).__name__}: {exc}")
    timings["final_ms"] = round((time.time() - final_started) * 1000.0, 3)
    reports.extend(final_reports)

    final_ran = final_result is not None
    suite_scope = "full"
    target_passed = bool(final_result.target_test_passed) if final_result else False
    suite_passed = bool(final_result.regression_passed) if final_result else False
    flaky = bool(final_result.flaky) if final_result else True

    # -- 4. flake confirmation in a clean environment --------------------
    ledger = FlakeLedger()
    assessment: Optional[FlakeAssessment] = None
    if not target_passed and final_ran:
        failing = _failing_report(final_reports)
        assessment = ledger.add(
            assess_failure(
                failing,
                run=confirm_runner,
                repo_path=repo_path,
                attempts=flake_confirm_attempts,
                clean=True,
                label=target_test or "the target test",
            )
        )

    # -- 5. independent judge on its own clean copy ----------------------
    judgment: Optional[Judgment] = None
    judge_copy = ""
    judge_staging = ""
    if held_out is not None:
        judge_copy, judge_staging = _clean_copy_for_judge(repo_path)
        try:
            judgment = judge_held_out(
                held_out,
                run=held_out_run or _default_held_out_runner,
                visible_report=_summary_report(final_reports),
                claims={
                    "target_test": target_test,
                    "changed_files": list(changed_files or []),
                },
                expected_fingerprint=held_out_fingerprint
                or held_out.sealed_fingerprint,
                threshold_points=gap_threshold_points,
                repo_path=repo_path,
                clean_repo_path=judge_copy,
            )
        finally:
            if judge_staging:
                shutil.rmtree(judge_staging, ignore_errors=True)

    premature = premature_completion(
        claimed_complete=claimed_complete,
        final_gate_ran=final_ran,
        final_gate_passed=bool(target_passed and suite_passed and not flaky),
        spec_guard_ok=None if spec_report is None else bool(spec_report.ok),
    )

    gates: List[GateResult] = [
        spec_gate,
        _spec_claimed_gate(spec_report, final_passed=target_passed and suite_passed),
        GateResult(
            GATE_TARGET,
            target_passed,
            True,
            "the target test passed in the final gate"
            if target_passed
            else _failure_reason(final_reports, "the target test did not pass"),
        ),
        GateResult(
            GATE_FULL_SUITE,
            suite_passed,
            True,
            f"the full suite ran in the final gate (scope={suite_scope})"
            if suite_passed
            else _failure_reason(final_reports, "the full suite did not pass"),
        ),
        GateResult(
            GATE_NOT_FLAKY,
            not flaky,
            True,
            "the target was consistent across reruns"
            if not flaky
            else "the target produced different outcomes across reruns",
        ),
        GateResult(
            GATE_NO_PREMATURE,
            not bool(premature.get("premature")),
            True,
            str(premature.get("reason") or ""),
        ),
    ]
    if held_out is not None:
        gates.append(
            GateResult(
                GATE_INDEPENDENT,
                bool(judgment and judgment.verified),
                True,
                "the independent judge accepted the claim"
                if judgment and judgment.verified
                else _judgment_reason(judgment),
            )
        )

    outcome = VerificationOutcome(
        status=STATUS_INDETERMINATE,
        result=final_result,
        inner_result=inner_result,
        final_gate_ran=final_ran,
        suite_scope=suite_scope,
        gates=tuple(gates),
        reports=tuple(reports),
        selection=selection,
        spec=spec_report,
        flake=assessment,
        flake_ledger=ledger,
        judgment=judgment,
        premature=premature,
        timings_ms=timings,
        errors=tuple(errors),
    )
    if not final_ran:
        outcome.status = STATUS_INDETERMINATE
    else:
        outcome.status = STATUS_VERIFIED if outcome.mint_success() else STATUS_FAILED
    return outcome


# ---------------------------------------------------------------------------
# gate helpers
# ---------------------------------------------------------------------------


def _spec_exists(root: str) -> bool:
    """Return whether a spec artifact is present under ``root``."""
    from execution.spec_ledger import guard_paths

    artifact, _seal = guard_paths(str(root))
    return os.path.isfile(artifact)


def _spec_gate(report: Optional[SpecGuardReport], *, require_spec: bool) -> GateResult:
    """Turn a spec guard report into a gate result.

    An absent spec is a SKIPPED optional gate when the caller did not require
    one, and a FAILING mandatory gate when they did. It is never reported as a
    pass.
    """
    if report is None:
        return GateResult(
            GATE_SPEC_INTACT,
            not require_spec,
            not require_spec,
            reason="no spec artifact was supplied; spec gating not requested"
            if not require_spec
            else "a spec artifact was required but none was found",
            skipped=not require_spec,
        )
    return GateResult(
        GATE_SPEC_INTACT,
        bool(report.ok),
        True,
        "the spec obligation set is intact"
        if report.ok
        else "; ".join(report.violations) or "the spec obligation set changed",
    )


def _spec_claimed_gate(
    report: Optional[SpecGuardReport], *, final_passed: bool
) -> GateResult:
    """Check that ``passes`` flips are backed by a clean final gate.

    Flipping ``passes`` is the one edit an agent is allowed, so the flip has to
    mean something. A claim made while the final gate is red is an unbacked
    claim, and the gate says so instead of letting the report read as done.
    """
    if report is None:
        return GateResult(
            GATE_SPEC_CLAIMED,
            True,
            False,
            reason="no spec artifact; nothing to back up",
            skipped=True,
        )
    claimed = list(report.claimed)
    if not claimed:
        return GateResult(
            GATE_SPEC_CLAIMED,
            True,
            False,
            reason="no spec item is claimed",
            skipped=False,
        )
    if final_passed:
        return GateResult(
            GATE_SPEC_CLAIMED,
            True,
            False,
            reason=f"{len(claimed)} claimed item(s) backed by a clean final gate",
        )
    return GateResult(
        GATE_SPEC_CLAIMED,
        False,
        False,
        reason=(
            "claimed item(s) are not backed by a passing final gate: "
            + ", ".join(claimed)
        ),
    )


def _failing_report(reports: Sequence[Mapping[str, Any]]) -> TestRunReport:
    """Return the last non-passing report, or a synthetic failure.

    When every report somehow parsed as a pass, a synthetic ``fail`` report is
    returned so the confirmation path still runs and the inconsistency shows up
    as a confirmed regression rather than a silent skip.
    """
    for item in reversed(list(reports)):
        if item.get("outcome") != "pass":
            return TestRunReport(
                outcome=str(item.get("outcome") or "fail"),
                exit_code=item.get("exit_code"),
                tests_collected=item.get("tests_collected"),
                tests_passed=item.get("tests_passed"),
                tests_failed=item.get("tests_failed"),
                source=str(item.get("source") or "exit_code"),
                confidence=str(item.get("confidence") or "low"),
            )
    return TestRunReport(
        outcome="fail",
        source="exit_code",
        confidence="low",
        notes=("no failing report was recorded; treating the run as unconfirmed",),
    )


def _summary_report(reports: Sequence[Mapping[str, Any]]) -> Optional[TestRunReport]:
    """Return an aggregate report for the visible suite, or None when empty."""
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


def _failure_reason(reports: Sequence[Mapping[str, Any]], fallback: str) -> str:
    """Return the first non-passing outcome label with its notes."""
    for item in reports:
        if item.get("outcome") not in (None, "pass", "info"):
            notes = "; ".join(str(note) for note in item.get("notes") or ())
            return (
                f"{item.get('outcome')}: {notes}" if notes else str(item.get("outcome"))
            )
    return fallback


def _judgment_reason(judgment: Optional[Judgment]) -> str:
    """Return why the independent judge did not accept the claim."""
    if judgment is None:
        return "the independent judge did not run"
    if judgment.reasons:
        return "; ".join(judgment.reasons)
    return f"verdict={judgment.verdict}"


def _clean_copy_for_judge(repo_path: str) -> Tuple[str, str]:
    """Return ``(copy_path, staging_root)`` for an independent judge context.

    The copy lives in a SHORT system-temp directory rather than under the run
    directory, and that is not cosmetic. Docker Desktop bind mounts a host path
    into the container, and a deeply nested run directory produces
    ``OSError: [Errno 5] Input/output error`` on ordinary reads INSIDE the
    mount — an environment failure that reads exactly like a code failure and
    would send the judge chasing a defect that is not there. A short staging
    path keeps the judge's reads working.

    The judge's own copy is what makes the verdict independent of whatever is
    lying around in the builder's working tree. Failures fall back to the
    original path with an empty staging root, which is honest (the judge still
    runs and the scope it used is visible) rather than silently substituting a
    second verdict.
    """
    try:
        from execution.independent_evidence import copy_tree

        staging = tempfile.mkdtemp(prefix="neojudge")
        target = os.path.join(staging, "repo")
        return copy_tree(str(repo_path), target), staging
    except (OSError, ValueError):
        return str(repo_path), ""


def _default_held_out_runner(repo_path: str, suite: HeldOutSuite) -> TestRunReport:
    """Run the held-out suite on the host when no runner was supplied.

    Deliberately explicit and out of the Docker path: the judge is an
    EVALUATOR context, and a caller that wants the held-out run inside the
    sandbox passes its own ``held_out_run``. The report it returns records that
    it came from a host process so nothing downstream can call it a sandbox
    pass.
    """
    import sys

    from execution.flake import run_local_command

    return run_local_command(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", suite.root],
        cwd=str(repo_path),
        timeout_s=120,
    )


__all__ = [
    "CLASSIFICATION_UNCONFIRMED",
    "GATE_FULL_SUITE",
    "GATE_INDEPENDENT",
    "GATE_NOT_FLAKY",
    "GATE_NO_PREMATURE",
    "GATE_SPEC_CLAIMED",
    "GATE_SPEC_INTACT",
    "GATE_TARGET",
    "MANDATORY_GATES",
    "STATUS_FAILED",
    "STATUS_INDETERMINATE",
    "STATUS_VERIFIED",
    "GateResult",
    "VerificationOutcome",
    "run_verification",
]
