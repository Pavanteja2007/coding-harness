"""Shared cost, token, context, event, and provider-health telemetry."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from .security import (
    SecurityViolation,
    is_sensitive_key,
    redact_secrets,
    redact_text,
    require_contained,
    safe_segment,
)

__all__ = [
    "ProviderHealth",
    "ProviderHealthTracker",
    "TelemetryEvent",
    "TelemetryStore",
    "enabled",
    "health_snapshot",
    "observe_event",
    "provider_health",
    "read_telemetry",
    "record_context",
    "record_event",
    "record_model_usage",
    "record_provider_failure",
    "record_provider_success",
    "summarize_telemetry",
    "telemetry_dir",
]

PathLike = Union[str, os.PathLike[str]]
TELEMETRY_DIR_ENV = "NEO_TELEMETRY_DIR"
TELEMETRY_ENABLED_ENV = "NEO_TELEMETRY_ENABLED"


@dataclass(frozen=True)
class TelemetryEvent:
    """One bounded, credential-free telemetry observation."""

    ts: float
    name: str
    module: str = ""
    task_id: str = ""
    run_id: str = ""
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    context_tokens: int = 0
    context_limit: int = 0
    event_count: int = 0
    provider: str = ""
    model: str = ""
    outcome: str = ""
    error_class: str = ""
    latency_ms: float = 0.0
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a redacted JSON-compatible event."""
        return redact_secrets(
            {
                "ts": round(float(self.ts), 3),
                "name": self.name,
                "module": self.module,
                "task_id": self.task_id,
                "run_id": self.run_id,
                "tokens": int(self.tokens),
                "prompt_tokens": int(self.prompt_tokens),
                "completion_tokens": int(self.completion_tokens),
                "cost_usd": round(float(self.cost_usd), 8),
                "context_tokens": int(self.context_tokens),
                "context_limit": int(self.context_limit),
                "event_count": int(self.event_count),
                "provider": self.provider,
                "model": self.model,
                "outcome": self.outcome,
                "error_class": self.error_class,
                "latency_ms": round(float(self.latency_ms), 3),
                "attributes": dict(self.attributes),
            }
        )


def _env_enabled() -> bool:
    value = str(os.environ.get(TELEMETRY_ENABLED_ENV, "1")).strip().casefold()
    return value not in {"0", "false", "no", "off"}


def telemetry_dir(root: Optional[PathLike] = None) -> Optional[Path]:
    """Resolve the telemetry root, or ``None`` when telemetry is disabled."""
    if not _env_enabled():
        return None
    if root is not None:
        return Path(root)
    value = os.environ.get(TELEMETRY_DIR_ENV) or os.environ.get("NEO_TRACE_DIR")
    return Path(value) if value and str(value).strip() else None


def enabled(root: Optional[PathLike] = None) -> bool:
    """Return whether telemetry output is enabled."""
    return telemetry_dir(root) is not None


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _error_class(value: Any) -> str:
    if isinstance(value, BaseException):
        return type(value).__name__
    text = str(value or "").casefold()
    if "rate" in text or "429" in text:
        return "rate_limit"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "auth" in text or "credential" in text or "api key" in text:
        return "authentication"
    if "connect" in text or "network" in text:
        return "connection"
    if text:
        return "provider_error"
    return ""


def _usage(fields: Mapping[str, Any]) -> tuple[int, int, int, float]:
    usage = fields.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}
    prompt = _safe_int(
        fields.get(
            "prompt_tokens", usage.get("prompt_tokens", usage.get("input_tokens"))
        )
    )
    completion = _safe_int(
        fields.get(
            "completion_tokens",
            usage.get("completion_tokens", usage.get("output_tokens")),
        )
    )
    total = _safe_int(
        fields.get("tokens", usage.get("total_tokens", usage.get("tokens")))
    )
    if not total:
        total = prompt + completion
    if not prompt and not completion and total:
        prompt = total
    cost = _safe_float(fields.get("cost_usd", usage.get("cost_usd", usage.get("cost"))))
    return prompt, completion, total, cost


def _context_values(fields: Mapping[str, Any]) -> tuple[int, int]:
    context = fields.get("context")
    if not isinstance(context, Mapping):
        context = {}
    used = fields.get(
        "context_tokens", fields.get("used_tokens", context.get("tokens"))
    )
    if used is None and fields.get("characters") is not None:
        used = (max(0, _safe_int(fields.get("characters"))) + 3) // 4
    limit = fields.get(
        "context_limit", fields.get("token_budget", context.get("limit"))
    )
    return _safe_int(used), _safe_int(limit)


def _attributes(fields: Mapping[str, Any]) -> dict[str, Any]:
    blocked = {
        "messages",
        "prompt",
        "response",
        "content",
        "command",
        "stdout",
        "stderr",
        "raw",
        "diff",
        "issue_text",
        "error",
        "api_key",
        "token",
        "password",
        "secret",
    }
    result: dict[str, Any] = {}
    for key, value in fields.items():
        name = str(key)
        lowered = name.casefold()
        if lowered in blocked or is_sensitive_key(name):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[name] = redact_secrets(value, name)
        elif isinstance(value, Mapping):
            result[name] = redact_secrets(value)
        else:
            result[name] = redact_text(value)
        if len(result) >= 64:
            break
    return result


def _telemetry_path(root: PathLike, task_id: str = "", run_id: str = "") -> Path:
    if task_id and not safe_segment(task_id):
        task_id = ""
    if run_id and not safe_segment(run_id):
        run_id = ""
    if task_id:
        relative = Path("_telemetry") / f"{task_id}.jsonl"
    elif run_id:
        relative = Path("_telemetry") / f"_run-{run_id}.jsonl"
    else:
        relative = Path("_telemetry") / "all.jsonl"
    return require_contained(root, relative)


class TelemetryStore:
    """Append-only telemetry journal for one task, run, or aggregate stream."""

    def __init__(self, root: PathLike, task_id: str = "", run_id: str = "") -> None:
        self.root = Path(root)
        self.task_id = task_id
        self.run_id = run_id
        self.path = _telemetry_path(root, task_id=task_id, run_id=run_id)
        self._lock = threading.RLock()

    def record(self, event: TelemetryEvent | Mapping[str, Any]) -> dict[str, Any]:
        """Persist one event after redaction and JSON normalisation."""
        value = (
            event.as_dict()
            if isinstance(event, TelemetryEvent)
            else redact_secrets(dict(event))
        )
        if not isinstance(value, dict):
            raise SecurityViolation("telemetry event must be an object")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise SecurityViolation(
                    "telemetry event could not be persisted"
                ) from exc
        return value

    def read(self) -> list[dict[str, Any]]:
        """Read valid rows, skipping malformed lines."""
        rows: list[dict[str, Any]] = []
        try:
            handle = self.path.open("r", encoding="utf-8")
        except (FileNotFoundError, OSError):
            return rows
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(redact_secrets(value))
        return rows

    def summary(self) -> dict[str, Any]:
        """Return aggregate cost/token/context/event metrics."""
        return summarize_telemetry(self.read())


def record_event(
    name: str,
    *,
    task_id: str = "",
    run_id: str = "",
    module: str = "",
    root: Optional[PathLike] = None,
    timestamp: Optional[float] = None,
    **fields: Any,
) -> Optional[dict[str, Any]]:
    """Record one normalized event; return ``None`` when telemetry is off."""
    target = telemetry_dir(root)
    if target is None:
        return None
    prompt, completion, tokens, cost = _usage(fields)
    context_tokens, context_limit = _context_values(fields)
    provider = str(fields.get("provider") or "")
    model = str(fields.get("model") or "")
    outcome = str(fields.get("outcome") or "")
    error_value = fields.get("error_class", fields.get("error"))
    latency = fields.get("latency_ms")
    if latency is None:
        latency = _safe_float(fields.get("elapsed_s")) * 1000.0
    event = TelemetryEvent(
        ts=time.time() if timestamp is None else float(timestamp),
        name=str(name or "event"),
        module=str(module or ""),
        task_id=str(task_id or ""),
        run_id=str(run_id or ""),
        tokens=tokens,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=cost,
        context_tokens=context_tokens,
        context_limit=context_limit,
        event_count=_safe_int(fields.get("event_count")),
        provider=provider,
        model=model,
        outcome=outcome,
        error_class=_error_class(error_value),
        latency_ms=_safe_float(latency),
        attributes=_attributes(fields),
    )
    result = TelemetryStore(target, task_id=task_id, run_id=run_id).record(event)
    if provider and (outcome or error_value is not None):
        try:
            ProviderHealthTracker(target).record(
                provider,
                model,
                success=outcome.casefold() in {"success", "ok", "accepted"},
                outcome=outcome,
                error=error_value,
                latency_s=_safe_float(fields.get("elapsed_s")),
            )
        except Exception:
            pass
    return result


record_model_usage = record_event


def record_context(
    used_tokens: int,
    limit_tokens: int = 0,
    *,
    task_id: str = "",
    run_id: str = "",
    root: Optional[PathLike] = None,
    **fields: Any,
) -> Optional[dict[str, Any]]:
    """Record a context-window usage observation."""
    return record_event(
        "context",
        task_id=task_id,
        run_id=run_id,
        module=str(fields.pop("module", "harness")),
        root=root,
        context_tokens=used_tokens,
        context_limit=limit_tokens,
        **fields,
    )


def observe_event(
    record: Mapping[str, Any], root: Optional[PathLike] = None
) -> Optional[dict[str, Any]]:
    """Convert a normalized trace event into a telemetry observation."""
    if not isinstance(record, Mapping):
        return None
    value = dict(record)
    name = str(value.pop("event", "event"))
    return record_event(
        name,
        task_id=str(value.pop("task_id", "") or ""),
        run_id=str(value.pop("run_id", "") or ""),
        module=str(value.pop("module", "") or ""),
        root=root,
        timestamp=value.pop("ts", None),
        **value,
    )


def read_telemetry(
    task_id: str = "",
    run_id: str = "",
    *,
    root: Optional[PathLike] = None,
) -> list[dict[str, Any]]:
    """Read one telemetry stream without following symlinks."""
    target = telemetry_dir(root)
    if target is None:
        return []
    return TelemetryStore(target, task_id=task_id, run_id=run_id).read()


def summarize_telemetry(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate cost, token, context, event, and provider metrics."""
    rows = [dict(event) for event in events if isinstance(event, Mapping)]
    by_name: dict[str, int] = {}
    by_provider: dict[str, dict[str, Any]] = {}
    total_tokens = 0
    total_cost = 0.0
    total_context = 0
    total_events = 0
    for row in rows:
        name = redact_text(str(row.get("name") or row.get("event") or "event"))
        by_name[name] = by_name.get(name, 0) + 1
        total_events += 1
        total_tokens += _safe_int(row.get("tokens"))
        total_cost += _safe_float(row.get("cost_usd"))
        total_context += _safe_int(row.get("context_tokens"))
        provider = redact_text(str(row.get("provider") or ""))
        if provider:
            bucket = by_provider.setdefault(
                provider, {"events": 0, "tokens": 0, "cost_usd": 0.0, "failures": 0}
            )
            bucket["events"] += 1
            bucket["tokens"] += _safe_int(row.get("tokens"))
            bucket["cost_usd"] += _safe_float(row.get("cost_usd"))
            if str(row.get("outcome") or "").casefold() in {
                "error",
                "failed",
                "failure",
            } or row.get("error_class"):
                bucket["failures"] += 1
    return {
        "events": total_events,
        "tokens": total_tokens,
        "cost_usd": round(total_cost, 8),
        "context_tokens": total_context,
        "by_event": dict(sorted(by_name.items())),
        "by_provider": {key: dict(value) for key, value in sorted(by_provider.items())},
    }


@dataclass
class ProviderHealth:
    """Non-secret aggregate health for one provider/model endpoint."""

    calls: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    total_latency_ms: float = 0.0
    last_success_ts: float = 0.0
    last_failure_ts: float = 0.0
    error_classes: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        """Return successful calls divided by observed calls."""
        return self.successes / self.calls if self.calls else 0.0

    @property
    def status(self) -> str:
        """Return healthy, degraded, unhealthy, or unknown state."""
        if not self.calls:
            return "unknown"
        if self.success_rate >= 0.9 and self.consecutive_failures < 3:
            return "healthy"
        if self.success_rate >= 0.5 and self.consecutive_failures < 5:
            return "degraded"
        return "unhealthy"

    def as_dict(self) -> dict[str, Any]:
        """Return a non-secret health snapshot."""
        return {
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "success_rate": round(self.success_rate, 6),
            "average_latency_ms": round(self.total_latency_ms / self.calls, 3)
            if self.calls
            else 0.0,
            "last_success_ts": self.last_success_ts,
            "last_failure_ts": self.last_failure_ts,
            "error_classes": dict(sorted(self.error_classes.items())),
            "status": self.status,
        }


class ProviderHealthTracker:
    """Thread-safe persistent provider health aggregator."""

    def __init__(self, root: PathLike) -> None:
        self.root = Path(root)
        self.path = require_contained(root, Path("_telemetry") / "provider-health.json")
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path.is_symlink():
            raise SecurityViolation("provider health must not be a symbolic link")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return {
            str(key): dict(item)
            for key, item in value.items()
            if isinstance(item, Mapping)
        }

    def _save(self, values: Mapping[str, Mapping[str, Any]]) -> None:
        try:
            if self.path.parent.is_symlink() or self.path.is_symlink():
                raise SecurityViolation(
                    "provider health path must not be a symbolic link"
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(
                        redact_secrets(values),
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
        except OSError as exc:
            raise SecurityViolation("provider health could not be persisted") from exc

    def record(
        self,
        provider: str,
        model: str = "",
        *,
        success: Optional[bool] = None,
        outcome: str = "",
        error: Any = None,
        latency_s: float = 0.0,
        timestamp: Optional[float] = None,
    ) -> dict[str, Any]:
        """Record one provider outcome and persist an aggregate snapshot."""
        provider_name = redact_text(provider).strip()
        if not provider_name:
            raise SecurityViolation("provider name is required")
        key = provider_name + "|" + redact_text(model)
        now = time.time() if timestamp is None else float(timestamp)
        if success is None:
            success = str(outcome).casefold() in {"success", "ok", "accepted"}
        with self._lock:
            values = self._load()
            raw = values.get(key, {})
            health = ProviderHealth(
                calls=_safe_int(raw.get("calls")),
                successes=_safe_int(raw.get("successes")),
                failures=_safe_int(raw.get("failures")),
                consecutive_failures=_safe_int(raw.get("consecutive_failures")),
                total_latency_ms=_safe_float(raw.get("total_latency_ms")),
                last_success_ts=_safe_float(raw.get("last_success_ts")),
                last_failure_ts=_safe_float(raw.get("last_failure_ts")),
                error_classes={
                    str(k): _safe_int(v)
                    for k, v in dict(raw.get("error_classes") or {}).items()
                },
            )
            health.calls += 1
            health.total_latency_ms += max(0.0, _safe_float(latency_s) * 1000.0)
            if success:
                health.successes += 1
                health.consecutive_failures = 0
                health.last_success_ts = now
            else:
                health.failures += 1
                health.consecutive_failures += 1
                health.last_failure_ts = now
                error_name = _error_class(error or outcome) or "provider_error"
                health.error_classes[error_name] = (
                    health.error_classes.get(error_name, 0) + 1
                )
            values[key] = health.as_dict()
            self._save(values)
            return {
                "provider": provider_name,
                "model": redact_text(model),
                **health.as_dict(),
            }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return all provider/model health snapshots."""
        with self._lock:
            return dict(sorted(self._load().items()))


def provider_health(root: Optional[PathLike] = None) -> dict[str, dict[str, Any]]:
    """Return the persisted provider health snapshot."""
    target = telemetry_dir(root)
    return ProviderHealthTracker(target).snapshot() if target is not None else {}


def health_snapshot(root: Optional[PathLike] = None) -> dict[str, dict[str, Any]]:
    """Alias for :func:`provider_health`."""
    return provider_health(root)


def record_provider_success(
    provider: str,
    model: str = "",
    *,
    latency_s: float = 0.0,
    root: Optional[PathLike] = None,
) -> dict[str, Any]:
    """Record a successful provider call."""
    target = telemetry_dir(root)
    if target is None:
        return {}
    return ProviderHealthTracker(target).record(
        provider, model, success=True, latency_s=latency_s
    )


def record_provider_failure(
    provider: str,
    model: str = "",
    error: Any = None,
    *,
    latency_s: float = 0.0,
    root: Optional[PathLike] = None,
) -> dict[str, Any]:
    """Record a failed provider call without persisting the raw error."""
    target = telemetry_dir(root)
    if target is None:
        return {}
    return ProviderHealthTracker(target).record(
        provider, model, success=False, error=error, latency_s=latency_s
    )
