"""Offline / read-only mode: a durable no-egress contract, not a hint.

"Neo should work offline" is easy to claim and easy to fake. The falsifiable
version is: **while offline, no outbound provider request is attempted at
all.** This module makes that testable and enforceable:

1. :func:`offline_config` — is offline mode on? (Task.config key, then the
   ``NEO_OFFLINE`` environment variable.) One predicate, so the router and
   the CLI cannot disagree about whether the product is offline.
2. :class:`OfflineEgressBlocked` — the typed refusal raised before a request
   is built for a remote target. It is a distinct type because a caller must
   be able to tell "the network is off on purpose" from "the provider
   failed"; the first is a state, the second is an incident.
3. :class:`OfflineIndicator` — the explicit, serializable "we are offline"
   fact that goes into the trace, the ledger, and any status surface. A mode
   that leaves no artifact is indistinguishable from a mode that is off.
4. :class:`OfflineQueue` — a durable, append-only queue of work that could
   not run. This is the "queued work is not silently lost when connectivity
   returns" half. It is a real file, fsync'd per entry, keyed by
   ``entry_id`` for idempotent re-enqueue, and drained explicitly by
   :meth:`OfflineQueue.drain`. Nothing is dropped on close, on failure, or
   on a later restart: the queue is the record.
5. :func:`offline_read_only_config` — the Task.config fragment a read-only
   question mode needs. It is a *fragment*, not a mode: the harness's
   question path is Terminal 1's file, and this round does not pretend to
   have changed it (see the handoff).

Guarantees:

- Nothing here opens a socket. The only network-shaped call in the module is
  the one a CALLER makes to check connectivity, and that is delegated to an
  injected probe.
- The queue never blocks. Enqueue is an append; drain is caller-driven.
- Offline is DEFAULT OFF, and a refusal carries the target's identity
  (provider + endpoint fingerprint) so a user can see what would have been
  dialed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .local_models import classify_target, endpoint_fingerprint, is_local_endpoint
from .redaction import redact_provider_text

__all__ = [
    "OfflineEgressBlocked",
    "OfflineIndicator",
    "OfflineQueue",
    "QueueEntry",
    "answer_offline_question",
    "collect_queue_roots",
    "drain_summary",
    "emit_offline_receipt",
    "offline_config",
    "offline_indicator",
    "offline_read_only_config",
    "queued_total",
    "queued_work_indicator",
    "require_local",
]

_QUEUE_NAME = "offline_queue.jsonl"
_TRUTHY = ("1", "true", "yes", "on", "offline")
_FALSEY = ("0", "false", "no", "off", "")


class OfflineEgressBlocked(RuntimeError):
    """Raised when offline mode forbids a request to a remote provider.

    The message names the provider and an endpoint FINGERPRINT, never the
    URL: a base URL can embed credentials.
    """

    def __init__(
        self,
        provider: Optional[str],
        model: Optional[str] = None,
        api_base: Optional[str] = None,
        reason: str = "offline",
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_base_sha256 = endpoint_fingerprint(api_base)
        self.reason = reason
        super().__init__(
            f"offline mode ({reason}) refused a request to provider "
            f"{provider or 'default'!r} model {model or 'default'!r} "
            f"endpoint sha256={self.api_base_sha256 or 'none'}; "
            "no network request was made"
        )


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    return default


def offline_config(config: Optional[Mapping[str, Any]] = None) -> bool:
    """Return whether offline mode is active.

    Precedence: the ``offline`` Task.config key, then ``NEO_OFFLINE``, then
    False. ``read_only`` alone is NOT offline: a read-only run may still use
    a cloud provider, and treating the two as the same flag would silently
    change existing read-only callers' behavior.
    """
    if isinstance(config, Mapping) and "offline" in config:
        resolved = _coerce_bool(config.get("offline"), False)
        if resolved:
            return True
    return _coerce_bool(os.environ.get("NEO_OFFLINE"), False)


@dataclass(frozen=True)
class OfflineIndicator:
    """The explicit, serializable statement that egress is disabled.

    ``local_only`` says the mode is on; ``queued`` says there is work waiting
    for connectivity. Both are facts a status surface can render without
    guessing, and both appear in the trace and the ledger.
    """

    offline: bool
    reason: str = "config"
    local_only: bool = False
    queued: int = 0
    queue_path: Optional[str] = None
    allow_local_models: bool = True
    egress_allowed_hosts: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe indicator for a trace event / status payload."""
        return {
            "offline": self.offline,
            "reason": self.reason,
            "local_only": self.local_only,
            "queued": self.queued,
            "queue_path": self.queue_path,
            "allow_local_models": self.allow_local_models,
            "egress_allowed_hosts": list(self.egress_allowed_hosts),
        }

    def label(self) -> str:
        """Return a one-token status label for compact renderers."""
        if not self.offline:
            return "online"
        return f"offline(queued={self.queued})" if self.queued else "offline"


def offline_indicator(
    config: Optional[Mapping[str, Any]] = None,
    queue_path: Any = None,
    *,
    queued: Optional[int] = None,
) -> OfflineIndicator:
    """Build the offline indicator for a run, including the queue depth."""
    offline = offline_config(config)
    queue = Path(queue_path) if queue_path else None
    count = queued
    if count is None and queue is not None:
        count = len(OfflineQueue(queue).pending())
    return OfflineIndicator(
        offline=offline,
        reason="config:offline"
        if isinstance(config, Mapping) and config.get("offline")
        else ("env:NEO_OFFLINE" if offline else "off"),
        local_only=offline,
        queued=int(count or 0),
        queue_path=str(queue) if queue is not None else None,
        allow_local_models=_coerce_bool(
            (config or {}).get("offline_allow_local_models", True)
            if isinstance(config, Mapping)
            else True,
            True,
        ),
    )


def require_local(
    provider: Optional[str],
    model: Optional[str] = None,
    api_base: Optional[str] = None,
    *,
    offline: Optional[bool] = None,
    config: Optional[Mapping[str, Any]] = None,
    reason: str = "offline",
) -> str:
    """Return the target's tier class, raising when egress is forbidden.

    A local endpoint is permitted in offline mode (that is the entire point of
    a local tier) unless the operator turned local models off with
    ``offline_allow_local_models=False``. A remote endpoint raises
    :class:`OfflineEgressBlocked` BEFORE any request is constructed.
    """
    active = offline_config(config) if offline is None else bool(offline)
    if not active:
        return classify_target(provider, api_base)
    if is_local_endpoint(provider, api_base):
        allowed = _coerce_bool(
            config.get("offline_allow_local_models", True)
            if isinstance(config, Mapping)
            else True,
            True,
        )
        if allowed:
            return "local"
        raise OfflineEgressBlocked(
            provider, model, api_base, reason=f"{reason}:local_models_disabled"
        )
    raise OfflineEgressBlocked(provider, model, api_base, reason=reason)


@dataclass
class QueueEntry:
    """One deferred unit of work.

    ``payload`` is the caller's data. It is stored verbatim because the queue
    is a local file inside the run's own log directory; a reader that wants
    the payload must have filesystem access to the run's logs, which is the
    same trust boundary as the run's own trace.
    """

    entry_id: str
    kind: str
    payload: Any
    created_at: float
    attempts: int = 0
    last_error: Optional[str] = None
    delivered_at: Optional[float] = None
    source: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe record (payload included; it is local-only)."""
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "delivered_at": self.delivered_at,
            "source": self.source,
        }

    @property
    def delivered(self) -> bool:
        """True once the entry has been acknowledged as delivered."""
        return self.delivered_at is not None


def _now() -> float:
    return time.time()


class OfflineQueue:
    """A durable, append-only deferral queue for one run.

    Layout: one JSONL file, one record per line, where each line is either an
    ``enqueue`` or a ``deliver``/``fail`` update keyed by ``entry_id``. The
    file is the authority and the in-memory view is derived from it on read,
    which is what makes the queue survive a hard kill: a process that dies
    after the append has already lost nothing.

    Concurrency: an ``O_APPEND`` write of a single line below the platform
    pipe-buffer size is atomic on POSIX, and a cross-process lock file
    serializes the read-modify-write cases on Windows. ``_locked`` is best
    effort and never fatal — a queue that cannot be locked still must not
    lose an entry, so a failure to lock degrades to the append only.

    Reads are tolerant by design: a torn final line (a process killed
    mid-append) is skipped and the rest of the queue survives. Losing the
    tail of a queue is recorded, not hidden: :meth:`pending` reports
    ``malformed`` so a caller can log it.
    """

    def __init__(self, path: Any, *, now: Optional[Callable[[], float]] = None) -> None:
        self.path = Path(path)
        self._now = now or _now
        self._lock = threading.RLock()

    # -- io ------------------------------------------------------------------

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, record: Mapping[str, Any]) -> None:
        self._ensure_parent()
        line = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:  # pragma: no cover - platform dependent
                pass

    def _read(self) -> Tuple[List[QueueEntry], int]:
        entries: Dict[str, QueueEntry] = {}
        order: List[str] = []
        malformed = 0
        if not self.path.is_file():
            return [], 0
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        malformed += 1
                        continue
                    if not isinstance(record, dict):
                        malformed += 1
                        continue
                    entry_id = str(record.get("entry_id") or "")
                    if not entry_id:
                        malformed += 1
                        continue
                    op = str(record.get("op") or "enqueue")
                    if op == "enqueue":
                        if entry_id in entries:
                            # Idempotent re-enqueue: keep the first arrival so
                            # a retry loop cannot inflate the queue depth.
                            entries[entry_id].attempts = max(
                                entries[entry_id].attempts,
                                int(record.get("attempts") or 0),
                            )
                            continue
                        entries[entry_id] = QueueEntry(
                            entry_id=entry_id,
                            kind=str(record.get("kind") or "work"),
                            payload=record.get("payload"),
                            created_at=float(record.get("created_at") or self._now()),
                            attempts=int(record.get("attempts") or 0),
                            last_error=record.get("last_error"),
                            source=record.get("source"),
                        )
                        order.append(entry_id)
                        continue
                    existing = entries.get(entry_id)
                    if existing is None:
                        # An update for an entry this reader never saw: keep
                        # the update as a synthetic delivered record so a
                        # partially-read queue cannot re-deliver.
                        entries[entry_id] = QueueEntry(
                            entry_id=entry_id,
                            kind=str(record.get("kind") or "work"),
                            payload=None,
                            created_at=float(record.get("created_at") or self._now()),
                            attempts=int(record.get("attempts") or 0),
                            delivered_at=(
                                float(record["delivered_at"])
                                if op == "deliver" and record.get("delivered_at")
                                else None
                            ),
                            last_error=record.get("last_error"),
                        )
                        order.append(entry_id)
                        continue
                    if op == "deliver":
                        existing.delivered_at = float(
                            record.get("delivered_at") or self._now()
                        )
                    elif op == "fail":
                        existing.attempts = int(
                            record.get("attempts") or existing.attempts + 1
                        )
                        existing.last_error = record.get("last_error")
        except OSError:
            return [], malformed
        return [entries[key] for key in order if key in entries], malformed

    # -- public surface ------------------------------------------------------

    def enqueue(
        self,
        kind: str,
        payload: Any = None,
        *,
        entry_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> QueueEntry:
        """Append one deferred work item and return the stored entry.

        Idempotent on ``entry_id``: re-enqueueing the same id updates the
        attempt count and does NOT add a second queue item, so a retry loop
        around an enqueue cannot quietly grow the queue.
        """
        with self._lock:
            identifier = str(entry_id or uuid.uuid4().hex)
            existing, _malformed = self._read()
            for entry in existing:
                if entry.entry_id == identifier and not entry.delivered:
                    entry.attempts += 1
                    self._append(
                        {
                            "op": "fail",
                            "entry_id": identifier,
                            "attempts": entry.attempts,
                            "last_error": "re-enqueued",
                        }
                    )
                    entry.last_error = "re-enqueued"
                    return entry
            entry = QueueEntry(
                entry_id=identifier,
                kind=str(kind or "work"),
                payload=payload,
                created_at=self._now(),
                source=source,
            )
            self._append(
                {
                    "op": "enqueue",
                    "entry_id": entry.entry_id,
                    "kind": entry.kind,
                    "payload": payload,
                    "created_at": entry.created_at,
                    "source": source,
                }
            )
            return entry

    def pending(self) -> List[QueueEntry]:
        """Return every entry that has not been delivered, in arrival order."""
        entries, _malformed = self._read()
        return [entry for entry in entries if not entry.delivered]

    def all_entries(self) -> List[QueueEntry]:
        """Return every entry, delivered or not, in arrival order."""
        entries, _malformed = self._read()
        return list(entries)

    def health(self) -> Dict[str, Any]:
        """Return queue counters, including any malformed lines.

        ``malformed`` is surfaced rather than swallowed: a queue that lost
        its tail must be visible to whoever claims connectivity returned.
        """
        entries, malformed = self._read()
        pending = [entry for entry in entries if not entry.delivered]
        return {
            "path": str(self.path),
            "exists": self.path.is_file(),
            "total": len(entries),
            "pending": len(pending),
            "delivered": len(entries) - len(pending),
            "malformed": malformed,
            "oldest_pending_created_at": pending[0].created_at if pending else None,
        }

    def drain(
        self,
        handler: Callable[[QueueEntry], Any],
        *,
        limit: Optional[int] = None,
        stop_on_error: bool = False,
    ) -> Dict[str, Any]:
        """Replay pending work through ``handler`` and mark what succeeded.

        A handler that raises marks the entry failed and CONTINUES by
        default, because one poisonous item must not strand the rest of the
        queue — and the entry stays pending, so nothing is lost either way.
        ``stop_on_error=True`` restores strict ordering for a handler that
        must not be re-entered (for example a provider that is still down).

        The caller supplies ``handler`` and therefore supplies the
        connectivity decision: this method runs the queue, it does not decide
        whether the network is back.
        """
        results: List[Dict[str, Any]] = []
        delivered = 0
        failed = 0
        for entry in self.pending():
            if limit is not None and delivered + failed >= max(0, int(limit)):
                break
            try:
                outcome = handler(entry)
            except Exception as exc:
                failed += 1
                with self._lock:
                    self._append(
                        {
                            "op": "fail",
                            "entry_id": entry.entry_id,
                            "attempts": entry.attempts + 1,
                            # Redacted at source: `offline_queue.jsonl` is
                            # appended with a raw `json.dumps` and no
                            # redaction at the writer, so an unredacted value
                            # here is durable. A drained entry's handler is a
                            # provider-facing path, which is the class of
                            # exception that echoes a credential.
                            "last_error": redact_provider_text(
                                f"{type(exc).__name__}: {exc}",
                                limit=200,
                                label="offline queue drain failure",
                            ),
                        }
                    )
                results.append(
                    {"entry_id": entry.entry_id, "ok": False, "outcome": None}
                )
                if stop_on_error:
                    break
                continue
            delivered += 1
            with self._lock:
                self._append(
                    {
                        "op": "deliver",
                        "entry_id": entry.entry_id,
                        "delivered_at": self._now(),
                    }
                )
            results.append({"entry_id": entry.entry_id, "ok": True, "outcome": outcome})
        return drain_summary(self, results=results, delivered=delivered, failed=failed)


def drain_summary(
    queue: OfflineQueue,
    *,
    results: Optional[List[Dict[str, Any]]] = None,
    delivered: int = 0,
    failed: int = 0,
) -> Dict[str, Any]:
    """Return the machine-readable receipt for a drain."""
    health = queue.health()
    return {
        "delivered": delivered,
        "failed": failed,
        "pending_after": health["pending"],
        "malformed": health["malformed"],
        "queue_path": health["path"],
        "results": list(results or []),
    }


def queued_work_indicator(queue: OfflineQueue) -> OfflineIndicator:
    """Return an offline indicator carrying the queue depth."""
    health = queue.health()
    return OfflineIndicator(
        offline=False,
        reason="queue",
        local_only=False,
        queued=health["pending"],
        queue_path=health["path"],
    )


def offline_read_only_config(
    local_profile: Optional[Mapping[str, Any]] = None, **overrides: Any
) -> Dict[str, Any]:
    """Return the Task.config fragment for a read-only offline question.

    This is a CONFIGURATION helper, deliberately not a mode: the harness's
    question path is owned elsewhere, and pretending to have switched it
    would be a claim this round cannot back. What it does guarantee is that
    a caller which adopts it gets, in one dict: offline on, a local model
    profile, and the privacy policy that forbids content leaving the host.
    """
    config: Dict[str, Any] = {
        "offline": True,
        "offline_allow_local_models": True,
        "read_only": True,
        "adaptive_routing": True,
        "privacy_policy": "local_only",
        "redact": True,
    }
    if isinstance(local_profile, Mapping) and local_profile:
        config["local_model_profile"] = dict(local_profile)
        config.setdefault("local_model", local_profile.get("model"))
        if local_profile.get("provider"):
            config.setdefault("local_provider", local_profile.get("provider"))
        if local_profile.get("api_base") or local_profile.get("base_url"):
            config.setdefault(
                "local_api_base",
                local_profile.get("api_base") or local_profile.get("base_url"),
            )
    for key, value in overrides.items():
        if value is not None:
            config[key] = value
    return config


def answer_offline_question(
    question: str,
    config: Optional[Mapping[str, Any]] = None,
    *,
    local_call: Optional[Callable[[List[Dict[str, str]]], Any]] = None,
    local_profile: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Answer a question with a LOCAL model and no egress, or refuse.

    This is the read-only offline question path as a *boundary*: it will not
    dial anything itself. The caller injects ``local_call`` (normally the
    router's ``call_model`` with a local-first config, which enforces the same
    rule again on the inside). What this function guarantees is the part a
    test can falsify without a network: a question asked with no local model
    configured is REFUSED, and a question asked with one is answered with
    the local model and an explicit ``egress: "denied"`` receipt.

    Raises ``OfflineEgressBlocked`` when no local model is configured, rather
    than silently falling back to a cloud provider — a silent fallback would
    be the exact failure mode offline mode exists to prevent.
    """
    from .privacy_policy import redact_messages, scrub_environment_secrets

    merged: Dict[str, Any] = dict(offline_read_only_config(local_profile))
    if isinstance(config, Mapping):
        merged.update({k: v for k, v in config.items() if v is not None})
    merged["offline"] = True
    if not merged.get("local_model_profile") and not merged.get("local_model"):
        raise OfflineEgressBlocked(
            None, None, None, reason="offline:no_local_model_for_question"
        )
    if not callable(local_call):
        raise OfflineEgressBlocked(
            merged.get("local_provider"),
            merged.get("local_model"),
            merged.get("local_api_base"),
            reason="offline:no_local_callable",
        )
    messages, receipt = redact_messages(
        [{"role": "user", "content": str(question or "")}],
        extra_secrets=scrub_environment_secrets(merged),
    )
    answer = local_call(messages)
    return {
        "question": str(question or "")[:2000],
        "answer": answer,
        "local_model": merged.get("local_model"),
        "local_provider": merged.get("local_provider"),
        "egress": "denied",
        "offline": True,
        "redaction": receipt,
        "read_only": True,
    }


def collect_queue_roots(log_root: Any) -> Iterable[Path]:
    """Yield the offline-queue paths under a run's log root.

    A convenience for a status surface that wants to report "N runs are
    holding deferred work" without knowing the layout.
    """
    root = Path(log_root)
    if not root.is_dir():
        return []
    return sorted(root.glob(f"*/{_QUEUE_NAME}"))


def queued_total(log_root: Any) -> Dict[str, Any]:
    """Return the aggregate deferred-work depth under a log root."""
    entries: List[Dict[str, Any]] = []
    pending = 0
    for path in collect_queue_roots(log_root):
        health = OfflineQueue(path).health()
        pending += health["pending"]
        if health["pending"]:
            entries.append(health)
    return {"pending": pending, "queues": entries}


def emit_offline_receipt(
    indicator: OfflineIndicator, task_id: Optional[str] = None
) -> Dict[str, Any]:
    """Emit the offline indicator to the unified trace and return it.

    Importing ``shared.tracing`` is deferred so this module stays importable
    (and testable) in a process with tracing disabled.
    """
    receipt = indicator.as_dict()
    if task_id:
        try:
            from shared import tracing

            tracing.emit("runtime", "offline_mode", task_id=task_id, **receipt)
        except Exception:  # pragma: no cover - tracing never raises
            pass
    return receipt
