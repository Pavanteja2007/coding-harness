"""Public-only lifecycle hook bridges and strict kernel component construction."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from extensions import HookContext, HookPoint, HookSecurityError
from harness.agent_kernel import (
    CompletionPolicy,
    PolicyEngine,
    RunResult,
    RunSpec,
    ToolCall,
    ToolRegistry,
    build_default_handlers,
)
from shared.agent_contracts import PermissionDecision
from shared.security import redact_text

from .errors import InvalidRequestError

__all__ = [
    "HookAwarePolicyEngine",
    "HookAwareToolRegistry",
    "build_strict_components",
    "dispatch_completion_after",
    "dispatch_completion_before",
    "dispatch_completion_hooks",
    "dispatch_task_after",
    "dispatch_task_before",
]

_STRICT_TOOLS: dict[str, set[str]] = {
    "daily": {
        "read",
        "glob",
        "grep",
        "apply_patch",
        "edit",
        "write",
        "git_status",
        "git_diff",
        "shell",
        "process",
        "test",
        "verify",
        "memory",
        "fetch",
        "mcp",
        "todo",
        "plan",
        "ask",
        "finish",
        "cancel",
    },
    "planning": {
        "read",
        "glob",
        "grep",
        "git_status",
        "git_diff",
        "memory",
        "todo",
        "plan",
        "ask",
        "finish",
        "cancel",
    },
    "question": {
        "read",
        "glob",
        "grep",
        "git_status",
        "git_diff",
        "memory",
        "ask",
        "finish",
        "cancel",
    },
}
_STRICT_TOOLS["research"] = _STRICT_TOOLS["question"] | {"fetch"}
_HOOK_METHODS: dict[HookPoint, str] = {
    HookPoint.TASK_BEFORE: "dispatch_task_before",
    HookPoint.TASK_AFTER: "dispatch_task_after",
    HookPoint.TOOL_BEFORE: "dispatch_tool_before",
    HookPoint.TOOL_AFTER: "dispatch_tool_after",
    HookPoint.PERMISSION_BEFORE: "dispatch_permission_before",
    HookPoint.PERMISSION_AFTER: "dispatch_permission_after",
    HookPoint.COMPLETION_BEFORE: "before_completion",
    HookPoint.COMPLETION_AFTER: "after_completion",
}


@dataclass
class _HookToolResult:
    """Small public-compatible result used when a hook prevents dispatch."""

    ok: bool
    output: Any
    reference: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible tool result projection."""
        return {"ok": self.ok, "output": self.output, "reference": self.reference}


def _safe_text(value: Any, limit: int = 512) -> str:
    """Return a bounded redacted hook reason."""
    return redact_text(str(value or ""))[:limit]


def _dispatch(hooks: Any, point: HookPoint, context: HookContext) -> Any:
    """Dispatch one hook point through a manager or its public dispatch API."""
    if hooks is None:
        return None
    method = getattr(hooks, _HOOK_METHODS[point], None)
    if callable(method):
        return method(context)
    dispatcher = getattr(hooks, "dispatch", None)
    if not callable(dispatcher):
        raise InvalidRequestError("hooks must provide a public hook dispatcher")
    return dispatcher(point, context)


def _task_context(
    spec: RunSpec,
    point: HookPoint,
    *,
    result: Optional[RunResult] = None,
) -> HookContext:
    """Build a redacted task or completion context for one SDK run."""
    data = {
        "run_id": spec.run_id,
        "session_id": spec.session_id,
        "request": spec.request,
        "strategy": spec.strategy,
        "repo_path": spec.repository_identity,
    }
    return HookContext(
        point=point,
        task_id=spec.run_id,
        event=point.value,
        data=data,
        context={
            "source": "agent_sdk",
            "run_id": spec.run_id,
            "session_id": spec.session_id,
            "strategy": spec.strategy,
        },
        metadata={"source": "agent_sdk"},
        task=spec,
        result=result,
    )


def _tool_context(
    spec: RunSpec,
    point: HookPoint,
    call: ToolCall,
    *,
    result: Any = None,
    permission: Any = None,
) -> HookContext:
    """Build a redacted tool or permission context for one strict call."""
    call_data = call.to_dict()
    result_value = (
        result.as_dict()
        if hasattr(result, "as_dict") and callable(result.as_dict)
        else result
    )
    return HookContext(
        point=point,
        task_id=spec.run_id,
        tool=call.tool,
        event=point.value,
        data={"call": call_data},
        context={"source": "agent_sdk", "run_id": spec.run_id},
        metadata={"source": "agent_sdk"},
        arguments=call.arguments,
        task=spec,
        result=result_value,
        permission=permission,
    )


def dispatch_task_before(hooks: Any, spec: RunSpec) -> Any:
    """Dispatch the task-before point for one SDK kernel run."""
    return _dispatch(
        hooks, HookPoint.TASK_BEFORE, _task_context(spec, HookPoint.TASK_BEFORE)
    )


def dispatch_task_after(hooks: Any, spec: RunSpec, result: RunResult) -> Any:
    """Dispatch the observational task-after point for one completed run."""
    return _dispatch(
        hooks,
        HookPoint.TASK_AFTER,
        _task_context(spec, HookPoint.TASK_AFTER, result=result),
    )


def dispatch_completion_before(hooks: Any, spec: RunSpec, result: RunResult) -> Any:
    """Dispatch the observational completion-before point for one result."""
    return _dispatch(
        hooks,
        HookPoint.COMPLETION_BEFORE,
        _task_context(spec, HookPoint.COMPLETION_BEFORE, result=result),
    )


def dispatch_completion_after(hooks: Any, spec: RunSpec, result: RunResult) -> Any:
    """Dispatch the observational completion-after point for one result."""
    return _dispatch(
        hooks,
        HookPoint.COMPLETION_AFTER,
        _task_context(spec, HookPoint.COMPLETION_AFTER, result=result),
    )


def dispatch_completion_hooks(
    hooks: Any, spec: RunSpec, result: RunResult
) -> tuple[Any, Any]:
    """Dispatch observational completion points around one canonical result."""
    return (
        dispatch_completion_before(hooks, spec, result),
        dispatch_completion_after(hooks, spec, result),
    )


class HookAwarePolicyEngine:
    """Restrictive hook-aware facade over a public PolicyEngine."""

    def __init__(self, engine: PolicyEngine, hooks: Any, spec: RunSpec) -> None:
        """Wrap a canonical policy engine for one task and hook manager."""
        self._engine = engine
        self._hooks = hooks
        self._spec = spec
        self._audit: list[PermissionDecision] = []
        self._audit_lock = threading.RLock()

    @property
    def audit_log(self) -> list[PermissionDecision]:
        """Return the effective decisions including restrictive hook decisions."""
        with self._audit_lock:
            return list(self._audit)

    @property
    def decisions(self) -> list[PermissionDecision]:
        """Return the effective decision list for policy-compatible callers."""
        return self.audit_log

    def _before(self, call: ToolCall, decision: PermissionDecision) -> Any:
        return _dispatch(
            self._hooks,
            HookPoint.PERMISSION_BEFORE,
            _tool_context(
                self._spec,
                HookPoint.PERMISSION_BEFORE,
                call,
                permission=decision,
            ),
        )

    def _after(self, call: ToolCall, decision: PermissionDecision) -> Any:
        return _dispatch(
            self._hooks,
            HookPoint.PERMISSION_AFTER,
            _tool_context(
                self._spec,
                HookPoint.PERMISSION_AFTER,
                call,
                permission=decision,
            ),
        )

    def _combine(
        self, canonical: PermissionDecision, outcome: Any
    ) -> PermissionDecision:
        """Apply only deny/ask hook precedence to a canonical decision."""
        action = str(getattr(outcome, "action", "allow") or "allow").casefold()
        if (
            bool(getattr(outcome, "denied", False))
            or bool(getattr(outcome, "security_violation", False))
            or action in {"deny", "short_circuit"}
            or (not bool(getattr(outcome, "allowed", True)) and action != "ask")
        ):
            action = "deny"
        if action not in {"allow", "ask", "deny"}:
            action = "allow"
        restrictive = action == "deny" or (
            action == "ask" and canonical.action == "allow"
        )
        if not restrictive or canonical.action == "deny":
            return canonical
        reason = (
            _safe_text(getattr(outcome, "reason", "")) or "hook restricted permission"
        )
        return PermissionDecision(
            matched_rule=f"{canonical.matched_rule}+hook",
            action=action,
            scope=canonical.scope,
            actor=canonical.actor,
            call_id=canonical.call_id,
            exact_effect=canonical.exact_effect,
            reason=reason,
            schema_version=canonical.schema_version,
        )

    def evaluate(
        self,
        call: ToolCall | Mapping[str, Any],
        context: Optional[Mapping[str, Any]] = None,
    ) -> PermissionDecision:
        """Evaluate canonical policy, apply restrictive hooks, and observe the result."""
        normalized = call if isinstance(call, ToolCall) else ToolCall.from_dict(call)
        canonical = self._engine.evaluate(normalized, context)
        before = self._before(normalized, canonical)
        effective = self._combine(canonical, before)
        with self._audit_lock:
            self._audit.append(effective)
        self._after(normalized, effective)
        return effective

    decide = evaluate

    def record_approval(
        self,
        call: ToolCall | Mapping[str, Any],
        decision: PermissionDecision,
        approved: bool,
        scope: Optional[str] = None,
    ) -> PermissionDecision:
        """Record approval on the canonical engine and observe the final decision."""
        normalized = call if isinstance(call, ToolCall) else ToolCall.from_dict(call)
        updated = self._engine.record_approval(normalized, decision, approved, scope)
        with self._audit_lock:
            if self._audit:
                self._audit[-1] = updated
        self._after(normalized, updated)
        return updated

    def grant(
        self,
        call: ToolCall | Mapping[str, Any],
        decision: PermissionDecision,
        scope: str = "once",
    ) -> Any:
        """Install a canonical approval grant and observe its effective decision."""
        normalized = call if isinstance(call, ToolCall) else ToolCall.from_dict(call)
        grant = self._engine.grant(normalized, decision, scope)
        with self._audit_lock:
            if self._audit:
                self._audit[-1] = PermissionDecision(
                    matched_rule=self._audit[-1].matched_rule,
                    action="allow",
                    scope=self._audit[-1].scope,
                    actor=self._audit[-1].actor,
                    call_id=self._audit[-1].call_id,
                    exact_effect=self._audit[-1].exact_effect,
                    reason="approval accepted",
                    schema_version=self._audit[-1].schema_version,
                )
            effective = self._audit[-1] if self._audit else decision
        self._after(normalized, effective)
        return grant

    def __getattr__(self, name: str) -> Any:
        """Expose unchanged public PolicyEngine attributes and methods."""
        return getattr(self._engine, name)


class HookAwareToolRegistry:
    """Hook-aware facade over a public ToolRegistry."""

    def __init__(self, registry: ToolRegistry, hooks: Any, spec: RunSpec) -> None:
        """Wrap a restricted registry and its installed public handlers."""
        self._registry = registry
        self._hooks = hooks
        self._spec = spec

    def _denied(self, outcome: Any, security: bool = False) -> _HookToolResult:
        prefix = "hook security denial" if security else "hook denied execution"
        reason = _safe_text(getattr(outcome, "reason", "")) or prefix
        return _HookToolResult(False, f"TOOL ERROR: {prefix}: {reason}")

    def _after(self, call: ToolCall, result: Any) -> Any:
        try:
            _dispatch(
                self._hooks,
                HookPoint.TOOL_AFTER,
                _tool_context(self._spec, HookPoint.TOOL_AFTER, call, result=result),
            )
        except HookSecurityError as exc:
            return self._denied(type("Outcome", (), {"reason": str(exc)})(), True)
        return result

    def execute(
        self,
        call: ToolCall | Mapping[str, Any],
        context: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Dispatch before/after hooks around one canonical tool execution."""
        normalized = self.validate(call)
        try:
            outcome = _dispatch(
                self._hooks,
                HookPoint.TOOL_BEFORE,
                _tool_context(self._spec, HookPoint.TOOL_BEFORE, normalized),
            )
        except HookSecurityError as exc:
            denied = self._denied(type("Outcome", (), {"reason": str(exc)})(), True)
            return self._after(normalized, denied)
        if outcome is not None and (
            bool(getattr(outcome, "denied", False))
            or bool(getattr(outcome, "short_circuited", False))
            or bool(getattr(outcome, "security_violation", False))
            or not bool(getattr(outcome, "allowed", True))
        ):
            return self._after(normalized, self._denied(outcome))
        result = self._registry.execute(normalized, context)
        return self._after(normalized, result)

    def __getattr__(self, name: str) -> Any:
        """Expose unchanged public ToolRegistry attributes and methods."""
        return getattr(self._registry, name)


def build_strict_components(
    spec: RunSpec,
    hooks: Any,
    config: Mapping[str, Any] | None,
    verifier: Any,
) -> tuple[HookAwarePolicyEngine, HookAwareToolRegistry, CompletionPolicy]:
    """Build public strict-strategy components with restrictive hook bridges."""
    cfg = dict(config or {})
    protected = cfg.get("protected_paths", ())
    if isinstance(protected, str):
        protected = (protected,)
    rules = cfg.get("permission_rules", cfg.get("agent_permission_rules", []))
    default_action = str(cfg.get("permission_default", "") or "").casefold()
    if not default_action:
        default_action = (
            "allow"
            if str(cfg.get("agent_approval", "auto")).casefold() == "auto"
            else "ask"
        )
    policy = PolicyEngine(
        rules,
        session_id=spec.session_id,
        default_action=default_action,
        protected_paths=protected,
    )
    completion = CompletionPolicy(verifier, config=cfg)
    registry = ToolRegistry()
    registry.restrict(_STRICT_TOOLS.get(spec.strategy, _STRICT_TOOLS["daily"]))
    build_default_handlers(
        registry,
        repo_path=spec.repository_identity,
        config=cfg,
        completion=completion,
    )
    return (
        HookAwarePolicyEngine(policy, hooks, spec),
        HookAwareToolRegistry(registry, hooks, spec),
        completion,
    )
