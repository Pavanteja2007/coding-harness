"""Deterministic, bounded, and failure-isolated lifecycle hooks for Neo.

The module owns the hook contract used by plugin implementations.  A hook
callback receives a redacted :class:`HookContext` and returns a
:class:`HookDecision` (or a small mapping/boolean shorthand).  Before
points may deny, stop the chain, or return a bounded mutation.  Observational
points still run every callback but cannot change a canonical result.

The dispatcher is intentionally synchronous and thread safe.  Registrations
are snapshotted for each dispatch, sorted by descending priority and then
ascending registration order, and every registration receives an audit row
even when it is skipped or raises an ordinary exception.
"""

from __future__ import annotations

import contextvars
import copy
import dataclasses
import inspect
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Union

from shared import security
from shared.agent_contracts import PermissionDecision, RunResult

_MAX_HOOK_DEPTH = 8
_MAX_HOOK_ITEMS = 128
_MAX_HOOK_STRING = 16_384
_MAX_HOOK_ERROR = 4_096
_MAX_HOOK_RECORDS = 100_000


class HookError(Exception):
    """Base class for typed hook registration and dispatch failures."""

    def __str__(self) -> str:
        """Return a redacted public error string."""
        return security.redact_text(super().__str__())[:_MAX_HOOK_ERROR]

    def __repr__(self) -> str:
        """Return a redacted public error repr."""
        return f"{type(self).__name__}({str(self)!r})"


class HookValidationError(HookError, ValueError):
    """Raised when a hook point, callback, or decision is invalid."""


class HookRegistrationError(HookValidationError):
    """Raised when a hook cannot be registered or removed."""


class HookPayloadError(HookValidationError):
    """Raised when a hook returns a value outside the bounded contract."""


class HookDispatchError(HookError):
    """Base class for failures that a dispatcher records and isolates."""


class HookSecurityError(HookError, security.SecurityViolation):
    """Raised when a hook explicitly trips the shared security boundary."""


class HookDeniedError(HookSecurityError):
    """Raised when a caller requests that an explicit hook denial raise."""


class HookPoint(str, Enum):
    """Stable lifecycle points supported by :class:`HookManager`."""

    TASK_BEFORE = "task.before"
    TASK_AFTER = "task.after"
    TOOL_BEFORE = "tool.before"
    TOOL_AFTER = "tool.after"
    PERMISSION_BEFORE = "permission.before"
    PERMISSION_AFTER = "permission.after"
    COMPLETION_BEFORE = "completion.before"
    COMPLETION_AFTER = "completion.after"
    COMPLETION = "completion.after"
    BEFORE_TASK = "task.before"
    AFTER_TASK = "task.after"
    BEFORE_TOOL = "tool.before"
    AFTER_TOOL = "tool.after"
    BEFORE_PERMISSION = "permission.before"
    AFTER_PERMISSION = "permission.after"
    TASK = "task.before"
    TOOL = "tool.before"
    PERMISSION = "permission.before"

    @classmethod
    def _missing_(cls, value: object) -> "HookPoint":
        """Normalize common string spellings when constructed directly."""
        raw = str(value or "").strip().casefold()
        normalized = raw.replace("-", "_").replace("/", "_").replace(".", "_")
        normalized = normalized.replace(":", "_").replace(" ", "_")
        aliases = {
            "task_before": "task.before",
            "before_task": "task.before",
            "task_start": "task.before",
            "task_after": "task.after",
            "after_task": "task.after",
            "tool_before": "tool.before",
            "before_tool": "tool.before",
            "tool_after": "tool.after",
            "after_tool": "tool.after",
            "permission_before": "permission.before",
            "before_permission": "permission.before",
            "permission_after": "permission.after",
            "after_permission": "permission.after",
            "permission": "permission.before",
            "task": "task.before",
            "tool": "tool.before",
            "completion": "completion.after",
            "completion_before": "completion.before",
            "before_completion": "completion.before",
            "completion_after": "completion.after",
            "after_completion": "completion.after",
        }
        if normalized in aliases:
            return cls(aliases[normalized])
        if raw in {item.value for item in cls}:
            return cls(raw)
        raise ValueError(f"unsupported hook point: {value!r}")

    def __str__(self) -> str:
        """Return the stable serialized point value."""
        return self.value

    @property
    def domain(self) -> str:
        """Return the lifecycle domain represented by this point."""
        return self.value.split(".", 1)[0]

    @property
    def phase(self) -> str:
        """Return the before/after phase for this point."""
        return self.value.rsplit(".", 1)[-1]


PointLike = Union[HookPoint, str]
DecisionValue = Union["HookDecision", Mapping[str, Any], bool, str, None]
Callback = Callable[["HookContext"], DecisionValue]


def _safe_text(value: Any, limit: int = _MAX_HOOK_STRING) -> str:
    """Return a bounded shared-security-redacted string."""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        text = ""
    return security.redact_text(text)[:limit]


def _safe_error(error: BaseException) -> str:
    """Return a redacted, bounded representation of an exception."""
    try:
        message = str(error)
    except Exception:
        message = ""
    if not message:
        message = type(error).__name__
    return security.redact_text(message)[:_MAX_HOOK_ERROR]


def _safe_repr(value: Any, limit: int = _MAX_HOOK_STRING) -> str:
    """Return a redacted repr without invoking an unbounded formatter."""
    try:
        text = repr(value)
    except Exception:
        try:
            text = f"<{type(value).__name__}>"
        except Exception:
            text = "<unrepresentable>"
    return security.redact_text(text)[:limit]


def _bound_value(
    value: Any,
    *,
    depth: int = 0,
    budget: Optional[list[int]] = None,
    seen: Optional[set[int]] = None,
) -> Any:
    """Return a recursively redacted and size-bounded public value."""
    if budget is None:
        budget = [_MAX_HOOK_ITEMS * _MAX_HOOK_DEPTH]
    if seen is None:
        seen = set()
    if budget[0] <= 0:
        return "[TRUNCATED]"
    budget[0] -= 1
    if depth >= _MAX_HOOK_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return security.redact_text(value)[:_MAX_HOOK_STRING]
    if isinstance(value, bytes):
        return "[BINARY]"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _bound_value(
                dataclasses.asdict(value), depth=depth, budget=budget, seen=seen
            )
        except Exception:
            return _safe_repr(value)
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return "[CIRCULAR]"
        seen.add(marker)
        result: dict[str, Any] = {}
        try:
            items = list(value.items())[:_MAX_HOOK_ITEMS]
        except Exception:
            items = []
        for key, item in items:
            if budget[0] <= 0:
                result["[TRUNCATED]"] = True
                break
            try:
                key_text = security.redact_text(str(key))[:_MAX_HOOK_STRING]
            except Exception:
                key_text = "<key>"
            if security.is_sensitive_key(key):
                item = security.REDACTED_SECRET
            result[key_text] = _bound_value(
                item, depth=depth + 1, budget=budget, seen=seen
            )
        seen.remove(marker)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in seen:
            return "[CIRCULAR]"
        seen.add(marker)
        try:
            values = list(value)[:_MAX_HOOK_ITEMS]
        except Exception:
            values = []
        result_list = [
            _bound_value(item, depth=depth + 1, budget=budget, seen=seen)
            for item in values
            if budget[0] > 0
        ]
        seen.remove(marker)
        return result_list
    try:
        redacted = security.redact_secrets(value)
    except Exception:
        redacted = _safe_repr(value)
    if redacted is value or isinstance(redacted, (dict, list, tuple)):
        return _bound_value(redacted, depth=depth, budget=budget, seen=seen)
    return _safe_text(redacted)


def _coerce_point(value: PointLike) -> HookPoint:
    """Normalize supported enum and string spellings to a hook point."""
    if isinstance(value, HookPoint):
        return value
    raw = _safe_text(value, 128).strip().casefold()
    if not raw:
        raise HookValidationError("hook point must not be empty")
    normalized = raw.replace("-", "_").replace("/", "_").replace(".", "_")
    normalized = normalized.replace(":", "_").replace(" ", "_")
    aliases = {
        "task_before": HookPoint.TASK_BEFORE,
        "before_task": HookPoint.TASK_BEFORE,
        "taskbefore": HookPoint.TASK_BEFORE,
        "task_start": HookPoint.TASK_BEFORE,
        "task_after": HookPoint.TASK_AFTER,
        "after_task": HookPoint.TASK_AFTER,
        "task_end": HookPoint.TASK_AFTER,
        "tool_before": HookPoint.TOOL_BEFORE,
        "before_tool": HookPoint.TOOL_BEFORE,
        "toolbefore": HookPoint.TOOL_BEFORE,
        "tool_after": HookPoint.TOOL_AFTER,
        "after_tool": HookPoint.TOOL_AFTER,
        "tool_end": HookPoint.TOOL_AFTER,
        "permission_before": HookPoint.PERMISSION_BEFORE,
        "before_permission": HookPoint.PERMISSION_BEFORE,
        "permissionbefore": HookPoint.PERMISSION_BEFORE,
        "permission_after": HookPoint.PERMISSION_AFTER,
        "after_permission": HookPoint.PERMISSION_AFTER,
        "permission_end": HookPoint.PERMISSION_AFTER,
        "permission": HookPoint.PERMISSION_BEFORE,
        "completion": HookPoint.COMPLETION_AFTER,
        "completion_before": HookPoint.COMPLETION_BEFORE,
        "before_completion": HookPoint.COMPLETION_BEFORE,
        "completion_after": HookPoint.COMPLETION_AFTER,
        "after_completion": HookPoint.COMPLETION_AFTER,
        "completion_end": HookPoint.COMPLETION_AFTER,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return HookPoint(raw)
    except (TypeError, ValueError):
        raise HookValidationError(f"unsupported hook point: {value!r}") from None


def _redacted_run_result(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Return a redacted RunResult or a safe projection for result-like data."""
    if isinstance(value, RunResult):
        try:
            clean = security.redact_secrets(value, secrets=secrets)
            return RunResult.from_dict(clean)
        except Exception:
            return _bound_value(value)
    if isinstance(value, Mapping):
        clean = _bound_value(value)
        if isinstance(clean, Mapping) and "status" in clean:
            try:
                return RunResult.from_dict(clean)
            except Exception:
                return clean
        return clean
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _redacted_run_result(dataclasses.asdict(value), secrets)
        except Exception:
            return _bound_value(value)
    return _bound_value(value)


@dataclass
class HookContext:
    """Redacted input and metadata passed to one hook callback."""

    point: PointLike = HookPoint.TASK_BEFORE
    task_id: str = ""
    tool: str = ""
    event: str = ""
    action: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    arguments: Mapping[str, Any] = field(default_factory=dict)
    payload: Any = None
    result: Any = None
    permission: Any = None
    mutation: Mapping[str, Any] = field(default_factory=dict)
    secrets: Sequence[str] = field(default_factory=tuple, repr=False)
    task: Any = None
    event_data: Any = None
    value: Any = None
    _raw_result: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Normalize and redact every externally supplied context field."""
        self.point = _coerce_point(self.point)
        if self._raw_result is None:
            self._raw_result = self.result
        raw_context = self.context if isinstance(self.context, Mapping) else {}
        raw_data = self.data if isinstance(self.data, Mapping) else {}
        raw_metadata = self.metadata if isinstance(self.metadata, Mapping) else {}
        raw_arguments = self.arguments if isinstance(self.arguments, Mapping) else {}
        raw_payload = self.payload
        secret_values = (
            [self.secrets]
            if isinstance(self.secrets, str)
            else list(self.secrets or ())
        )
        for source in (raw_data, raw_context, raw_metadata, raw_arguments, raw_payload):
            if not isinstance(source, Mapping):
                continue
            try:
                items = source.items()
            except Exception:
                continue
            for key, value in items:
                if not security.is_sensitive_key(key):
                    continue
                values = value if isinstance(value, (list, tuple, set)) else [value]
                secret_values.extend(values)
        try:
            self.secrets = tuple(_safe_text(item, 512) for item in secret_values)
        except Exception:
            self.secrets = ()
        if not self.task_id:
            self.task_id = str(
                raw_context.get(
                    "task_id", raw_context.get("id", raw_data.get("task_id", ""))
                )
            )
        if not self.tool:
            self.tool = str(raw_context.get("tool", raw_data.get("tool", "")))
        self.task_id = _safe_text(self.task_id, 512)
        self.tool = _safe_text(self.tool, 512)
        self.event = _safe_text(self.event, 512)
        self.action = _safe_text(self.action, 128)
        self.data = _bound_value(self.data if isinstance(self.data, Mapping) else {})
        self.context = _bound_value(
            self.context if isinstance(self.context, Mapping) else {}
        )
        self.metadata = _bound_value(
            self.metadata if isinstance(self.metadata, Mapping) else {}
        )
        self.arguments = _bound_value(
            self.arguments if isinstance(self.arguments, Mapping) else {}
        )
        self.mutation = _bound_value(
            self.mutation if isinstance(self.mutation, Mapping) else {}
        )
        self.payload = _bound_value(self.payload)
        self.task = _bound_value(self.task)
        self.event_data = _bound_value(self.event_data)
        self.value = _bound_value(self.value)
        self.permission = self._redact_permission(self.permission)
        self.result = self._redact_result(self.result)

    def _redact_permission(self, value: Any) -> Any:
        """Return a safe permission view while retaining its canonical action."""
        if isinstance(value, PermissionDecision):
            try:
                return PermissionDecision(
                    matched_rule=_safe_text(value.matched_rule, 512),
                    action=_safe_text(value.action, 32).casefold(),
                    scope=_safe_text(value.scope, 128),
                    actor=_safe_text(value.actor, 128),
                    call_id=_safe_text(value.call_id, 512),
                    exact_effect=_safe_text(value.exact_effect),
                    reason=_safe_text(value.reason),
                    schema_version=value.schema_version,
                )
            except Exception:
                return _bound_value(value)
        if isinstance(value, str):
            return _safe_text(value, 64).casefold()
        return _bound_value(value)

    def _redact_result(self, value: Any) -> Any:
        """Return the redacted result projection exposed to callbacks."""
        return _redacted_run_result(value, self.secrets)

    @property
    def hook_point(self) -> HookPoint:
        """Return the normalized hook point alias."""
        return self.point

    @property
    def is_before(self) -> bool:
        """Return whether this point has the before phase."""
        return self.point.phase == "before"

    @property
    def is_after(self) -> bool:
        """Return whether this point has the after phase."""
        return self.point.phase == "after"

    @property
    def is_completion(self) -> bool:
        """Return whether this is a completion point."""
        return self.point.domain == "completion"

    @property
    def is_observational(self) -> bool:
        """Return whether callback decisions cannot affect canonical state."""
        return self.is_after or self.is_completion

    @property
    def permission_action(self) -> str:
        """Return a valid permission action or an empty string."""
        if isinstance(self.permission, PermissionDecision):
            return self.permission.action
        if isinstance(self.permission, str):
            return self.permission.casefold()
        if isinstance(self.permission, Mapping):
            return str(self.permission.get("action", "")).casefold()
        return ""

    @property
    def status(self) -> str:
        """Return the canonical result status when one is available."""
        if isinstance(self.result, RunResult):
            return str(self.result.status)
        if isinstance(self.result, Mapping):
            return str(self.result.get("status", ""))
        return ""

    @property
    def canonical_result(self) -> Any:
        """Return the redacted result projection exposed to callbacks."""
        return self.result

    @property
    def raw_result(self) -> Any:
        """Return the caller-owned canonical result without exposing it publicly."""
        return self._raw_result

    @property
    def raw_canonical_result(self) -> Any:
        """Return the raw canonical result under an explicit compatibility name."""
        return self._raw_result

    @property
    def result_data(self) -> Any:
        """Return a mapping-friendly result projection alias."""
        return self.result_projection

    @property
    def permission_decision(self) -> Any:
        """Return the redacted permission view."""
        return self.permission

    @property
    def result_projection(self) -> Any:
        """Return a JSON-friendly redacted projection of the result."""
        if isinstance(self.result, RunResult):
            try:
                return _bound_value(self.result.to_dict())
            except Exception:
                return {}
        return _bound_value(self.result)

    def with_updates(self, **updates: Any) -> "HookContext":
        """Return a redacted context copy with bounded field updates."""
        values = {
            "point": self.point,
            "task_id": self.task_id,
            "tool": self.tool,
            "event": self.event,
            "action": self.action,
            "data": self.data,
            "context": self.context,
            "metadata": self.metadata,
            "arguments": self.arguments,
            "payload": self.payload,
            "result": self.result,
            "permission": self.permission,
            "mutation": self.mutation,
            "secrets": self.secrets,
            "task": self.task,
            "event_data": self.event_data,
            "value": self.value,
            "_raw_result": self._raw_result,
        }
        known = set(values)
        if "result" in updates:
            values["_raw_result"] = updates["result"]
        extra = {key: item for key, item in updates.items() if key not in known}
        values.update({key: item for key, item in updates.items() if key in known})
        if extra:
            data = dict(self.data) if isinstance(self.data, Mapping) else {}
            data.update(extra)
            values["data"] = data
        return HookContext(**values)

    def to_dict(self) -> dict[str, Any]:
        """Return a bounded, redacted mapping suitable for audit storage."""
        return {
            "point": self.point.value,
            "task_id": self.task_id,
            "tool": self.tool,
            "event": self.event,
            "action": self.action,
            "data": _bound_value(self.data),
            "context": _bound_value(self.context),
            "metadata": _bound_value(self.metadata),
            "arguments": _bound_value(self.arguments),
            "payload": _bound_value(self.payload),
            "result": self.result_projection,
            "permission": _bound_value(self.permission),
            "mutation": _bound_value(self.mutation),
            "task": _bound_value(self.task),
            "event_data": _bound_value(self.event_data),
            "value": _bound_value(self.value),
        }

    def __repr__(self) -> str:
        """Return a repr that cannot expose context secrets."""
        return (
            "HookContext("
            f"point={self.point.value!r}, task_id={self.task_id!r}, tool={self.tool!r}, "
            f"data={_safe_repr(self.data)}, metadata={_safe_repr(self.metadata)}, "
            f"payload={_safe_repr(self.payload)}, result={_safe_repr(self.result)})"
        )

    @classmethod
    def from_value(
        cls,
        value: Any = None,
        point: PointLike = HookPoint.TASK_BEFORE,
        **updates: Any,
    ) -> "HookContext":
        """Build a context from a context, mapping, object, or keyword values."""
        if isinstance(value, cls):
            requested = _coerce_point(point)
            if updates:
                selected_updates = dict(updates)
                selected_updates.pop("point", None)
                if selected_updates or value.point != requested:
                    return value.with_updates(point=requested, **selected_updates)
                return value
            if value.point != requested:
                return value.with_updates(point=requested)
            return value
        known = {
            "point",
            "task_id",
            "tool",
            "event",
            "action",
            "data",
            "context",
            "metadata",
            "arguments",
            "payload",
            "result",
            "permission",
            "mutation",
            "secrets",
            "task",
            "event_data",
            "value",
        }
        requested_point = _coerce_point(point)
        if value is None:
            values = {"point": requested_point, **updates}
        elif isinstance(value, Mapping):
            values = dict(value)
            values.update(updates)
        else:
            values = {"point": requested_point, "context": value, **updates}
        values["point"] = requested_point
        extra = {key: item for key, item in values.items() if key not in known}
        values = {key: item for key, item in values.items() if key in known}
        if extra:
            existing = values.get("data")
            data = dict(existing) if isinstance(existing, Mapping) else {}
            data.update(extra)
            values["data"] = data
        return cls(**values)

    def __getitem__(self, key: str) -> Any:
        """Provide mapping-style access to common context fields."""
        if key == "data":
            return self.data
        if key == "metadata":
            return self.metadata
        if key == "payload":
            return self.payload
        if key == "result":
            return self.result
        if key in {"result_data", "result_projection"}:
            return self.result_projection
        if key == "canonical_result":
            return self.result
        if key in {"task_id", "tool", "event", "action", "arguments", "permission"}:
            return getattr(self, key)
        raise KeyError(key)


@dataclass(init=False)
class HookDecision:
    """A bounded decision returned by a hook callback."""

    action: str
    reason: str
    mutation: dict[str, Any]
    payload: Any
    metadata: dict[str, Any]
    short_circuit: bool
    security_violation: bool

    def __init__(
        self,
        action: str = "allow",
        reason: str = "",
        mutation: Optional[Mapping[str, Any]] = None,
        payload: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
        short_circuit: bool = False,
        security_violation: bool = False,
        *,
        decision: Optional[str] = None,
        mutations: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        allow: Optional[bool] = None,
        deny: Optional[bool] = None,
    ) -> None:
        """Normalize a decision and bound its mutation, payload, and metadata."""
        if allow is False or deny is True:
            chosen = "deny"
        elif allow is True:
            chosen = "allow"
        else:
            chosen = decision if decision is not None else action
        try:
            normalized = _safe_text(chosen, 64).strip().casefold().replace("-", "_")
        except Exception:
            raise HookValidationError("hook decision action is invalid") from None
        if not normalized:
            normalized = "allow"
        combined: dict[str, Any] = {}
        for candidate in (mutation, mutations, data):
            if candidate is None:
                continue
            if not isinstance(candidate, Mapping):
                raise HookPayloadError("hook mutation must be a mapping")
            combined.update(dict(candidate))
        self.action = normalized
        self.reason = _safe_text(reason)
        self.mutation = _bound_value(combined)
        self.payload = _bound_value(payload)
        self.metadata = _bound_value(metadata if isinstance(metadata, Mapping) else {})
        self.short_circuit = bool(short_circuit) or normalized in {
            "short_circuit",
            "shortcircuit",
            "stop",
        }
        if self.short_circuit and normalized == "allow":
            self.action = "short_circuit"
        self.security_violation = bool(security_violation)

    @classmethod
    def allow(
        cls,
        reason: str = "",
        *,
        mutation: Optional[Mapping[str, Any]] = None,
        payload: Any = None,
    ) -> "HookDecision":
        """Construct an allowing decision."""
        return cls(action="allow", reason=reason, mutation=mutation, payload=payload)

    @classmethod
    def deny(cls, reason: str = "", *, payload: Any = None) -> "HookDecision":
        """Construct an explicit denial decision."""
        return cls(action="deny", reason=reason, payload=payload)

    @classmethod
    def mutate(
        cls,
        mutation: Mapping[str, Any],
        *,
        reason: str = "",
        payload: Any = None,
    ) -> "HookDecision":
        """Construct a bounded mutation decision."""
        return cls(action="mutate", reason=reason, mutation=mutation, payload=payload)

    @classmethod
    def short_circuit(
        cls,
        reason: str = "",
        *,
        payload: Any = None,
    ) -> "HookDecision":
        """Construct a decision that stops the current hook chain."""
        return cls(action="short_circuit", reason=reason, payload=payload)

    @property
    def decision(self) -> str:
        """Return the decision action alias."""
        return self.action

    @property
    def mutations(self) -> dict[str, Any]:
        """Return the mutation compatibility alias."""
        return self.mutation

    @property
    def denied(self) -> bool:
        """Return whether this decision explicitly denies execution."""
        return self.action == "deny"

    @property
    def allowed(self) -> bool:
        """Return whether this decision permits immediate continuation."""
        return self.action == "allow" or (
            self.action not in {"deny", "short_circuit", "ask"}
            and not self.denied
            and not self.short_circuit
        )

    @property
    def needs_approval(self) -> bool:
        """Return whether the decision requires an approval step."""
        return self.action == "ask"

    @property
    def is_denied(self) -> bool:
        """Return the explicit-denial alias."""
        return self.denied

    @property
    def is_short_circuit(self) -> bool:
        """Return the short-circuit alias."""
        return self.short_circuit

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted JSON-friendly decision mapping."""
        return {
            "action": self.action,
            "reason": self.reason,
            "mutation": _bound_value(self.mutation),
            "payload": _bound_value(self.payload),
            "metadata": _bound_value(self.metadata),
            "short_circuit": self.short_circuit,
            "security_violation": self.security_violation,
        }

    @classmethod
    def from_value(cls, value: DecisionValue) -> "HookDecision":
        """Normalize a callback return value into a typed decision."""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return cls(
                action=value.action,
                reason=value.reason,
                mutation=value.mutation,
                payload=value.payload,
                metadata=value.metadata,
                short_circuit=value.short_circuit,
                security_violation=value.security_violation,
            )
        if isinstance(value, HookOutcome):
            return value.decision
        if isinstance(value, bool):
            return cls(action="allow" if value else "deny")
        if isinstance(value, str):
            return cls(action=value)
        if isinstance(value, Mapping):
            data = dict(value)
            action = data.get("action", data.get("decision", data.get("status")))
            if data.get("allow") is False:
                action = "deny"
            elif data.get("allow") is True and action is None:
                action = "allow"
            if action is None and data.get("denied") is True:
                action = "deny"
            if action is None and data.get("deny") is True:
                action = "deny"
            if action is None and any(
                key in data for key in ("mutation", "mutations", "data", "payload")
            ):
                action = (
                    "mutate"
                    if any(key in data for key in ("mutation", "mutations", "data"))
                    else "allow"
                )
            return cls(
                action="allow" if action is None else action,
                reason=str(data.get("reason", "")),
                mutation=data.get("mutation", data.get("mutations", data.get("data"))),
                payload=data.get("payload"),
                metadata=data.get("metadata"),
                short_circuit=bool(data.get("short_circuit", False)),
                security_violation=bool(data.get("security_violation", False)),
            )
        decision = getattr(value, "decision", None)
        if decision is not None:
            return cls.from_value(decision)
        raise HookValidationError(f"unsupported hook decision: {type(value).__name__}")


@dataclass
class HookRecord:
    """One redacted audit row for a registered hook invocation."""

    hook_id: str
    point: HookPoint
    plugin_id: str = ""
    priority: int = 0
    registration_order: int = 0
    invoked: bool = False
    action: str = "allow"
    allowed: bool = True
    skipped: bool = False
    error: Optional[str] = None
    context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    result: Any = None
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0
    name: str = ""
    canonical_result: Any = field(default=None, repr=False, compare=False)
    raw_metadata: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Redact all audit fields and normalize enum values."""
        self.point = _coerce_point(self.point)
        supplied_raw_metadata = (
            dict(self.raw_metadata) if isinstance(self.raw_metadata, Mapping) else {}
        )
        if self.canonical_result is not None:
            supplied_raw_metadata.setdefault("canonical_result", self.canonical_result)
        self.raw_metadata = supplied_raw_metadata
        self.hook_id = _safe_text(self.hook_id, 256)
        self.plugin_id = _safe_text(self.plugin_id, 256)
        self.name = _safe_text(self.name, 256)
        self.priority = int(self.priority)
        self.registration_order = int(self.registration_order)
        self.action = _safe_text(self.action, 64).casefold()
        self.allowed = bool(self.allowed)
        self.invoked = bool(self.invoked)
        self.skipped = bool(self.skipped)
        self.error = None if self.error is None else _safe_error(Exception(self.error))
        self.context = _bound_value(self.context)
        self.metadata = _bound_value(self.metadata)
        self.result = _bound_value(self.result)
        self.duration_s = max(0.0, float(self.duration_s))

    @property
    def hook(self) -> str:
        """Return the registration identifier alias."""
        return self.hook_id

    @property
    def order(self) -> int:
        """Return the registration-order alias."""
        return self.registration_order

    @property
    def success(self) -> bool:
        """Return whether the row represents a successful invocation."""
        return self.invoked and self.error is None

    @property
    def decision(self) -> str:
        """Return the row action alias."""
        return self.action

    @property
    def raw_result(self) -> Any:
        """Return the raw canonical result retained for internal audit use."""
        return self.canonical_result

    @property
    def raw_canonical_result(self) -> Any:
        """Return the raw canonical result under an explicit name."""
        return self.canonical_result

    @property
    def audit_metadata(self) -> dict[str, Any]:
        """Return non-serialized audit metadata for trusted in-process callers."""
        value = dict(self.raw_metadata)
        if self.canonical_result is not None:
            value.setdefault("canonical_result", self.canonical_result)
        return value

    @property
    def nonredacted_metadata(self) -> dict[str, Any]:
        """Return the trusted in-process metadata alias."""
        return self.audit_metadata

    @property
    def status(self) -> str:
        """Return a compact row status."""
        if self.skipped:
            return "skipped"
        if self.error:
            return "error"
        return self.action

    @property
    def callback_name(self) -> str:
        """Return the registration name alias."""
        return self.name

    @property
    def timestamp(self) -> float:
        """Return the audit timestamp alias."""
        return self.started_at

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted mapping for structured audit storage."""
        return {
            "hook_id": self.hook_id,
            "point": self.point.value,
            "plugin_id": self.plugin_id,
            "name": self.name,
            "priority": self.priority,
            "registration_order": self.registration_order,
            "invoked": self.invoked,
            "action": self.action,
            "allowed": self.allowed,
            "skipped": self.skipped,
            "error": self.error,
            "context": _bound_value(self.context),
            "metadata": _bound_value(self.metadata),
            "result": _bound_value(self.result),
            "started_at": self.started_at,
            "duration_s": self.duration_s,
        }

    def __repr__(self) -> str:
        """Return a repr that redacts errors, context, and results."""
        return (
            "HookRecord("
            f"hook_id={self.hook_id!r}, point={self.point.value!r}, "
            f"plugin_id={self.plugin_id!r}, action={self.action!r}, "
            f"allowed={self.allowed!r}, skipped={self.skipped!r}, "
            f"error={self.error!r}, metadata={_safe_repr(self.metadata)})"
        )


@dataclass(init=False)
class HookOutcome:
    """The aggregate result of dispatching one hook point."""

    point: HookPoint
    decision: HookDecision
    records: list[HookRecord]
    result: Any
    context: HookContext
    mutation: dict[str, Any]
    payload: Any
    errors: tuple[str, ...]
    security_violation: bool

    def __init__(
        self,
        point: PointLike = HookPoint.TASK_BEFORE,
        decision: Optional[HookDecision] = None,
        records: Optional[Sequence[HookRecord]] = None,
        result: Any = None,
        context: Optional[HookContext] = None,
        mutation: Optional[Mapping[str, Any]] = None,
        payload: Any = None,
        errors: Sequence[str] = (),
        security_violation: bool = False,
        *,
        allowed: Optional[bool] = None,
        denied: Optional[bool] = None,
        short_circuited: Optional[bool] = None,
    ) -> None:
        """Build an aggregate outcome with safe public aliases."""
        self.point = _coerce_point(point)
        self.decision = (
            decision
            if isinstance(decision, HookDecision)
            else HookDecision.from_value(decision)
        )
        if denied is True:
            self.decision = HookDecision(
                action="deny",
                reason=self.decision.reason,
                mutation=self.decision.mutation,
                payload=self.decision.payload,
                metadata=self.decision.metadata,
            )
        elif short_circuited is True or (allowed is False and not self.decision.denied):
            self.decision = HookDecision(
                action="short_circuit",
                reason=self.decision.reason,
                mutation=self.decision.mutation,
                payload=self.decision.payload,
                metadata=self.decision.metadata,
            )
        self.records = list(records or [])
        self.result = result
        self.context = context or HookContext(point=self.point)
        self.mutation = _bound_value(mutation if isinstance(mutation, Mapping) else {})
        self.payload = _bound_value(payload)
        self.errors = tuple(_safe_error(Exception(item)) for item in errors or ())
        self.security_violation = (
            bool(security_violation) or self.decision.security_violation
        )

    @property
    def action(self) -> str:
        """Return the aggregate decision action."""
        return self.decision.action

    @property
    def status(self) -> str:
        """Return a compact aggregate status."""
        if self.denied:
            return "denied"
        if self.needs_approval:
            return "needs_approval"
        if self.short_circuited:
            return "short_circuited"
        if self.errors:
            return "allowed_with_errors"
        return "allowed"

    @property
    def hook_records(self) -> list[HookRecord]:
        """Return the per-hook rows for this dispatch."""
        return list(self.records)

    @property
    def allowed(self) -> bool:
        """Return whether the aggregate decision permits the operation."""
        return self.decision.allowed

    @property
    def denied(self) -> bool:
        """Return whether the aggregate decision explicitly denies the operation."""
        return self.decision.denied

    @property
    def needs_approval(self) -> bool:
        """Return whether the aggregate permission decision needs approval."""
        return self.decision.needs_approval

    @property
    def reason(self) -> str:
        """Return the aggregate decision reason."""
        return self.decision.reason

    @property
    def short_circuited(self) -> bool:
        """Return whether the chain was explicitly short-circuited."""
        return self.decision.short_circuit

    @property
    def short_circuit(self) -> bool:
        """Return the short-circuit alias."""
        return self.short_circuited

    @property
    def should_continue(self) -> bool:
        """Return whether a caller may continue after this outcome."""
        return self.allowed and not self.short_circuited

    @property
    def mutations(self) -> dict[str, Any]:
        """Return the mutation compatibility alias."""
        return self.mutation

    @property
    def data(self) -> dict[str, Any]:
        """Return the safe mutation compatibility alias."""
        return self.mutation

    @property
    def canonical_result(self) -> Any:
        """Return the result that must remain authoritative for the caller."""
        return self.result

    @property
    def canonical_status(self) -> str:
        """Return the canonical result status when available."""
        if isinstance(self.result, RunResult):
            return str(self.result.status)
        if isinstance(self.result, Mapping):
            return str(self.result.get("status", ""))
        return ""

    @property
    def safe_result(self) -> Any:
        """Return a redacted projection of the canonical result."""
        if isinstance(self.result, RunResult):
            return _redacted_run_result(self.result)
        return _bound_value(self.result)

    @property
    def error(self) -> Optional[str]:
        """Return the first isolated error, if any."""
        return self.errors[0] if self.errors else None

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted mapping of the outcome and audit rows."""
        return {
            "point": self.point.value,
            "action": self.action,
            "allowed": self.allowed,
            "denied": self.denied,
            "short_circuited": self.short_circuited,
            "decision": self.decision.to_dict(),
            "mutation": _bound_value(self.mutation),
            "payload": _bound_value(self.payload),
            "errors": list(self.errors),
            "security_violation": self.security_violation,
            "result": _bound_value(self.safe_result),
            "records": [record.to_dict() for record in self.records],
        }

    def __repr__(self) -> str:
        """Return a repr with no raw result, error, or context data."""
        return (
            "HookOutcome("
            f"point={self.point.value!r}, action={self.action!r}, "
            f"allowed={self.allowed!r}, denied={self.denied!r}, "
            f"short_circuited={self.short_circuited!r}, "
            f"mutation={_safe_repr(self.mutation)}, payload={_safe_repr(self.payload)}, "
            f"errors={self.errors!r}, records={len(self.records)})"
        )


def _copy_dispatch_context(context: HookContext, point: HookPoint) -> HookContext:
    """Return an isolated callback view while retaining the normalized point."""
    try:
        copied = copy.deepcopy(context)
    except Exception:
        copied = context.with_updates(point=point)
        copied._raw_result = context.raw_result
    if not isinstance(copied, HookContext):
        copied = context.with_updates(point=point)
        copied._raw_result = context.raw_result
    copied = copied.with_updates(point=point)
    copied._raw_result = context.raw_result
    return copied


@dataclass
class _Registration:
    """Internal immutable-after-registration hook record."""

    hook_id: str
    point: HookPoint
    callback: Callback
    priority: int
    order: int
    plugin_id: str
    name: str
    metadata: Mapping[str, Any]


class HookManager:
    """Thread-safe deterministic dispatcher and owner for hook callbacks."""

    _owner: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
        "neo_hook_owner", default=None
    )

    def __init__(self, *, max_records: int = _MAX_HOOK_RECORDS) -> None:
        """Create an empty manager with an audit-history safety bound."""
        if isinstance(max_records, bool) or int(max_records) <= 0:
            raise HookRegistrationError("max_records must be a positive integer")
        self._max_records = int(max_records)
        self._owner = contextvars.ContextVar(f"neo_hook_owner_{id(self)}", default=None)
        self._lock = threading.RLock()
        self._registrations: dict[str, _Registration] = {}
        self._records: list[HookRecord] = []
        self._owner_locks: dict[str, threading.RLock] = {}
        self._sequence = 0

    def owner_lock(self, plugin_id: str) -> threading.RLock:
        """Return the lifecycle lock shared with plugin deactivation."""
        owner = _safe_text(plugin_id, 256)
        with self._lock:
            return self._owner_locks.setdefault(owner, threading.RLock())

    def _next_sequence(self) -> int:
        """Allocate a monotonic registration order under the manager lock."""
        with self._lock:
            self._sequence += 1
            return self._sequence

    def register(
        self,
        point: PointLike,
        callback: Optional[Callback] = None,
        priority: int = 0,
        plugin_id: Optional[str] = None,
        name: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        owner: Optional[str] = None,
        plugin: Optional[str] = None,
    ) -> str:
        """Register a callback and return its stable removal token.

        ``priority`` is higher-is-first.  Equal priorities retain registration
        order.  A callback registered during plugin activation inherits the
        activation scope as its owner when ``plugin_id`` is omitted.
        """
        if callback is None:
            raise HookRegistrationError("hook callback must not be None")
        if not callable(callback):
            raise HookRegistrationError("hook callback must be callable")
        normalized_point = _coerce_point(point)
        if isinstance(priority, bool):
            raise HookRegistrationError("hook priority must be an integer")
        try:
            normalized_priority = int(priority)
        except (TypeError, ValueError):
            raise HookRegistrationError("hook priority must be an integer") from None
        selected_owner = plugin_id
        if selected_owner is None:
            selected_owner = plugin
        if selected_owner is None:
            selected_owner = owner
        owner_value = self._owner.get() if selected_owner is None else selected_owner
        owner_text = _safe_text(owner_value or "", 256)
        order = self._next_sequence()
        hook_id = f"hook-{order}"
        registration = _Registration(
            hook_id=hook_id,
            point=normalized_point,
            callback=callback,
            priority=normalized_priority,
            order=order,
            plugin_id=owner_text,
            name=_safe_text(name, 256),
            metadata=_bound_value(metadata if isinstance(metadata, Mapping) else {}),
        )
        with self._lock:
            self._registrations[hook_id] = registration
        return hook_id

    def register_hook(
        self,
        point: PointLike,
        callback: Optional[Callback] = None,
        priority: int = 0,
        plugin_id: Optional[str] = None,
        name: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        owner: Optional[str] = None,
        plugin: Optional[str] = None,
    ) -> str:
        """Register a hook using the explicit compatibility method name."""
        return self.register(
            point, callback, priority, plugin_id, name, metadata, owner, plugin
        )

    def add_hook(
        self,
        point: PointLike,
        callback: Optional[Callback] = None,
        priority: int = 0,
        plugin_id: Optional[str] = None,
        name: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        owner: Optional[str] = None,
        plugin: Optional[str] = None,
    ) -> str:
        """Register a hook using the short compatibility method name."""
        return self.register(
            point, callback, priority, plugin_id, name, metadata, owner, plugin
        )

    def hook(
        self,
        point: PointLike,
        *,
        priority: int = 0,
        plugin_id: Optional[str] = None,
        name: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Callable[[Callback], Callback]:
        """Return a decorator that registers the decorated callback."""

        def decorate(callback: Callback) -> Callback:
            self.register(
                point,
                callback,
                priority=priority,
                plugin_id=plugin_id,
                name=name or getattr(callback, "__name__", ""),
                metadata=metadata,
            )
            return callback

        return decorate

    def unregister(self, hook_id: str) -> bool:
        """Remove one registration and return whether it existed."""
        with self._lock:
            return self._registrations.pop(_safe_text(hook_id, 256), None) is not None

    def remove_hook(self, hook_id: str) -> bool:
        """Remove one registration using the compatibility method name."""
        return self.unregister(hook_id)

    def add(self, *args: Any, **kwargs: Any) -> str:
        """Register a hook using the shortest add alias."""
        return self.add_hook(*args, **kwargs)

    def remove(self, hook_id: str) -> bool:
        """Remove one registration using the short remove alias."""
        return self.unregister(hook_id)

    def remove_plugin(self, plugin_id: str) -> int:
        """Remove every registration owned by a plugin and return the count."""
        owner = _safe_text(plugin_id, 256)
        with self._lock:
            ids = [
                hook_id
                for hook_id, registration in self._registrations.items()
                if registration.plugin_id == owner
            ]
            for hook_id in ids:
                self._registrations.pop(hook_id, None)
        return len(ids)

    def remove_plugin_hooks(self, plugin_id: str) -> int:
        """Remove all hooks for a plugin using the explicit alias."""
        return self.remove_plugin(plugin_id)

    def remove_hooks(
        self,
        *,
        plugin_id: Optional[str] = None,
        point: Optional[PointLike] = None,
    ) -> int:
        """Remove registrations by owner and/or point."""
        owner = _safe_text(plugin_id, 256) if plugin_id is not None else None
        normalized = _coerce_point(point) if point is not None else None
        with self._lock:
            ids = [
                hook_id
                for hook_id, registration in self._registrations.items()
                if (owner is None or registration.plugin_id == owner)
                and (normalized is None or registration.point == normalized)
            ]
            for hook_id in ids:
                self._registrations.pop(hook_id, None)
        return len(ids)

    def clear(self, *, plugin_id: Optional[str] = None) -> int:
        """Remove all registrations or all registrations for one owner."""
        if plugin_id is None:
            with self._lock:
                count = len(self._registrations)
                self._registrations.clear()
                return count
        return self.remove_plugin(plugin_id)

    def _snapshot(self, point: HookPoint) -> list[_Registration]:
        """Return a stable sorted snapshot for one dispatch."""
        with self._lock:
            values = [
                registration
                for registration in self._registrations.values()
                if registration.point == point
            ]
        return sorted(values, key=lambda item: (-item.priority, item.order))

    def registrations_for_plugin(self, plugin_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return redacted registrations owned by one plugin."""
        return self.registrations_for_owner(plugin_id)

    def registrations_for_owner(self, plugin_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return redacted registrations owned by one plugin."""
        owner = _safe_text(plugin_id, 256)
        return tuple(
            item for item in self.registrations() if item["plugin_id"] == owner
        )

    def registration_ids(self, plugin_id: Optional[str] = None) -> tuple[str, ...]:
        """Return a deterministic snapshot of registration identifiers."""
        with self._lock:
            values = [
                (item.order, hook_id)
                for hook_id, item in self._registrations.items()
                if plugin_id is None or item.plugin_id == _safe_text(plugin_id, 256)
            ]
        return tuple(hook_id for _, hook_id in sorted(values))

    def registrations(
        self, point: Optional[PointLike] = None
    ) -> tuple[Mapping[str, Any], ...]:
        """Return redacted registration metadata without callback objects."""
        normalized = _coerce_point(point) if point is not None else None
        with self._lock:
            values = list(self._registrations.values())
        values.sort(key=lambda item: (item.point.value, -item.priority, item.order))
        return tuple(
            {
                "hook_id": item.hook_id,
                "point": item.point.value,
                "plugin_id": item.plugin_id,
                "name": item.name,
                "priority": item.priority,
                "registration_order": item.order,
                "metadata": _bound_value(item.metadata),
            }
            for item in values
            if normalized is None or item.point == normalized
        )

    @property
    def hooks(self) -> tuple[Mapping[str, Any], ...]:
        """Return redacted registrations as a compatibility view."""
        return self.registrations()

    @property
    def records(self) -> tuple[HookRecord, ...]:
        """Return a thread-safe snapshot of the redacted audit history."""
        with self._lock:
            return tuple(self._records)

    @property
    def audit_records(self) -> tuple[HookRecord, ...]:
        """Return the audit-history compatibility alias."""
        return self.records

    @property
    def history(self) -> tuple[HookRecord, ...]:
        """Return the audit-history alias."""
        return self.records

    def get_records(
        self,
        *,
        point: Optional[PointLike] = None,
        plugin_id: Optional[str] = None,
    ) -> tuple[HookRecord, ...]:
        """Return redacted audit rows filtered by point or plugin owner."""
        normalized = _coerce_point(point) if point is not None else None
        owner = _safe_text(plugin_id, 256) if plugin_id is not None else None
        return tuple(
            record
            for record in self.records
            if (normalized is None or record.point == normalized)
            and (owner is None or record.plugin_id == owner)
        )

    def clear_records(self) -> int:
        """Clear audit history and return the number of removed rows."""
        with self._lock:
            count = len(self._records)
            self._records.clear()
            return count

    def _append_record(self, record: HookRecord) -> None:
        """Append one audit row while retaining a bounded global history."""
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._max_records:
                del self._records[: len(self._records) - self._max_records]

    def plugin_scope(self, plugin_id: str) -> Any:
        """Return a context manager that assigns registrations to a plugin."""
        manager = self

        class _Scope:
            def __enter__(self) -> "HookManager":
                self.token = manager._owner.set(_safe_text(plugin_id, 256))
                return manager

            def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
                manager._owner.reset(self.token)
                return False

        return _Scope()

    def _invoke(self, callback: Callback, context: HookContext) -> Any:
        """Invoke sync callbacks without confusing internal TypeErrors with arity."""
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return callback(context)
        parameters = list(signature.parameters.values())
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        keyword_only = [
            parameter
            for parameter in parameters
            if parameter.kind == inspect.Parameter.KEYWORD_ONLY
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        )
        has_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
        if not positional and not has_varargs:
            required_keywords = [
                parameter
                for parameter in keyword_only
                if parameter.default is inspect.Parameter.empty
            ]
            if required_keywords:
                return callback(**{required_keywords[0].name: context})
            if has_kwargs:
                return callback(context=context)
            return callback()
        required = [
            parameter
            for parameter in positional
            if parameter.default is inspect.Parameter.empty
        ]
        if len(required) <= 1:
            return callback(context)
        if len(positional) >= 2:
            second_name = positional[1].name.casefold()
            secondary = (
                self
                if second_name in {"manager", "hooks", "hook_manager", "registry"}
                else context.point
            )
            return callback(context, secondary)
        return callback(context)

    @staticmethod
    def _default_decision(point: HookPoint, context: HookContext) -> HookDecision:
        """Return the safe default decision for a hook point."""
        if point.domain == "permission":
            action = context.permission_action
            return HookDecision(
                action=action if action in {"allow", "ask", "deny"} else "allow"
            )
        return HookDecision()

    @staticmethod
    def _validate_decision(point: HookPoint, decision: HookDecision) -> None:
        """Reject decisions that cannot be valid at a specific point."""
        if point.domain == "permission":
            if decision.action not in {"allow", "ask", "deny"}:
                raise HookValidationError(
                    f"permission hook returned invalid action: {decision.action}"
                )
            return
        if point.phase == "before" and point.domain != "completion":
            if decision.action not in {
                "allow",
                "deny",
                "mutate",
                "short_circuit",
                "stop",
                "shortcircuit",
            }:
                raise HookValidationError(
                    f"before hook returned invalid action: {decision.action}"
                )
            return
        if decision.action not in {
            "allow",
            "deny",
            "mutate",
            "short_circuit",
            "stop",
            "shortcircuit",
        }:
            raise HookValidationError(
                f"observational hook returned invalid action: {decision.action}"
            )

    @staticmethod
    def _merge_mutation(
        current: Mapping[str, Any], incoming: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Merge bounded hook mutations without allowing unbounded nesting."""
        merged = dict(current)
        for key, value in dict(incoming).items():
            if (
                key in merged
                and isinstance(merged[key], Mapping)
                and isinstance(value, Mapping)
            ):
                merged[key] = HookManager._merge_mutation(merged[key], value)
            else:
                merged[key] = value
        return _bound_value(merged)

    @staticmethod
    def _permission_combine(current: str, incoming: str) -> str:
        """Combine permission actions using deny, then ask, precedence."""
        rank = {"allow": 1, "ask": 2, "deny": 3}
        return incoming if rank.get(incoming, 0) > rank.get(current, 0) else current

    def _record_skipped(
        self,
        registration: _Registration,
        context: HookContext,
        reason: str,
        canonical_result: Any = None,
    ) -> HookRecord:
        """Create and store an audit row for a callback not invoked."""
        record = HookRecord(
            hook_id=registration.hook_id,
            point=registration.point,
            plugin_id=registration.plugin_id,
            priority=registration.priority,
            registration_order=registration.order,
            invoked=False,
            action="skipped",
            allowed=True,
            skipped=True,
            error=reason,
            context=context.to_dict(),
            metadata=registration.metadata,
            result=None,
            name=registration.name,
            canonical_result=canonical_result,
            raw_metadata={"canonical_result": canonical_result},
        )
        self._append_record(record)
        return record

    def dispatch(
        self,
        point: PointLike,
        context: Any = None,
        *,
        raise_on_deny: bool = False,
        **context_values: Any,
    ) -> HookOutcome:
        """Dispatch all callbacks for a point and return one bounded outcome.

        Ordinary callback and validation errors become redacted audit rows
        and do not stop later callbacks.  A shared security violation is
        recorded and re-raised as :class:`HookSecurityError`; explicit deny
        decisions are represented in the outcome unless ``raise_on_deny`` is
        requested.
        """
        normalized_point = _coerce_point(point)
        hook_context = HookContext.from_value(
            context,
            point=normalized_point,
            **context_values,
        )
        raw_result = None
        if isinstance(context, HookContext):
            raw_result = context.raw_result
            if raw_result is None:
                raw_result = context.result
        elif isinstance(context, Mapping):
            raw_result = context.get(
                "result",
                context.get("canonical_result", context.get("result_data")),
            )
        elif context is not None and not context_values:
            raw_result = getattr(context, "raw_result", None)
            if raw_result is None:
                raw_result = getattr(context, "result", None)
        if raw_result is None:
            raw_result = hook_context.raw_result
        canonical_result = raw_result if raw_result is not None else hook_context.result
        registrations = self._snapshot(normalized_point)
        aggregate = self._default_decision(normalized_point, hook_context)
        aggregate_mutation: dict[str, Any] = {}
        aggregate_payload: Any = None
        records: list[HookRecord] = []
        errors: list[str] = []
        current = hook_context
        stopped = False
        security_error: Optional[HookSecurityError] = None
        for registration in registrations:
            if stopped:
                records.append(
                    self._record_skipped(
                        registration,
                        current,
                        "chain stopped by an earlier decision",
                        canonical_result,
                    )
                )
                continue
            started = time.perf_counter()
            timestamp = time.time()
            record_action = "allow"
            record_allowed = True
            record_error: Optional[str] = None
            record_result: Any = None
            decision: Optional[HookDecision] = None
            try:
                callback_context = _copy_dispatch_context(current, normalized_point)
                owner_lock = (
                    self.owner_lock(registration.plugin_id)
                    if registration.plugin_id
                    else nullcontext()
                )
                with owner_lock:
                    returned = self._invoke(registration.callback, callback_context)
                if inspect.isawaitable(returned):
                    close = getattr(returned, "close", None)
                    if callable(close):
                        close()
                    raise HookDispatchError(
                        "asynchronous hook callbacks are not supported"
                    )
                decision = HookDecision.from_value(returned)
                self._validate_decision(normalized_point, decision)
                record_action = decision.action
                record_result = decision.payload
                if decision.security_violation:
                    raise HookSecurityError(decision.reason or "hook security denial")
            except (security.SecurityViolation, HookSecurityError) as exc:
                record_error = _safe_error(exc)
                record_action = "deny"
                record_allowed = False
                record = HookRecord(
                    hook_id=registration.hook_id,
                    point=registration.point,
                    plugin_id=registration.plugin_id,
                    priority=registration.priority,
                    registration_order=registration.order,
                    invoked=True,
                    action="security_denial",
                    allowed=False,
                    error=record_error,
                    context=current.to_dict(),
                    metadata=registration.metadata,
                    result=record_result,
                    started_at=timestamp,
                    duration_s=max(0.0, time.perf_counter() - started),
                    name=registration.name,
                    canonical_result=canonical_result,
                    raw_metadata={"canonical_result": canonical_result},
                )
                records.append(record)
                self._append_record(record)
                security_error = HookSecurityError(record_error)
                stopped = True
                break
            except Exception as exc:
                record_error = _safe_error(exc)
                errors.append(record_error)
                record_action = "error"
                record_result = None
            record = HookRecord(
                hook_id=registration.hook_id,
                point=registration.point,
                plugin_id=registration.plugin_id,
                priority=registration.priority,
                registration_order=registration.order,
                invoked=True,
                action=record_action,
                allowed=record_allowed,
                error=record_error,
                context=current.to_dict(),
                metadata=registration.metadata,
                result=record_result,
                started_at=timestamp,
                duration_s=max(0.0, time.perf_counter() - started),
                name=registration.name,
                canonical_result=canonical_result,
                raw_metadata={"canonical_result": canonical_result},
            )
            records.append(record)
            self._append_record(record)
            if decision is None:
                continue
            observational = (
                normalized_point.phase == "after"
                or normalized_point.domain == "completion"
            )
            if normalized_point.domain == "permission" and not observational:
                combined_action = self._permission_combine(
                    aggregate.action,
                    decision.action,
                )
                aggregate = HookDecision(
                    action=combined_action,
                    reason=decision.reason or aggregate.reason,
                    mutation=aggregate.mutation,
                    payload=decision.payload
                    if decision.payload is not None
                    else aggregate.payload,
                    metadata=decision.metadata or aggregate.metadata,
                )
                current = current.with_updates(
                    action=combined_action,
                    permission=combined_action,
                )
                if combined_action == "deny":
                    stopped = True
                continue
            if observational:
                continue
            if decision.mutation:
                aggregate_mutation = self._merge_mutation(
                    aggregate_mutation,
                    decision.mutation,
                )
                current = current.with_updates(
                    mutation=aggregate_mutation,
                    data=self._merge_mutation(current.data, decision.mutation),
                )
                if decision.payload is None and "payload" in decision.mutation:
                    aggregate_payload = decision.mutation["payload"]
                    current = current.with_updates(payload=aggregate_payload)
            if decision.payload is not None:
                aggregate_payload = decision.payload
                current = current.with_updates(payload=decision.payload)
            if decision.denied or decision.short_circuit:
                aggregate = HookDecision(
                    action=decision.action,
                    reason=decision.reason,
                    mutation=aggregate_mutation,
                    payload=aggregate_payload,
                    metadata=decision.metadata,
                    short_circuit=decision.short_circuit,
                    security_violation=decision.security_violation,
                )
                stopped = True
        if security_error is not None:
            for registration in registrations[len(records) :]:
                records.append(
                    self._record_skipped(
                        registration,
                        current,
                        "chain stopped by a security violation",
                        canonical_result,
                    )
                )
            raise security_error
        if not stopped and aggregate_mutation:
            aggregate = HookDecision(
                action=aggregate.action,
                reason=aggregate.reason,
                mutation=aggregate_mutation,
                payload=aggregate_payload,
                metadata=aggregate.metadata,
            )
        if normalized_point.domain == "permission" and not (
            normalized_point.phase == "after" or normalized_point.domain == "completion"
        ):
            aggregate = HookDecision(
                action=aggregate.action,
                reason=aggregate.reason,
                mutation=aggregate_mutation,
                payload=aggregate_payload,
                metadata=aggregate.metadata,
            )
        outcome = HookOutcome(
            point=normalized_point,
            decision=aggregate,
            records=records,
            result=canonical_result,
            context=current,
            mutation=aggregate_mutation,
            payload=aggregate_payload,
            errors=errors,
        )
        if raise_on_deny and (outcome.denied or outcome.short_circuited):
            reason = aggregate.reason or "hook explicitly denied execution"
            raise HookDeniedError(reason)
        return outcome

    def dispatch_hooks(
        self, point: PointLike, context: Any = None, **kwargs: Any
    ) -> HookOutcome:
        """Dispatch a point using the explicit compatibility method name."""
        return self.dispatch(point, context, **kwargs)

    def dispatch_hook(
        self, point: PointLike, context: Any = None, **kwargs: Any
    ) -> HookOutcome:
        """Dispatch one point using the singular compatibility name."""
        return self.dispatch(point, context, **kwargs)

    def run(self, point: PointLike, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch a point using the short compatibility method name."""
        return self.dispatch(point, context, **kwargs)

    def invoke(
        self, point: PointLike, context: Any = None, **kwargs: Any
    ) -> HookOutcome:
        """Dispatch a point using the callback-style compatibility name."""
        return self.dispatch(point, context, **kwargs)

    def before_task(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch task-before hooks."""
        return self.dispatch(HookPoint.TASK_BEFORE, context, **kwargs)

    def after_task(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch task-after hooks."""
        return self.dispatch(HookPoint.TASK_AFTER, context, **kwargs)

    def before_tool(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch tool-before hooks."""
        return self.dispatch(HookPoint.TOOL_BEFORE, context, **kwargs)

    def after_tool(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch tool-after hooks."""
        return self.dispatch(HookPoint.TOOL_AFTER, context, **kwargs)

    def before_permission(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch permission-before hooks."""
        return self.dispatch(HookPoint.PERMISSION_BEFORE, context, **kwargs)

    def after_permission(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch permission-after hooks."""
        return self.dispatch(HookPoint.PERMISSION_AFTER, context, **kwargs)

    def before_completion(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch completion-before hooks."""
        return self.dispatch(HookPoint.COMPLETION_BEFORE, context, **kwargs)

    def after_completion(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch completion-after hooks."""
        return self.dispatch(HookPoint.COMPLETION_AFTER, context, **kwargs)

    def completion(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch completion hooks using the completion-after point."""
        return self.dispatch(HookPoint.COMPLETION_AFTER, context, **kwargs)

    def dispatch_task_before(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch task-before hooks using an explicit point method name."""
        return self.before_task(context, **kwargs)

    def dispatch_task_after(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch task-after hooks using an explicit point method name."""
        return self.after_task(context, **kwargs)

    def dispatch_tool_before(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch tool-before hooks using an explicit point method name."""
        return self.before_tool(context, **kwargs)

    def dispatch_tool_after(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch tool-after hooks using an explicit point method name."""
        return self.after_tool(context, **kwargs)

    def dispatch_permission_before(
        self, context: Any = None, **kwargs: Any
    ) -> HookOutcome:
        """Dispatch permission-before hooks using an explicit point method name."""
        return self.before_permission(context, **kwargs)

    def dispatch_permission_after(
        self, context: Any = None, **kwargs: Any
    ) -> HookOutcome:
        """Dispatch permission-after hooks using an explicit point method name."""
        return self.after_permission(context, **kwargs)

    def dispatch_completion(self, context: Any = None, **kwargs: Any) -> HookOutcome:
        """Dispatch completion hooks using an explicit point method name."""
        return self.completion(context, **kwargs)

    def dispatch_before(
        self, point: PointLike, context: Any = None, **kwargs: Any
    ) -> HookOutcome:
        """Dispatch a before point after normalizing its phase."""
        normalized = _coerce_point(point)
        if normalized.domain == "completion":
            normalized = HookPoint.COMPLETION_BEFORE
        elif normalized.phase != "before":
            normalized = HookPoint(f"{normalized.domain}.before")
        return self.dispatch(normalized, context, **kwargs)

    def dispatch_after(
        self, point: PointLike, context: Any = None, **kwargs: Any
    ) -> HookOutcome:
        """Dispatch an after point after normalizing its phase."""
        normalized = _coerce_point(point)
        if normalized.domain == "completion":
            normalized = HookPoint.COMPLETION_AFTER
        elif normalized.phase != "after":
            normalized = HookPoint(f"{normalized.domain}.after")
        return self.dispatch(normalized, context, **kwargs)

    def __repr__(self) -> str:
        """Return a callback-free manager repr."""
        with self._lock:
            registrations = len(self._registrations)
            records = len(self._records)
        return f"HookManager(registrations={registrations}, records={records})"


__all__ = [
    "Callback",
    "DecisionValue",
    "HookContext",
    "HookDecision",
    "HookDeniedError",
    "HookDispatchError",
    "HookError",
    "HookManager",
    "HookOutcome",
    "HookPayloadError",
    "HookPoint",
    "HookRecord",
    "HookRegistrationError",
    "HookSecurityError",
    "HookValidationError",
    "PointLike",
]
