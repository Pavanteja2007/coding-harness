"""TaskResult <-> dict serialization (used across the process boundary).

TaskResult contains nested dataclasses and a log_path; worker processes
build it, the scheduler reads JSON off disk. Assumes TaskResult fields match
shared/types.py exactly (contract types — never change shape here).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from shared.types import TaskResult, VerificationResult


def _verification_to_dict(v: Optional[VerificationResult]) -> Optional[Dict[str, Any]]:
    if v is None:
        return None
    return {
        "target_test_passed": v.target_test_passed,
        "baseline_passed": v.baseline_passed,
        "regression_passed": v.regression_passed,
        "flaky": v.flaky,
        "raw_output": v.raw_output,
    }


def _verification_from_dict(d: Optional[Dict[str, Any]]) -> Optional[VerificationResult]:
    if d is None:
        return None
    return VerificationResult(
        target_test_passed=bool(d.get("target_test_passed", False)),
        baseline_passed=bool(d.get("baseline_passed", False)),
        regression_passed=bool(d.get("regression_passed", False)),
        flaky=bool(d.get("flaky", False)),
        raw_output=str(d.get("raw_output", "")),
    )


def result_to_dict(r: TaskResult) -> Dict[str, Any]:
    """TaskResult -> plain dict for JSON transport across processes."""
    return {
        "task_id": r.task_id,
        "status": r.status,
        "attempts": r.attempts,
        "diff": r.diff,
        "verification": _verification_to_dict(r.verification),
        "cost_usd": r.cost_usd,
        "model_calls": r.model_calls,
        "log_path": r.log_path,
    }


def result_from_dict(d: Dict[str, Any]) -> TaskResult:
    """Plain dict (from result_to_dict) -> TaskResult. Tolerates a few
    missing keys (writes safe defaults) so an old journal replays cleanly.
    """
    return TaskResult(
        task_id=str(d.get("task_id", "")),
        status=d.get("status", "error"),
        attempts=int(d.get("attempts", 0)),
        diff=d.get("diff"),
        verification=_verification_from_dict(d.get("verification")),
        cost_usd=float(d.get("cost_usd", 0.0)),
        model_calls=list(d.get("model_calls", [])),
        log_path=str(d.get("log_path", "")),
    )
