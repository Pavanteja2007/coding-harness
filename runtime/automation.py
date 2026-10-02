"""Optional, disabled-by-default schedule and webhook ingress for workflows.

This module only validates and queues immutable workflow definitions. It never
starts workers, owns model credentials, or exposes a recursive spawn API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from runtime.fsutil import (
    append_jsonl,
    atomic_write_json,
    now_iso,
    read_json,
    read_json_or_none,
)
from runtime.orchestration import WorkflowSpec
from runtime.paths import validate_path_segment
from shared.security import contains_secret, redact_secrets


class AutomationError(RuntimeError):
    """Base error for automation ingress failures."""


class AutomationDisabled(AutomationError):
    """Raised when an explicitly disabled automation interface is used."""


class AutomationPayloadError(AutomationError):
    """Raised when an automation payload is unsafe or invalid."""


class IdempotencyConflict(AutomationError):
    """Raised when one idempotency key is reused for a different workflow."""


@dataclass(frozen=True)
class AutomationConfig:
    """Configuration for the optional automation adapter."""

    enabled: bool = False
    max_pending: int = 32
    webhook_token: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "max_pending", max(1, int(self.max_pending)))
        object.__setattr__(self, "webhook_token", str(self.webhook_token or ""))

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | bool | None) -> "AutomationConfig":
        """Build config from an explicit mapping or boolean toggle."""
        if isinstance(value, bool):
            return cls(enabled=value)
        data = dict(value or {})
        return cls(
            enabled=bool(
                data.get("enabled", data.get("orchestration_automation_enabled", False))
            ),
            max_pending=int(data.get("max_pending", 32)),
            webhook_token=str(data.get("webhook_token", "")),
        )


class AutomationIngress:
    """Queue schedules and authenticated webhook payloads without executing them."""

    def __init__(
        self,
        logs_root: str | Path,
        config: AutomationConfig | Mapping[str, Any] | bool | None = None,
    ) -> None:
        self.logs_root = Path(logs_root).expanduser().resolve()
        self.config = (
            config
            if isinstance(config, AutomationConfig)
            else AutomationConfig.from_value(config)
        )
        self.root = self.logs_root / "_automation"
        self.queue_dir = self.root / "queue"
        self.schedule_dir = self.root / "schedules"
        self.journal_path = self.root / "events.jsonl"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.schedule_dir.mkdir(parents=True, exist_ok=True)

    def submit(
        self,
        spec: WorkflowSpec | Mapping[str, Any],
        *,
        idempotency_key: str,
        source: str = "api",
    ) -> Dict[str, Any]:
        """Validate and enqueue one workflow definition idempotently."""
        self._require_enabled()
        key = str(idempotency_key or "").strip()
        if not key:
            raise AutomationPayloadError("idempotency_key is required")
        workflow = (
            spec if isinstance(spec, WorkflowSpec) else WorkflowSpec.from_dict(spec)
        )
        self._reject_secrets(workflow)
        digest = _digest(workflow.to_dict())
        filename = _key_filename(key)
        path = self.queue_dir / f"{filename}.json"
        existing = read_json_or_none(path)
        if isinstance(existing, dict):
            if existing.get("spec_digest") != digest:
                raise IdempotencyConflict(
                    "idempotency key is already bound to another workflow"
                )
            return dict(existing)
        pending = [item for item in self.queue_dir.glob("*.json") if item.is_file()]
        if len(pending) >= self.config.max_pending:
            raise AutomationPayloadError("automation queue is full")
        record = {
            "schema_version": 1,
            "queue_id": filename,
            "idempotency_key_hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "source": str(source or "api"),
            "status": "queued",
            "spec_digest": digest,
            "spec": redact_secrets(workflow.to_dict()),
            "queued_at": now_iso(),
        }
        atomic_write_json(path, record)
        self._event("queued", {"queue_id": filename, "source": record["source"]})
        return dict(record)

    def register_schedule(
        self,
        schedule_id: str,
        spec: WorkflowSpec | Mapping[str, Any],
        *,
        interval_s: float,
        start_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Register a periodic immutable workflow definition."""
        self._require_enabled()
        selected = validate_path_segment(str(schedule_id), "schedule id")
        interval = float(interval_s)
        if interval <= 0:
            raise AutomationPayloadError("schedule interval must be positive")
        workflow = (
            spec if isinstance(spec, WorkflowSpec) else WorkflowSpec.from_dict(spec)
        )
        self._reject_secrets(workflow)
        now = time.time()
        record = {
            "schema_version": 1,
            "schedule_id": selected,
            "interval_s": interval,
            "next_at": float(start_at if start_at is not None else now),
            "status": "active",
            "spec_digest": _digest(workflow.to_dict()),
            "spec": redact_secrets(workflow.to_dict()),
            "created_at": now_iso(),
        }
        atomic_write_json(self.schedule_dir / f"{selected}.json", record)
        self._event(
            "schedule_registered", {"schedule_id": selected, "interval_s": interval}
        )
        return dict(record)

    def due(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Return due schedule records and advance their next run time."""
        self._require_enabled()
        current = time.time() if now is None else float(now)
        due: List[Dict[str, Any]] = []
        for path in sorted(self.schedule_dir.glob("*.json")):
            record = read_json_or_none(path)
            if not isinstance(record, dict) or record.get("status") != "active":
                continue
            if float(record.get("next_at", 0.0)) > current:
                continue
            due.append(dict(record))
            record["next_at"] = current + float(record.get("interval_s", 1.0))
            record["last_due_at"] = current
            atomic_write_json(path, record)
        if due:
            self._event("schedule_due", {"count": len(due)})
        return due

    def accept_webhook(
        self,
        payload: WorkflowSpec | Mapping[str, Any],
        *,
        authorization: str,
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        """Authenticate and enqueue a webhook payload without spawning work."""
        self._require_enabled()
        expected = self.config.webhook_token
        if not expected:
            raise AutomationPayloadError("webhook token is not configured")
        supplied = str(authorization or "")
        prefix = "bearer "
        if not supplied.lower().startswith(prefix):
            raise AutomationPayloadError("webhook authorization is required")
        candidate = supplied[len(prefix) :].strip()
        if not hmac.compare_digest(candidate, expected):
            raise AutomationPayloadError("webhook authorization failed")
        data = payload.to_dict() if isinstance(payload, WorkflowSpec) else dict(payload)
        key = str(
            idempotency_key or data.get("idempotency_key") or data.get("event_id") or ""
        )
        if not key:
            digest = _digest(data)
            key = f"webhook-{digest[:24]}"
        return self.submit(data, idempotency_key=key, source="webhook")

    def pending(self, *, limit: int = 32) -> List[Dict[str, Any]]:
        """Return queued records for an external, bounded consumer."""
        self._require_enabled()
        count = max(1, int(limit))
        records: List[Dict[str, Any]] = []
        for path in sorted(self.queue_dir.glob("*.json")):
            value = read_json_or_none(path)
            if isinstance(value, dict) and value.get("status") == "queued":
                records.append(dict(value))
            if len(records) >= count:
                break
        return records

    def acknowledge(
        self, queue_id: str, *, success: bool, error: str = ""
    ) -> Dict[str, Any]:
        """Mark a queued record consumed or failed without executing tools."""
        self._require_enabled()
        selected = validate_path_segment(str(queue_id), "queue id")
        path = self.queue_dir / f"{selected}.json"
        record = read_json(path)
        if not isinstance(record, dict):
            raise AutomationPayloadError("queue record is not an object")
        record["status"] = "completed" if success else "failed"
        record["finished_at"] = now_iso()
        record["error"] = str(error or "")
        atomic_write_json(path, record)
        self._event(
            "queue_acknowledged",
            {"queue_id": selected, "status": record["status"]},
        )
        return dict(record)

    def _reject_secrets(self, spec: WorkflowSpec) -> None:
        if contains_secret(spec.to_dict()):
            raise AutomationPayloadError(
                "automation payloads must not contain credentials; use worker environment configuration"
            )

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise AutomationDisabled("orchestration automation is disabled by default")

    def _event(self, event: str, data: Mapping[str, Any]) -> None:
        append_jsonl(
            self.journal_path,
            {
                "schema_version": 1,
                "ts": now_iso(),
                "event": event,
                "data": dict(data),
            },
        )


def _key_filename(value: str) -> str:
    return "queue-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _digest(value: Any) -> str:
    payload = json.dumps(
        redact_secrets(value), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "AutomationConfig",
    "AutomationDisabled",
    "AutomationError",
    "AutomationIngress",
    "AutomationPayloadError",
    "IdempotencyConflict",
]
