"""Local transport backed by one public AgentKernel per active run."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from extensions import HookSecurityError
from harness.agent_kernel import (
    AgentKernel,
    Checkpoint,
    ModelGateway,
    RunEventJournal,
    RunResult,
    RunSpec,
    new_run_id,
    new_session_id,
    safe_segment,
)
from shared.agent_contracts import CompletionStatus
from shared.security import redact_secrets, redact_text

from .bridges import (
    build_strict_components,
    dispatch_completion_after,
    dispatch_completion_before,
    dispatch_task_after,
    dispatch_task_before,
)
from .catalog import catalog_get, catalog_list, catalog_schema, catalog_search
from .errors import (
    ClosedError,
    EventReplayError,
    InvalidRequestError,
    RunNotFoundError,
)
from .events import Events, validated_event_rows, validated_replay
from .models import PROTOCOL_VERSION, Result, RunRequest, Tool
from .transport import RunHandle, Transport
from .workspace import LocalWorkspaceManager, Workspace

__all__ = ["LocalAgent", "LocalClient", "LocalTransport"]


@dataclass
class _LocalRun:
    """Mutable internal state for one active or completed local run."""

    run_id: str
    session_id: str
    spec: RunSpec
    request: RunRequest
    kernel: Optional[AgentKernel]
    handle: RunHandle
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[Result] = None
    thread: Optional[threading.Thread] = None
    cancel_requested: bool = False
    workspace_id: str = ""
    error: str = ""


class LocalTransport(Transport):
    """Run public Boundary-0 kernels locally with explicit per-run ownership."""

    def __init__(
        self,
        repo_path: str | Path,
        *,
        log_root: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
        model: Callable[..., Any] | str | None = None,
        call_fn: Callable[..., Any] | None = None,
        model_callable: Callable[..., Any] | None = None,
        model_fn: Callable[..., Any] | None = None,
        verifier: Callable[..., Any] | None = None,
        workspace_root: str | Path | None = None,
        session_id: str = "",
        on_event: Callable[[dict[str, Any]], None] | None = None,
        hooks: Any = None,
        hook_manager: Any = None,
        tool_catalog: Any = None,
        catalog: Any = None,
    ) -> None:
        """Create a local transport rooted at an explicit absolute log directory."""
        if hooks is not None and hook_manager is not None and hooks is not hook_manager:
            raise InvalidRequestError(
                "hooks and hook_manager must identify the same manager"
            )
        self.repo_path = self._repo_path(repo_path)
        selected_log_root = log_root or (Path(self.repo_path) / "logs")
        self.log_root = Path(selected_log_root).expanduser().absolute().resolve()
        self.config = dict(config or {})
        if (
            tool_catalog is not None
            and catalog is not None
            and tool_catalog is not catalog
        ):
            raise InvalidRequestError(
                "tool_catalog and catalog must identify the same catalog"
            )
        self.hooks = hook_manager if hook_manager is not None else hooks
        self.hook_manager = self.hooks
        self.tool_catalog = tool_catalog if tool_catalog is not None else catalog
        self.catalog = self.tool_catalog
        selected_model = (
            call_fn
            or model_callable
            or model_fn
            or (model if callable(model) else None)
        )
        self.model = selected_model
        if isinstance(model, str):
            self.config.setdefault("model", model)
        self.verifier = verifier
        self.session_id = self._safe_id(session_id or new_session_id(), "session id")
        self.on_event = on_event
        self.workspace_manager = LocalWorkspaceManager(
            workspace_root or (self.log_root / "workspaces")
        )
        self._runs: dict[str, _LocalRun] = {}
        self._conversation: Any = None
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}
        self._closed = False

    @property
    def supports_local_hooks(self) -> bool:
        """Return that this transport executes injected hooks locally."""
        return True

    @property
    def client(self) -> "LocalTransport":
        """Return this transport under a client-compatible alias."""
        return self

    @property
    def conversation(self):
        """Return a session facade for transport-oriented callers."""
        if self._conversation is None:
            from .client import Conversation

            self._conversation = Conversation(self, self.session_id)
        return self._conversation

    def stream(
        self,
        value: Any = "",
        *,
        run_id: str = "",
        after_sequence: int = 0,
        **kwargs: Any,
    ) -> Events:
        """Start or select a local run and return its event stream."""
        if isinstance(value, RunHandle):
            return value.events(after_sequence)
        if run_id:
            return self.events(
                run_id, session_id=self.session_id, after_sequence=after_sequence
            )
        if isinstance(value, str) and value and self.has_run(value):
            return self.events(
                value, session_id=self.session_id, after_sequence=after_sequence
            )
        handle = self.run(value, wait=False, **kwargs)
        if isinstance(handle, RunHandle):
            return handle.events(after_sequence)
        return self.events(
            handle.run_result.run_id,
            session_id=self.session_id,
            after_sequence=after_sequence,
        )

    @property
    def kernels(self) -> dict[str, AgentKernel]:
        """Return the actual per-run kernels retained by this transport."""
        with self._lock:
            return {
                run_id: record.kernel
                for run_id, record in self._runs.items()
                if record.kernel is not None
            }

    @property
    def capabilities(self) -> Dict[str, Any]:
        """Return local transport capabilities for version negotiation."""
        return {
            "local": True,
            "events": True,
            "sse": True,
            "websocket": False,
            "openai": False,
            "workspaces": True,
            "tools": True,
            "tool_catalog": self.tool_catalog is not None,
            "hooks": self.hooks is not None,
        }

    def _repo_path(self, value: str | Path | Workspace) -> str:
        candidate = getattr(value, "path", value)
        path = Path(str(candidate)).expanduser().absolute().resolve()
        if not path.is_dir():
            raise InvalidRequestError(f"repository path is not a directory: {path}")
        return str(path)

    @staticmethod
    def _safe_id(value: str, label: str) -> str:
        text = str(value or "").strip()
        if not text or text != safe_segment(text) or not safe_segment(text):
            raise InvalidRequestError(f"{label} must be one safe path segment")
        return text

    def _check_open(self) -> None:
        if self._closed:
            raise ClosedError("local transport is closed")

    def _confine_repo(self, value: str | Path) -> str:
        """Return a canonical repository path only when it remains under this agent root."""
        raw = Path(str(value)).expanduser().absolute()
        cursor = raw
        while True:
            if cursor.is_symlink():
                raise InvalidRequestError("repository path must not contain symlinks")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        candidate = raw.resolve()
        root = Path(self.repo_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise InvalidRequestError(
                "repository path is outside the configured agent root"
            ) from exc
        return str(candidate)

    def _request(
        self,
        value: Any,
        *,
        session_id: str = "",
        wait: bool | None = None,
        run_id: str = "",
        **overrides: Any,
    ) -> RunRequest:
        request = RunRequest.from_value(value, **overrides)
        if not request.request:
            raise InvalidRequestError("agent request text is required")
        if not request.repo_path:
            request.repo_path = self.repo_path
        else:
            request.repo_path = self._confine_repo(request.repo_path)
        if request.workspace_id:
            workspace = self.workspace_manager.get(request.workspace_id)
            try:
                request.repo_path = self._confine_repo(workspace.path)
            except InvalidRequestError:
                workspace_path = Path(workspace.path).expanduser().absolute().resolve()
                managed_root = self.workspace_manager.root.resolve()
                try:
                    workspace_path.relative_to(managed_root)
                except ValueError as exc:
                    raise InvalidRequestError(
                        "managed workspace is outside the configured workspace root"
                    ) from exc
                if workspace_path.is_symlink():
                    raise InvalidRequestError(
                        "workspace path must not be a symlink"
                    ) from None
                request.repo_path = str(workspace_path)
        if not request.session_id:
            request.session_id = session_id or self.session_id
        self._safe_id(request.session_id, "session id")
        if run_id:
            request.run_id = run_id
        if wait is not None:
            request.wait = bool(wait)
        if request.run_id:
            self._safe_id(request.run_id, "run id")
        return request

    def _make_spec(self, request: RunRequest) -> RunSpec:
        selected_run = self._safe_id(request.run_id or new_run_id(), "run id")
        request.run_id = selected_run
        merged = dict(self.config)
        merged.update(request.config or {})
        metadata = dict(request.metadata or {})
        metadata["config"] = dict(merged)
        return RunSpec(
            session_id=request.session_id,
            run_id=selected_run,
            request=request.request,
            repository_identity=request.repo_path or self.repo_path,
            strategy=request.strategy,
            workspace_policy=dict(request.workspace_policy),
            verification_policy=dict(request.verification_policy),
            resume_token=str(request.metadata.get("resume_token", "") or "") or None,
            metadata=metadata,
            repo_path=request.repo_path or self.repo_path,
        )

    def _kernel(self, spec: RunSpec, record: _LocalRun) -> AgentKernel:
        def on_event(event: dict[str, Any]) -> None:
            if record.cancel_requested and event.get("event") in {
                "model_request",
                "tool_call",
                "tool_result",
            }:
                try:
                    if record.kernel is not None:
                        record.kernel.cancel(spec.run_id)
                except Exception:
                    pass
            callback = self.on_event
            if callback is not None:
                try:
                    callback(dict(redact_secrets(event)))
                except Exception:
                    pass

        gateway = ModelGateway(call_fn=self.model) if self.model is not None else None
        policy = None
        registry = None
        completion = None
        if self.hooks is not None and spec.strategy in {
            "daily",
            "planning",
            "question",
            "research",
        }:
            policy, registry, completion = build_strict_components(
                spec,
                self.hooks,
                dict(spec.metadata.get("config", {})),
                self.verifier,
            )
        return AgentKernel(
            repo_path=spec.repository_identity,
            log_root=self.log_root,
            config=dict(spec.metadata.get("config", {})),
            model_gateway=gateway,
            policy_engine=policy,
            tool_registry=registry,
            completion_policy=completion,
            verifier=self.verifier,
            on_event=on_event,
        )

    def start_run(
        self, value: Any, *, wait: bool | None = None, **kwargs: Any
    ) -> RunHandle:
        """Start one run in a worker and return its public handle."""
        self._check_open()
        request = self._request(value, wait=wait, **kwargs)
        with self._lock:
            if request.run_id and request.run_id in self._runs:
                existing = self._runs[request.run_id]
                if not request.resume:
                    return existing.handle
                if not existing.done.is_set():
                    raise InvalidRequestError(
                        f"run is already active: {request.run_id}"
                    )
            spec = self._make_spec(request)
            if spec.run_id in self._runs and not request.resume:
                raise InvalidRequestError(f"run already exists: {spec.run_id}")
            handle = RunHandle(
                self,
                spec.run_id,
                session_id=spec.session_id,
                trace_path=str(
                    self.log_root / safe_segment(spec.run_id) / "trace.jsonl"
                ),
            )
            record = _LocalRun(
                run_id=spec.run_id,
                session_id=spec.session_id,
                spec=spec,
                request=request,
                kernel=None,
                handle=handle,
                workspace_id=request.workspace_id,
            )
            if request.workspace_id:
                self.workspace_manager.claim(request.workspace_id, spec.run_id)
                record.workspace_id = request.workspace_id
            try:
                record.kernel = self._kernel(spec, record)
            except Exception:
                if record.workspace_id:
                    self.workspace_manager.release(record.workspace_id, spec.run_id)
                raise
            self._runs[spec.run_id] = record
            thread = threading.Thread(
                target=self._execute,
                args=(record,),
                name=f"neo-sdk-{spec.run_id}",
                daemon=True,
            )
            record.thread = thread
            try:
                thread.start()
            except Exception:
                self._runs.pop(spec.run_id, None)
                if record.workspace_id:
                    self.workspace_manager.release(record.workspace_id, spec.run_id)
                raise
        return handle

    def _session_lock(self, session_id: str) -> threading.RLock:
        """Return the serialization lock for one session."""
        with self._lock:
            return self._session_locks.setdefault(str(session_id), threading.RLock())

    def _execute(self, record: _LocalRun) -> None:
        """Serialize all runs sharing a session while allowing other sessions to run."""
        with self._session_lock(record.session_id):
            self._execute_serialized(record)

    def _execute_serialized(self, record: _LocalRun) -> None:
        result: Optional[RunResult] = None
        try:
            if record.cancel_requested:
                try:
                    record.kernel.cancel(record.run_id)
                except Exception:
                    pass
            before = (
                dispatch_task_before(self.hooks, record.spec)
                if self.hooks is not None
                else None
            )
            if before is not None and (
                bool(getattr(before, "denied", False))
                or bool(getattr(before, "short_circuited", False))
                or bool(getattr(before, "security_violation", False))
                or not bool(getattr(before, "should_continue", True))
            ):
                result = self._blocked_hook_result(record, before)
            else:
                result = record.kernel.run(
                    record.spec,
                    strategy=record.spec.strategy,
                    resume=record.request.resume,
                )
        except HookSecurityError as exc:
            result = self._blocked_hook_result(record, exc)
        except Exception as exc:
            result = self._failure_result(record, exc)
        if result is None:
            result = self._failure_result(
                record, RuntimeError("kernel returned no result")
            )
        if self.hooks is not None:
            self._dispatch_observational_hooks(record, result)
        record.result = Result(result)
        record.error = str(result.error or "")
        record.done.set()
        if record.workspace_id:
            try:
                self.workspace_manager.release(record.workspace_id, record.run_id)
            except Exception:
                pass

    def _archive_fresh_sdk_run(self, record: _LocalRun) -> None:
        """Archive an old run directory before a fresh pre-kernel block."""
        run_dir = self.log_root / safe_segment(record.run_id)
        if record.request.resume or not run_dir.exists():
            return
        archive = run_dir.with_name(
            f"{record.run_id}.old-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        suffix = 1
        while archive.exists():
            archive = run_dir.with_name(f"{archive.name}-{suffix}")
            suffix += 1
        run_dir.rename(archive)

    def _blocked_hook_result(self, record: _LocalRun, source: Any) -> RunResult:
        """Return a traced blocked result without invoking the kernel."""
        try:
            self._archive_fresh_sdk_run(record)
        except OSError:
            pass
        reason = (
            str(source)
            if isinstance(source, BaseException)
            else str(getattr(source, "reason", "") or "task hook denied execution")
        )
        reason = redact_text(reason)[:1000] or "task hook denied execution"
        trace_path = self.log_root / safe_segment(record.run_id) / "trace.jsonl"
        self._append_sdk_event(
            record,
            "run_started",
            {
                "task_id": record.run_id,
                "run_id": record.run_id,
                "session_id": record.session_id,
                "mode": record.spec.strategy,
                "strategy": record.spec.strategy,
                "request": record.spec.request,
                "repo_path": record.spec.repository_identity,
                "run_spec": record.spec.to_dict(),
            },
        )
        self._append_sdk_event(
            record,
            "sdk_task_before",
            {"point": "task.before", "reason": reason, "blocked": True},
        )
        result = RunResult(
            status=CompletionStatus.BLOCKED,
            run_id=record.run_id,
            session_id=record.session_id,
            trace_path=str(trace_path),
            error=reason,
            follow_up_needs=[reason],
            verification_evidence=[
                {
                    "kind": "hook_blocked",
                    "passed": False,
                    "description": reason,
                }
            ],
            metadata={"hook_point": "task.before", "hook_reason": reason},
        )
        self._append_sdk_event(
            record,
            "run_finished",
            {
                "status": result.status,
                "result": result.to_dict(),
                "error": result.error,
            },
        )
        return result

    def _append_sdk_event(
        self, record: _LocalRun, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        """Append one redacted SDK lifecycle event and notify the event callback."""
        try:
            journal = RunEventJournal(
                self.log_root / safe_segment(record.run_id) / "trace.jsonl",
                session_id=record.session_id,
                run_id=record.run_id,
            )
            event = journal.append(event_type, dict(payload))
            callback = self.on_event
            if callback is not None:
                callback(dict(redact_secrets(event.to_dict())))
        except Exception:
            return

    def _dispatch_observational_hooks(
        self, record: _LocalRun, result: RunResult
    ) -> None:
        """Dispatch completion and task-after hooks without changing the result."""
        operations = (
            (
                "completion.before",
                lambda: dispatch_completion_before(self.hooks, record.spec, result),
            ),
            (
                "completion.after",
                lambda: dispatch_completion_after(self.hooks, record.spec, result),
            ),
            (
                "task.after",
                lambda: dispatch_task_after(self.hooks, record.spec, result),
            ),
        )
        for point, operation in operations:
            try:
                outcome = operation()
                self._append_sdk_event(
                    record,
                    "sdk_hook_dispatched",
                    {
                        "point": point,
                        "action": str(getattr(outcome, "action", "allow")),
                        "allowed": bool(getattr(outcome, "allowed", True)),
                        "denied": bool(getattr(outcome, "denied", False)),
                        "short_circuited": bool(
                            getattr(outcome, "short_circuited", False)
                        ),
                        "errors": list(getattr(outcome, "errors", ()) or ()),
                    },
                )
            except HookSecurityError as exc:
                self._append_sdk_event(
                    record,
                    "sdk_hook_security_error",
                    {"point": point, "error": redact_text(str(exc))},
                )
            except Exception as exc:
                self._append_sdk_event(
                    record,
                    "sdk_hook_error",
                    {"point": point, "error": redact_text(str(exc))},
                )

    def _failure_result(self, record: _LocalRun, exc: Exception) -> RunResult:
        trace_path = self.log_root / safe_segment(record.run_id) / "trace.jsonl"
        try:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            journal = RunEventJournal(
                trace_path,
                session_id=record.session_id,
                run_id=record.run_id,
            )
            journal.append("sdk_error", {"error": str(exc)})
        except Exception:
            pass
        safe_error = redact_text(str(exc))[:1000]
        result = RunResult(
            status=CompletionStatus.FAILED,
            run_id=record.run_id,
            session_id=record.session_id,
            trace_path=str(trace_path),
            error=safe_error,
            follow_up_needs=[safe_error],
        )
        try:
            RunEventJournal(
                trace_path,
                session_id=record.session_id,
                run_id=record.run_id,
            ).append(
                "run_finished",
                {
                    "status": result.status,
                    "result": result.to_dict(),
                    "error": result.error,
                },
            )
        except Exception:
            pass
        return result

    def run(
        self, value: Any, *, wait: bool | None = None, **kwargs: Any
    ) -> Result | RunHandle:
        """Start a local run and return a Result or RunHandle according to wait."""
        request = self._request(value, wait=wait, **kwargs)
        handle = self.start_run(request, wait=False)
        return handle.wait() if request.wait else handle

    def query(self, value: Any, **kwargs: Any) -> Result:
        """Run one synchronous question-style request."""
        request = self._request(value, wait=True, **kwargs)
        request.strategy = kwargs.get("strategy", "question") or "question"
        request.wait = True
        handle = self.start_run(request, wait=False)
        return handle.wait()

    def wait(self, run_id: str, *, timeout: float | None = None) -> Result:
        """Wait for one local run's worker and return its result."""
        with self._lock:
            record = self._runs.get(str(run_id))
        if record is None:
            record = self._discover_record(str(run_id))
        if record is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        if not record.done.wait(timeout=timeout):
            raise TimeoutError(f"timed out waiting for run: {run_id}")
        if record.result is None:
            raise RunNotFoundError(f"run has no result: {run_id}")
        return record.result

    def get_result(self, run_id: str) -> Result | None:
        """Return a completed local result without blocking."""
        with self._lock:
            record = self._runs.get(str(run_id))
        if record is None:
            record = self._discover_record(str(run_id))
        return record.result if record is not None and record.done.is_set() else None

    def status(self, run_id: str) -> dict[str, Any]:
        """Return a public status projection for a local run."""
        with self._lock:
            record = self._runs.get(str(run_id))
        if record is None:
            record = self._discover_record(str(run_id))
        if record is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        result = record.result
        return dict(
            redact_secrets(
                {
                    "run_id": record.run_id,
                    "session_id": record.session_id,
                    "status": str(result.status) if result is not None else "running",
                    "done": record.done.is_set(),
                    "terminal": record.done.is_set(),
                    "trace_path": str(
                        self.log_root / safe_segment(record.run_id) / "trace.jsonl"
                    ),
                    "error": str(result.error if result is not None else record.error),
                    "protocol_version": PROTOCOL_VERSION,
                    "schema_version": result.run_result.schema_version
                    if result is not None
                    else 1,
                }
            )
        )

    def is_terminal(self, run_id: str) -> bool:
        """Return whether a local run has completed."""
        try:
            return bool(self.status(run_id).get("terminal"))
        except RunNotFoundError:
            return False

    def has_run(self, run_id: str) -> bool:
        """Return whether a run is active or persisted locally."""
        with self._lock:
            if str(run_id) in self._runs:
                return True
        try:
            return self._discover_record(str(run_id)) is not None
        except (RunNotFoundError, EventReplayError):
            return False

    def cancel(self, run_id: str) -> bool:
        """Cancel an active run by calling its actual kernel directly."""
        with self._lock:
            record = self._runs.get(str(run_id))
        if record is None:
            record = self._discover_record(str(run_id))
        if record is None or record.done.is_set():
            return False
        record.cancel_requested = True
        try:
            return bool(record.kernel.cancel(str(run_id)))
        except Exception:
            return False

    def resume(
        self,
        run_id: str,
        request: str = "",
        *,
        wait: bool = True,
        **kwargs: Any,
    ) -> Result | RunHandle:
        """Resume a persisted run using its public checkpoint and trace."""
        self._check_open()
        selected = self._safe_id(run_id, "run id")
        with self._lock:
            previous = self._runs.get(selected)
        if previous is None:
            previous = self._discover_record(selected)
        if previous is None:
            raise RunNotFoundError(f"run not found: {selected}")
        projection = validated_replay(self.trace_path(selected), run_id=selected)
        first_payload = (
            dict(projection.events[0].get("payload", {}))
            if projection.events and isinstance(projection.events[0], Mapping)
            else {}
        )
        raw_spec = first_payload.get("run_spec")
        if not isinstance(raw_spec, Mapping):
            raise InvalidRequestError("run journal has no resumable run specification")
        prior_spec = RunSpec.from_dict(raw_spec)
        if (
            prior_spec.run_id != selected
            or prior_spec.session_id != previous.session_id
        ):
            raise InvalidRequestError(
                "run journal identity does not match the requested run"
            )
        if previous.spec.run_id != selected:
            raise InvalidRequestError("run identity cannot be changed while resuming")
        if prior_spec.strategy and previous.spec.strategy != prior_spec.strategy:
            raise InvalidRequestError("run strategy metadata is inconsistent")
        configured_repo = str(Path(self.repo_path).resolve())
        if prior_spec.repository_identity:
            prior_repo = str(Path(prior_spec.repository_identity).resolve())
            if prior_repo != configured_repo and not previous.request.workspace_id:
                raise InvalidRequestError("run belongs to a different repository")
        checkpoint_data = self._read_checkpoint(selected)
        if checkpoint_data:
            checkpoint = Checkpoint.from_dict(checkpoint_data)
            if checkpoint.run_id and checkpoint.run_id != selected:
                raise InvalidRequestError("checkpoint run identity mismatch")
            if checkpoint.session_id and checkpoint.session_id != previous.session_id:
                raise InvalidRequestError("checkpoint session identity mismatch")
        requested_strategy = kwargs.pop("strategy", "")
        if (
            requested_strategy
            and str(requested_strategy).lower() != previous.spec.strategy
        ):
            raise InvalidRequestError("resume strategy must match the original run")
        requested_session = kwargs.pop("session_id", "")
        if requested_session and str(requested_session) != previous.session_id:
            raise InvalidRequestError("resume session must match the original run")
        requested_workspace = kwargs.pop("workspace_id", "")
        if (
            requested_workspace
            and str(requested_workspace) != previous.request.workspace_id
        ):
            raise InvalidRequestError("resume workspace must match the original run")
        metadata = dict(previous.request.metadata or {})
        metadata.update(dict(kwargs.pop("metadata", {}) or {}))
        for key, value in dict(prior_spec.metadata or {}).items():
            if key != "config":
                metadata.setdefault(key, value)
        if checkpoint_data:
            metadata.setdefault(
                "resume_token", str(checkpoint_data.get("resume_token", ""))
            )
        prior_workspace = str(
            previous.request.workspace_id
            or prior_spec.metadata.get("workspace_id", "")
            or prior_spec.workspace_policy.get("workspace_id", "")
        )
        base = RunRequest(
            request=str(request or "continue the active task"),
            repo_path=prior_spec.repo_path or previous.spec.repo_path or self.repo_path,
            session_id=previous.session_id,
            run_id=selected,
            strategy=prior_spec.strategy or previous.spec.strategy,
            config=dict(
                previous.request.config or prior_spec.metadata.get("config", {}) or {}
            ),
            verification_policy=dict(
                previous.spec.verification_policy
                or prior_spec.verification_policy
                or {}
            ),
            workspace_policy=dict(
                previous.spec.workspace_policy or prior_spec.workspace_policy or {}
            ),
            workspace_id=str(prior_workspace or requested_workspace or ""),
            metadata=metadata,
            wait=wait,
            resume=True,
        )
        return self.run(base, wait=wait)

    def events(
        self, run_id: str, *, session_id: str = "", after_sequence: int = 0
    ) -> Events:
        """Return a local validated event stream."""
        return Events(
            self,
            str(run_id),
            session_id=session_id,
            after_sequence=after_sequence,
        )

    def read_events(
        self,
        run_id: str,
        *,
        session_id: str = "",
        after_sequence: int = 0,
    ) -> list[Any]:
        """Read validated public event rows after an exclusive sequence."""
        rows, _ = validated_event_rows(self, str(run_id), session_id=session_id)
        return [row for row in rows if row.sequence > max(0, int(after_sequence))]

    def trace_path(self, run_id: str) -> Path:
        """Return the absolute trace path for a safe run identifier."""
        selected = self._safe_id(str(run_id), "run id")
        return self.log_root / safe_segment(selected) / "trace.jsonl"

    def replay(self, run_id: str) -> Any:
        """Return a deterministic redacted replay projection."""
        return validated_replay(self.trace_path(run_id), run_id=str(run_id))

    def list_runs(self) -> list[dict[str, Any]]:
        """List active and persisted runs with public status metadata."""
        found: dict[str, dict[str, Any]] = {}
        with self._lock:
            for run_id, _record in self._runs.items():
                found[run_id] = self.status(run_id)
        try:
            directories = list(self.log_root.iterdir())
        except OSError:
            directories = []
        for directory in directories:
            if not directory.is_dir() or directory.is_symlink():
                continue
            run_id = directory.name
            if ".old-" in run_id:
                continue
            if not safe_segment(run_id) or safe_segment(run_id) != run_id:
                continue
            trace = directory / "trace.jsonl"
            if not trace.is_file():
                continue
            try:
                projection = validated_replay(trace, run_id=run_id)
                found.setdefault(
                    run_id,
                    {
                        "run_id": run_id,
                        "session_id": projection.session_id,
                        "status": projection.final_status or "running",
                        "done": bool(projection.final_status),
                        "terminal": bool(projection.final_status),
                        "trace_path": str(trace),
                        "error": "",
                        "protocol_version": PROTOCOL_VERSION,
                        "schema_version": 1,
                    },
                )
            except EventReplayError as exc:
                found.setdefault(
                    run_id,
                    {
                        "run_id": run_id,
                        "session_id": "",
                        "status": "failed",
                        "done": True,
                        "terminal": True,
                        "trace_path": str(trace),
                        "error": str(exc),
                        "protocol_version": PROTOCOL_VERSION,
                        "schema_version": 1,
                    },
                )
        return [
            dict(redact_secrets(item))
            for item in sorted(
                found.values(), key=lambda item: str(item.get("run_id", ""))
            )
        ]

    def history(
        self, session_id: str = "", *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Read bounded public session turns from the durable session state."""
        selected = self._safe_id(session_id or self.session_id, "session id")
        path = self.log_root / safe_segment(selected) / "session.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError) as exc:
            raise EventReplayError(f"session history is unreadable: {exc}") from exc
        turns = raw.get("turns", []) if isinstance(raw, Mapping) else []
        values = [dict(item) for item in turns if isinstance(item, Mapping)]
        if limit is not None:
            values = values[-max(0, int(limit)) :]
        return [dict(redact_secrets(item)) for item in values]

    def close(self) -> None:
        """Cancel active kernels directly and release local resources."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = list(self._runs.values())
        for record in records:
            if not record.done.is_set():
                record.cancel_requested = True
                try:
                    record.kernel.cancel(record.run_id)
                except Exception:
                    pass
        for record in records:
            record.done.wait(timeout=2.0)

    def close_conversation(self, session_id: str) -> None:
        """Close a local conversation pointer without stopping unrelated runs."""
        selected = self._safe_id(session_id or self.session_id, "session id")
        path = self.log_root / safe_segment(selected) / "session.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return
        if isinstance(raw, dict):
            raw["active_run_id"] = ""
            raw["updated_at"] = time.time()
            temporary = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
            temporary.write_text(
                json.dumps(redact_secrets(raw), default=str), encoding="utf-8"
            )
            os.replace(temporary, path)

    def create_workspace(
        self,
        name: str = "",
        *,
        workspace_id: str = "",
        source_path: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a managed workspace from the configured repository only."""
        selected_source = str(source_path or "")
        if selected_source:
            source = self._confine_repo(selected_source)
            if source != str(Path(self.repo_path).resolve()):
                raise InvalidRequestError(
                    "workspace source must be the configured repository root"
                )
        return self.workspace_manager.create(
            name,
            workspace_id=workspace_id,
            source_path=selected_source or None,
            metadata=metadata,
        ).to_dict()

    def list_workspaces(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        """List local managed workspace records for the server protocol."""
        return [
            item.to_dict()
            for item in self.workspace_manager.list(include_deleted=include_deleted)
        ]

    def get_workspace(
        self, workspace_id: str, *, include_deleted: bool = False
    ) -> dict[str, Any]:
        """Return one local managed workspace record."""
        return self.workspace_manager.get(
            workspace_id, include_deleted=include_deleted
        ).to_dict()

    def delete_workspace(self, workspace_id: str) -> dict[str, Any]:
        """Delete one inactive local managed workspace record."""
        return self.workspace_manager.delete(workspace_id).to_dict()

    def list_tools(self) -> list[Tool]:
        """List injected catalog descriptors without resolving deferred schemas."""
        return catalog_list(self.tool_catalog)

    def search_tools(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[Tool]:
        """Search injected catalog metadata without resolving deferred schemas."""
        return catalog_search(
            self.tool_catalog, query, max_results if limit is None else limit
        )

    def get_tool(self, name: str) -> Tool:
        """Return one exact injected catalog descriptor without schema resolution."""
        return catalog_get(self.tool_catalog, name)

    def search(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[Tool]:
        """Search injected catalog metadata under the short alias."""
        return self.search_tools(query, max_results, limit=limit)

    def get(self, name: str) -> Tool:
        """Return one exact injected catalog descriptor under the short alias."""
        return self.get_tool(name)

    def resolve_tool_schema(self, name: str) -> Any:
        """Resolve one exact catalog schema on explicit request."""
        return catalog_schema(self.tool_catalog, name)

    def resolve_schema(self, name: str) -> Any:
        """Return one exact catalog schema under the short compatibility name."""
        return self.resolve_tool_schema(name)

    def resolve_tool(self, name: str) -> Any:
        """Return one exact catalog schema under the resolve-tool alias."""
        return self.resolve_tool_schema(name)

    def resolve(self, name: str) -> Any:
        """Return one exact catalog schema under the short resolve alias."""
        return self.resolve_tool_schema(name)

    def version(self) -> dict[str, Any]:
        """Return local protocol version metadata."""
        return {
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": 1,
            "supported_protocol_versions": [PROTOCOL_VERSION],
            "supported_schema_versions": [1],
            "capabilities": self.capabilities,
        }

    def _read_checkpoint(self, run_id: str) -> dict[str, Any]:
        path = self.log_root / safe_segment(run_id) / "checkpoint.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _discover_record(self, run_id: str) -> Optional[_LocalRun]:
        trace = self.log_root / safe_segment(run_id) / "trace.jsonl"
        if not trace.is_file():
            return None
        projection = validated_replay(trace, run_id=run_id)
        with self._lock:
            if run_id in self._runs:
                return self._runs[run_id]
        result_data = dict(projection.result)
        if not result_data:
            return None
        try:
            result = Result(result_data)
        except Exception:
            return None
        request = RunRequest(
            request=projection.request,
            repo_path=self.repo_path,
            session_id=projection.session_id,
            run_id=run_id,
            strategy=projection.strategy or "daily",
        )
        spec = RunSpec(
            session_id=projection.session_id,
            run_id=run_id,
            request=projection.request,
            repository_identity=self.repo_path,
            strategy=projection.strategy or "daily",
        )
        kernel = AgentKernel(repo_path=self.repo_path, log_root=self.log_root)
        handle = RunHandle(
            self, run_id, session_id=projection.session_id, trace_path=str(trace)
        )
        record = _LocalRun(
            run_id=run_id,
            session_id=projection.session_id,
            spec=spec,
            request=request,
            kernel=kernel,
            handle=handle,
            result=result,
        )
        record.done.set()
        with self._lock:
            self._runs[run_id] = record
        return record


LocalClient = LocalTransport
LocalAgent = LocalTransport
