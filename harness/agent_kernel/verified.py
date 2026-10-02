"""Compatibility strategy for the verifier-gated legacy fix engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .checkpoints import (
    CheckpointStore,
    checkpoint_identity,
    checkpoint_identity_matches,
    is_continuation_request,
    with_effort,
)
from .contracts import CompletionStatus, RunResult, RunSpec
from .events import JournalTraceLogger, RunEventJournal


class VerifiedFixStrategy:
    """Delegate verified fixing to the private legacy loop through one journal."""

    def __init__(
        self,
        log_root: Any = None,
        task_factory: Any = None,
        runner: Optional[Callable[..., Any]] = None,
        events: Optional[RunEventJournal] = None,
    ) -> None:
        self.log_root = log_root
        self.task_factory = task_factory
        self.runner = runner
        self.events = events
        self.legacy_result: Any = None

    def cancel(self) -> None:
        """Retain the verified strategy cancellation compatibility no-op."""

    def run(self, spec: RunSpec, resume: bool = False) -> RunResult:
        """Run the legacy fix engine and translate its stable result."""
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
        checkpoint_store = None
        checkpoint_path = ""
        if self.events is not None:
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
                        status=CompletionStatus.BLOCKED,
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
        from shared.types import Task

        config = dict(spec.metadata.get("config") or {})
        config.update(spec.verification_policy)
        config.update(spec.workspace_policy)
        if resume:
            config["resume"] = True
        task = (
            self.task_factory(spec, config)
            if self.task_factory is not None
            else Task(
                task_id=spec.run_id,
                repo_path=spec.repository_identity,
                issue_text=spec.request,
                config=config,
            )
        )
        runner = self.runner
        if runner is None:
            from harness.core import _run_task_legacy

            runner = _run_task_legacy
        bridge = JournalTraceLogger(self.events) if self.events is not None else None
        if bridge is None:
            legacy = runner(task, log_root=self.log_root)
        else:
            legacy = runner(
                task,
                log_root=self.log_root,
                _trace=bridge,
                _reuse_run_dir=True,
            )
        self.legacy_result = legacy
        legacy_status = str(_value(legacy, "status", "failed"))
        verification = _value(legacy, "verification", None)
        target_passed = bool(
            _value(
                verification,
                "target_test_passed",
                _value(verification, "target_passed", False),
            )
            if verification is not None
            else False
        )
        regression_passed = bool(
            _value(verification, "regression_passed", False)
            if verification is not None
            else False
        )
        flaky = bool(
            _value(verification, "flaky", False) if verification is not None else False
        )
        clean = target_passed and regression_passed and not flaky
        if legacy_status == "success" and clean:
            status = CompletionStatus.COMPLETED_VERIFIED
        elif legacy_status == "timeout":
            status = CompletionStatus.TIMEOUT
        elif legacy_status == "success":
            status = CompletionStatus.FAILED
        else:
            status = CompletionStatus.FAILED
        evidence = [
            {
                "kind": "verification",
                "passed": clean,
                "target_passed": target_passed,
                "regression_passed": regression_passed,
                "flaky": flaky,
                "raw": str(
                    _value(verification, "raw_output", _value(verification, "raw", ""))
                    if verification is not None
                    else ""
                )[-4000:],
            }
        ]
        changed_files = self._state_files(spec.run_id)
        if checkpoint_store is not None:
            checkpoint_store.make_checkpoint(
                last_event_sequence=self.events.last_sequence,
                agent_owned_changes=changed_files,
                spend=float(_value(legacy, "cost_usd", 0.0) or 0.0),
                turn_id=spec.turn_id,
                identity=identity,
            )
        return RunResult(
            status=status,
            answer="Verified fix completed."
            if status == CompletionStatus.COMPLETED_VERIFIED
            else "",
            changed_files=changed_files,
            verification_evidence=evidence,
            cost=float(_value(legacy, "cost_usd", 0.0) or 0.0),
            attempts=int(_value(legacy, "attempts", 0) or 0),
            resume_availability="available"
            if status != CompletionStatus.COMPLETED_VERIFIED
            else "unavailable",
            follow_up_needs=[]
            if status == CompletionStatus.COMPLETED_VERIFIED
            else [f"legacy status: {legacy_status}"],
            run_id=spec.run_id,
            session_id=spec.session_id,
            trace_path=str(self.events.path)
            if self.events is not None
            else str(_value(legacy, "log_path", "")),
            checkpoint_path=checkpoint_path,
            diff=str(_value(legacy, "diff", "") or ""),
            model_calls=list(_value(legacy, "model_calls", []) or []),
            error=""
            if status == CompletionStatus.COMPLETED_VERIFIED
            else str(legacy_status),
            metadata={"legacy_status": legacy_status},
        )

    def _state_files(self, run_id: str) -> list[str]:
        if self.events is None:
            return []
        path = (
            Path(self.log_root or self.events.path.parent.parent)
            / run_id
            / "state.json"
        )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(data, Mapping):
            return []
        values = data.get("files_touched") or []
        return sorted({str(item).replace("\\", "/") for item in values})


def _value(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)
