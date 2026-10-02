"""Completion and verification policy for daily coding work."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .contracts import CompletionStatus, RunResult, RunSpec

Verifier = Callable[..., Any]


@dataclass
class CompletionDecision:
    """Internal completion decision before a RunResult is assembled."""

    status: str
    answer: str = ""
    evidence: List[Dict[str, Any]] = None
    follow_up_needs: List[str] = None

    def __post_init__(self) -> None:
        self.evidence = list(self.evidence or [])
        self.follow_up_needs = list(self.follow_up_needs or [])


class CompletionPolicy:
    """Turn model finish requests into honest, evidence-backed outcomes."""

    def __init__(
        self,
        verifier: Optional[Verifier] = None,
        *,
        config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.config = dict(config or {})
        self._verifier = verifier
        self.checks_performed: List[Dict[str, Any]] = []

    @property
    def has_verifier(self) -> bool:
        """Return whether a verification callable is available."""
        if self._verifier is not None:
            return True
        try:
            from harness.deps import get_verify

            return callable(get_verify())
        except Exception:
            return False

    def verify(
        self,
        repo_path: str,
        spec: RunSpec,
        *,
        event: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run the declared verifier, or return None when none is declared."""
        policy = dict(spec.verification_policy or {})
        target = policy.get("target_test", self.config.get("target_test"))
        command = policy.get(
            "test_command", policy.get("command", self.config.get("test_command"))
        )
        if not target and not command:
            self.checks_performed.append(
                {
                    "kind": "verification_unavailable",
                    "passed": False,
                    "description": "No target test or project verification command was declared.",
                }
            )
            return None
        verifier = self._verifier
        if verifier is None:
            try:
                from harness.deps import get_verify

                verifier = get_verify()
            except Exception as exc:
                evidence = {
                    "kind": "verification_error",
                    "passed": False,
                    "error": str(exc),
                    "description": "The configured verifier could not be loaded.",
                }
                self.checks_performed.append(evidence)
                if event:
                    event("verification", evidence)
                return evidence
        try:
            result = verifier(
                repo_path,
                target,
                rerun_for_flake_check=int(self.config.get("baseline_reruns", 1)),
                test_command=command,
                verify_timeout_s=int(self.config.get("verify_timeout_s", 300)),
            )
        except TypeError:
            try:
                result = verifier(repo_path, target, test_command=command)
            except Exception as exc:
                result = {
                    "target_passed": False,
                    "regression_passed": False,
                    "error": str(exc),
                }
        except Exception as exc:
            result = {
                "target_passed": False,
                "regression_passed": False,
                "error": str(exc),
            }
        evidence = self._normalize_result(result)
        evidence.update(
            {
                "kind": "verification",
                "target_test": target,
                "test_command": command,
                "performed_at": time.time(),
            }
        )
        self.checks_performed.append(evidence)
        if event:
            event("verification", evidence)
        return evidence

    def finish(
        self,
        spec: RunSpec,
        *,
        repo_path: str = "",
        answer: str = "",
        changed_files: Optional[Sequence[str]] = None,
        verification: Optional[Mapping[str, Any]] = None,
        checks: Optional[Sequence[Mapping[str, Any]]] = None,
        cost: float = 0.0,
        trace_path: str = "",
        checkpoint_path: str = "",
        diff: str = "",
        model_calls: Optional[List[Dict[str, Any]]] = None,
        event: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> RunResult:
        """Finalize a finish request without treating model text as proof."""
        evidence = (
            [dict(item) for item in (verification or {}).get("evidence", [])]
            if verification
            else []
        )
        if verification and isinstance(verification, Mapping):
            evidence = [dict(verification)]
        performed = [dict(item) for item in (checks or self.checks_performed)]
        for item in performed:
            if item not in evidence:
                evidence.append(item)
        if not verification:
            performed.append(
                {
                    "kind": "model_finish",
                    "passed": False,
                    "description": "The model requested completion without verifier evidence.",
                }
            )
            evidence.extend(performed[-1:])
            status = "completed_unverified"
        else:
            passed = bool(
                verification.get("target_passed", False)
                and verification.get("regression_passed", True)
                and not verification.get("flaky", False)
                and not verification.get("error")
            )
            status = "completed_verified" if passed else "failed"
            if not passed:
                performed.append(
                    {
                        "kind": "verification_failed",
                        "passed": False,
                        "description": "Declared verification did not pass cleanly.",
                    }
                )
                evidence.append(performed[-1])
        return RunResult(
            status=status,
            answer=str(answer or ""),
            changed_files=list(changed_files or []),
            verification_evidence=evidence,
            cost=float(cost or 0.0),
            resume_availability="available"
            if _checkpoint_exists(checkpoint_path)
            else "unavailable",
            follow_up_needs=[]
            if status in {"completed_verified", "completed_unverified"}
            else performed,
            run_id=spec.run_id,
            session_id=spec.session_id,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
            diff=diff,
            model_calls=list(model_calls or []),
        )

    def timeout(
        self,
        spec: RunSpec,
        reason: str = "wall-clock limit",
        *,
        changed_files: Optional[Sequence[str]] = None,
        cost: float = 0.0,
        trace_path: str = "",
        checkpoint_path: str = "",
    ) -> RunResult:
        """Build a resumable timeout result."""
        return RunResult(
            status=CompletionStatus.TIMEOUT,
            answer=str(reason or "wall-clock limit"),
            changed_files=list(changed_files or []),
            verification_evidence=[
                {"kind": "timeout", "passed": False, "description": str(reason)}
            ],
            cost=float(cost or 0.0),
            resume_availability="available"
            if _checkpoint_exists(checkpoint_path)
            else "unavailable",
            follow_up_needs=[str(reason)],
            run_id=spec.run_id,
            session_id=spec.session_id,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
            error=str(reason),
        )

    def blocked(
        self,
        spec: RunSpec,
        reason: str,
        *,
        changed_files: Optional[Sequence[str]] = None,
        cost: float = 0.0,
        trace_path: str = "",
        checkpoint_path: str = "",
    ) -> RunResult:
        """Build a terminal blocked result for a denied call."""
        return RunResult(
            status="blocked",
            answer="",
            changed_files=list(changed_files or []),
            verification_evidence=[
                {"kind": "permission_denied", "passed": False, "description": reason}
            ],
            cost=float(cost or 0.0),
            resume_availability="available"
            if _checkpoint_exists(checkpoint_path)
            else "unavailable",
            follow_up_needs=[reason],
            run_id=spec.run_id,
            session_id=spec.session_id,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
            error=reason,
        )

    def cancelled(
        self,
        spec: RunSpec,
        reason: str = "cancelled by user",
        *,
        changed_files: Optional[Sequence[str]] = None,
        cost: float = 0.0,
        trace_path: str = "",
        checkpoint_path: str = "",
    ) -> RunResult:
        """Build a resumable cancellation result."""
        return RunResult(
            status="cancelled",
            answer=str(reason or "cancelled by user"),
            changed_files=list(changed_files or []),
            verification_evidence=[
                {"kind": "cancelled", "passed": False, "description": str(reason)}
            ],
            cost=float(cost or 0.0),
            resume_availability="available"
            if _checkpoint_exists(checkpoint_path)
            else "unavailable",
            follow_up_needs=[],
            run_id=spec.run_id,
            session_id=spec.session_id,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
        )

    def needs_input(
        self,
        spec: RunSpec,
        question: str,
        *,
        changed_files: Optional[Sequence[str]] = None,
        cost: float = 0.0,
        trace_path: str = "",
        checkpoint_path: str = "",
    ) -> RunResult:
        """Build a result for an explicit ask or missing required input."""
        return RunResult(
            status="needs_input",
            answer=question,
            changed_files=list(changed_files or []),
            verification_evidence=[],
            cost=float(cost or 0.0),
            resume_availability="available"
            if _checkpoint_exists(checkpoint_path)
            else "unavailable",
            follow_up_needs=[question],
            run_id=spec.run_id,
            session_id=spec.session_id,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
        )

    @staticmethod
    def _normalize_result(result: Any) -> Dict[str, Any]:
        if isinstance(result, Mapping):
            data = dict(result)
            return {
                **data,
                "target_passed": bool(
                    data.get("target_passed", data.get("target_test_passed", False))
                ),
                "regression_passed": bool(
                    data.get("regression_passed", data.get("full_suite_passed", False))
                ),
                "flaky": bool(data.get("flaky", False)),
            }
        if isinstance(result, bool):
            return {
                "target_passed": result,
                "regression_passed": result,
                "flaky": False,
            }
        return {
            "target_passed": bool(getattr(result, "target_test_passed", False)),
            "regression_passed": bool(getattr(result, "regression_passed", False)),
            "flaky": bool(getattr(result, "flaky", False)),
            "raw": str(getattr(result, "raw_output", "")),
        }


def _checkpoint_exists(path: str) -> bool:
    """Return whether a concrete checkpoint artifact is present."""
    return bool(path and Path(path).is_file())
