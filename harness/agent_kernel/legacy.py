"""Legacy general-agent loop adapted as an authoritative kernel strategy."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .checkpoints import (
    CheckpointStore,
    checkpoint_identity,
    checkpoint_identity_matches,
    is_continuation_request,
    with_effort,
)
from .contracts import CompletionStatus, RunResult, RunSpec
from .events import JournalTraceLogger, RunEventJournal


class LegacyAgentStrategy:
    """Run the historical interactive loop through the canonical event journal."""

    def __init__(
        self,
        events: RunEventJournal,
        *,
        approval_callback: Any = None,
        plan_guidance: str = "",
        resume_history: str = "",
        session_context: Any = None,
    ) -> None:
        self.events = events
        self.approval_callback = approval_callback
        self.plan_guidance = str(plan_guidance or "")
        self.resume_history = str(resume_history or "")
        self.session_context = session_context
        self.legacy_result: Optional[Mapping[str, Any]] = None

    def cancel(self) -> None:
        """Retain the legacy loop's cooperative cancellation compatibility."""

    def run(self, spec: RunSpec, resume: bool = False) -> RunResult:
        """Execute the legacy loop once and translate its result."""
        spec.validate()
        identity = checkpoint_identity(
            spec.repository_identity,
            spec.request,
            workspace_policy=spec.workspace_policy,
            # AGT-08: the effort rung joins the resume identity, so a resume
            # that would change it is refused rather than performed silently.
            metadata=with_effort(spec.metadata, getattr(self, "config", None)),
            resume_namespace=spec.run_id,
        )
        checkpoint_path = str(self.events.path.parent / "checkpoint.json")
        checkpoint_store = CheckpointStore(
            checkpoint_path,
            session_id=spec.session_id,
            run_id=spec.run_id,
        )
        if resume:
            previous = checkpoint_store.load()
            identity_matches = previous is None or checkpoint_identity_matches(
                previous, identity
            )
            if (
                previous is not None
                and not identity_matches
                and is_continuation_request(spec.request)
                and checkpoint_identity_matches(
                    previous, identity, allow_request_alias=True
                )
            ):
                identity["request_identity"] = previous.request_identity
                identity_matches = True
            if previous is not None and (
                not identity_matches
                or (previous.session_id and previous.session_id != spec.session_id)
                or (previous.run_id and previous.run_id != spec.run_id)
            ):
                return RunResult(
                    status="blocked",
                    run_id=spec.run_id,
                    session_id=spec.session_id,
                    trace_path=str(self.events.path),
                    checkpoint_path=checkpoint_path,
                    verification_evidence=[
                        {
                            "kind": "resume_identity_mismatch",
                            "passed": False,
                            "description": "checkpoint identity does not match this run",
                        }
                    ],
                    follow_up_needs=["start a new run id"],
                    error="resume checkpoint identity mismatch",
                )
        from harness.agent_loop import _run_agent_legacy

        bridge = JournalTraceLogger(self.events)
        legacy = _run_agent_legacy(
            request=spec.request,
            repo_path=spec.repository_identity,
            config=dict(spec.metadata.get("config") or {}),
            log_root=self.events.path.parent.parent,
            task_id=spec.run_id,
            approve_fn=self.approval_callback,
            on_event=None,
            plan_guidance=self.plan_guidance,
            resume_history=self.resume_history,
            session_context=self.session_context,
            session_id=spec.session_id,
            _trace=bridge,
        )
        self.legacy_result = legacy
        legacy_status = str(legacy.get("status", "failed"))
        verification = legacy.get("verification")
        clean = bool(
            verification
            and verification.get("target_passed")
            and verification.get("regression_passed", True)
            and not verification.get("flaky")
        )
        if legacy_status == "success" and clean:
            status = CompletionStatus.COMPLETED_VERIFIED
        elif legacy_status == "success":
            status = CompletionStatus.COMPLETED_UNVERIFIED
        elif legacy_status == "timeout":
            status = CompletionStatus.TIMEOUT
        elif legacy_status in {"cancelled", "aborted"}:
            status = CompletionStatus.CANCELLED
        else:
            status = CompletionStatus.FAILED
        evidence = [dict(verification)] if isinstance(verification, Mapping) else []
        if not evidence:
            evidence = [
                {
                    "kind": "legacy_completion"
                    if status == CompletionStatus.COMPLETED_UNVERIFIED
                    else "legacy_failure",
                    "passed": status == CompletionStatus.COMPLETED_UNVERIFIED,
                    "description": str(legacy.get("answer") or legacy_status),
                }
            ]
        checkpoint_path = str(self.events.path.parent / "checkpoint.json")
        checkpoint_store.make_checkpoint(
            last_event_sequence=self.events.last_sequence,
            agent_owned_changes=list(legacy.get("files_touched") or []),
            spend=float(legacy.get("cost_usd", 0.0) or 0.0),
            turn_id=spec.turn_id,
            identity=identity,
        )
        return RunResult(
            status=status,
            answer=str(legacy.get("answer") or ""),
            changed_files=list(legacy.get("files_touched") or []),
            verification_evidence=evidence,
            cost=float(legacy.get("cost_usd", 0.0) or 0.0),
            attempts=int(legacy.get("attempts", 1) or 1),
            resume_availability="unavailable",
            follow_up_needs=[]
            if status
            in {
                CompletionStatus.COMPLETED_VERIFIED,
                CompletionStatus.COMPLETED_UNVERIFIED,
            }
            else [legacy_status],
            run_id=spec.run_id,
            session_id=spec.session_id,
            trace_path=str(self.events.path),
            checkpoint_path=checkpoint_path,
            diff=str(legacy.get("diff") or ""),
            model_calls=list(legacy.get("model_calls") or []),
            error=""
            if status
            in {
                CompletionStatus.COMPLETED_VERIFIED,
                CompletionStatus.COMPLETED_UNVERIFIED,
            }
            else legacy_status,
            metadata={"legacy_status": legacy_status},
        )
