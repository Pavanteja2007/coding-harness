"""Append-only ordered event authority and deterministic replay."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from harness.redaction import redact_for_journal

from .contracts import SCHEMA_VERSION, RunEvent

Listener = Callable[[RunEvent], None]


class ReplayError(ValueError):
    """Raised when an event journal cannot be replayed deterministically."""


@dataclass
class ReplayProjection:
    """Semantic state reconstructed from one ordered event journal."""

    session_id: str
    run_id: str
    strategy: str
    request: str
    final_status: str
    digest: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)
    checkpoints: List[Dict[str, Any]] = field(default_factory=list)
    verification_evidence: List[Dict[str, Any]] = field(default_factory=list)
    result: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a deterministic JSON-compatible replay projection."""
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "strategy": self.strategy,
            "request": self.request,
            "final_status": self.final_status,
            "digest": self.digest,
            "events": list(self.events),
            "tool_calls": list(self.tool_calls),
            "tool_results": list(self.tool_results),
            "changed_files": list(self.changed_files),
            "checkpoints": list(self.checkpoints),
            "verification_evidence": list(self.verification_evidence),
            "result": dict(self.result),
        }


class RunEventJournal:
    """Persist one run's normalized events with compatibility aliases."""

    def __init__(
        self,
        path: Path | str,
        session_id: str = "",
        run_id: str = "",
        turn_id: str = "turn-1",
    ) -> None:
        self.path = Path(path)
        self.session_id = str(session_id or "")
        self.run_id = str(run_id or "")
        self.turn_id = str(turn_id or "")
        self._lock = threading.RLock()
        self._listeners: List[Listener] = []
        self._warnings: List[str] = []
        self._last_sequence = self._scan_last_sequence()

    @property
    def trace_path(self) -> Path:
        """Return the compatibility trace path, which is the same file."""
        return self.path

    @property
    def warnings(self) -> List[str]:
        """Return warnings encountered while loading the journal."""
        return list(self._warnings)

    @property
    def last_sequence(self) -> int:
        """Return the last durably allocated sequence number."""
        return self._last_sequence

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Subscribe to future events and return an unsubscribe callback."""
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    def append(
        self,
        event_type: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> RunEvent:
        """Durably append an event and notify subscribers after persistence.

        Every string in `payload` is capped at
        ``harness.redaction.JOURNAL_TEXT_CAP`` (200 000 chars) and redacted
        through ``harness.redaction.redact_for_journal`` BEFORE it is written.
        That is the one fail-closed boundary this journal uses: a tool result,
        a model response or an error string that reached this call cannot reach
        the file, the shared tracing overlay, or a subscriber listener without
        passing the authority first. If the redactor cannot answer, the row is
        still written and carries ``(detail withheld: ...)`` — losing the row
        would lose the evidence of the withholding.
        """
        with self._lock, _exclusive_file_lock(self.path):
            sequence = max(self._last_sequence, self._scan_last_sequence()) + 1
            event = RunEvent(
                sequence=sequence,
                timestamp=time.time() if timestamp is None else timestamp,
                session_id=session_id or self.session_id,
                run_id=run_id or self.run_id,
                turn_id=turn_id or self.turn_id,
                event_type=event_type,
                payload=redact_for_journal(
                    dict(payload or {}),
                    where=f"RunEventJournal.append({event_type})",
                ),
                schema_version=SCHEMA_VERSION,
            )
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            event.to_dict(),
                            ensure_ascii=False,
                            default=str,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                self._last_sequence = sequence
            except OSError as exc:
                message = f"event journal write failed: {exc}"
                self._warnings.append(message)
                raise OSError(message) from exc
            try:
                from shared.tracing import emit

                emit(
                    "harness",
                    event.event_type,
                    task_id=event.run_id or event.session_id,
                    run_id=event.run_id,
                    sequence=event.sequence,
                    session_id=event.session_id,
                    turn_id=event.turn_id,
                    payload=event.payload,
                )
            except Exception:
                pass
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(RunEvent.from_dict(event.to_dict()))
            except Exception as exc:
                self._warnings.append(f"event listener failed: {exc}")
        return event

    emit = append

    def read_events(self) -> List[RunEvent]:
        """Return valid events in file order with explicit corrupt-row warnings."""
        events, _ = self.load_events()
        return events

    def load_events(self) -> Tuple[List[RunEvent], List[str]]:
        """Return events and warnings for malformed rows."""
        events: List[RunEvent] = []
        warnings: List[str] = []
        try:
            handle = self.path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return events, warnings
        except OSError as exc:
            return events, [f"event journal unreadable: {exc}"]
        with handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    raw = json.loads(text)
                except ValueError:
                    warnings.append(f"malformed event row {line_number}")
                    continue
                if not isinstance(raw, dict):
                    warnings.append(f"non-object event row {line_number}")
                    continue
                try:
                    events.append(RunEvent.from_dict(raw))
                except (TypeError, ValueError) as exc:
                    warnings.append(f"invalid event row {line_number}: {exc}")
        with self._lock:
            for warning in warnings:
                if warning not in self._warnings:
                    self._warnings.append(warning)
            if events:
                self._last_sequence = max(self._last_sequence, events[-1].sequence)
        return events, warnings

    def events_since(self, sequence: int) -> List[RunEvent]:
        """Return events strictly after a sequence number."""
        return [event for event in self.read_events() if event.sequence > int(sequence)]

    def replay(self) -> ReplayProjection:
        """Reconstruct this journal without executing tools or calling a model."""
        return replay_run(self.path)

    def to_records(self) -> List[Dict[str, Any]]:
        """Return serialized event rows for dictionary-based consumers."""
        return [event.to_dict() for event in self.read_events()]

    def _scan_last_sequence(self) -> int:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                values: List[int] = []
                for line in handle:
                    try:
                        raw = json.loads(line)
                        value = (
                            int(raw.get("sequence", 0)) if isinstance(raw, dict) else 0
                        )
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if value > 0:
                        values.append(value)
                return max(values, default=0)
        except OSError:
            return 0


class JournalTraceLogger:
    """Compatibility logger that writes legacy trace calls into one journal."""

    def __init__(self, journal: RunEventJournal) -> None:
        self.journal = journal
        self.log_dir = journal.path.parent
        self.path = journal.path

    def log(self, kind: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Append one legacy-named trace event to the canonical journal."""
        self.journal.append(kind, data or {})

    def read_all(self) -> List[Dict[str, Any]]:
        """Return normalized rows with legacy ``kind`` and ``data`` fields."""
        return self.journal.to_records()

    def find_events(
        self,
        query: str,
        kinds: Optional[list] = None,
        limit: int = 5,
        max_chars: int = 4000,
    ) -> List[Dict[str, Any]]:
        """Provide TraceLogger's reversible-compaction retrieval surface."""
        needle = str(query or "").strip().lower()
        if not needle:
            return []
        per_event_cap = max(200, int(max_chars) // max(1, int(limit)))
        matches: List[Dict[str, Any]] = []
        for line_number, event in enumerate(self.read_all(), start=1):
            kind = str(event.get("kind", ""))
            if kinds is not None and kind not in kinds:
                continue
            haystack = (
                kind.lower()
                + " "
                + json.dumps(
                    event.get("data", {}), ensure_ascii=False, default=str
                ).lower()
            )
            if needle not in haystack:
                continue
            dumped = json.dumps(event.get("data", {}), ensure_ascii=False, default=str)
            if len(dumped) > per_event_cap:
                dumped = dumped[:per_event_cap] + "...[truncated]"
            matches.append({"line": line_number, "kind": kind, "data": dumped})
        return matches[-max(0, int(limit)) :]


def replay_run(path: Path | str) -> ReplayProjection:
    """Replay one journal into a deterministic semantic projection."""
    source = Path(path)
    raw_events: List[Dict[str, Any]] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ReplayError(f"event journal does not exist: {source}") from exc
    except OSError as exc:
        raise ReplayError(f"event journal is unreadable: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise ReplayError(f"malformed event row {line_number}") from exc
        if not isinstance(raw, dict):
            raise ReplayError(f"non-object event row {line_number}")
        try:
            event = RunEvent.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise ReplayError(f"invalid event row {line_number}: {exc}") from exc
        raw_events.append(event.to_dict())
    if not raw_events:
        raise ReplayError("event journal is empty")
    expected = 1
    session_id = ""
    run_id = ""
    for raw in raw_events:
        sequence = int(raw.get("sequence", 0))
        if sequence != expected:
            raise ReplayError(
                f"non-contiguous event sequence {sequence}; expected {expected}"
            )
        event_session = str(raw.get("session_id", ""))
        event_run = str(raw.get("run_id", ""))
        if expected == 1:
            session_id = event_session
            run_id = event_run
        if event_session != session_id or event_run != run_id:
            raise ReplayError("event journal identity changed mid-run")
        expected += 1
    canonical_events: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    changed_files: set[str] = set()
    checkpoints: List[Dict[str, Any]] = []
    verification: List[Dict[str, Any]] = []
    strategy = ""
    request = ""
    final_status = ""
    result: Dict[str, Any] = {}
    for raw in raw_events:
        event_type = str(raw.get("event", ""))
        payload = dict(raw.get("payload", {}) or {})
        canonical_events.append(
            {
                "sequence": int(raw["sequence"]),
                "session_id": str(raw.get("session_id", "")),
                "run_id": str(raw.get("run_id", "")),
                "turn_id": str(raw.get("turn_id", "")),
                "event": event_type,
                "payload": payload,
            }
        )
        if event_type in {"run_started", "strategy_selected"}:
            selected = str(payload.get("strategy", "") or "")
            if selected and not strategy:
                strategy = selected
            requested = str(payload.get("request", "") or "")
            if requested:
                request = requested
            run_spec = payload.get("run_spec")
            if isinstance(run_spec, Mapping):
                if not strategy:
                    strategy = str(run_spec.get("strategy", "") or "")
                request = str(run_spec.get("request", request) or request)
        elif event_type == "tool_call":
            tool_calls.append(dict(payload))
        elif event_type == "tool_result":
            tool_results.append(dict(payload))
        elif event_type in {"checkpoint_saved", "checkpoint"}:
            checkpoint = payload.get("checkpoint")
            if isinstance(checkpoint, Mapping):
                checkpoints.append(dict(checkpoint))
                changed_files.update(
                    str(item).replace("\\", "/")
                    for item in checkpoint.get("agent_owned_changes", [])
                )
        elif event_type in {"verification", "verify"}:
            verification.append(dict(payload))
        elif event_type == "run_finished":
            final_status = str(payload.get("status", final_status) or final_status)
            candidate = payload.get("result")
            if isinstance(candidate, Mapping):
                result = dict(candidate)
        for key in ("changed_files", "files_touched"):
            values = payload.get(key)
            if isinstance(values, list):
                changed_files.update(str(item).replace("\\", "/") for item in values)
        path = payload.get("path")
        if event_type in {"edit_applied", "file_changed"} and path:
            changed_files.add(str(path).replace("\\", "/"))
    if not strategy:
        strategy = "daily"
    if not final_status and result:
        final_status = str(result.get("status", ""))
    digest_input = json.dumps(
        canonical_events, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    return ReplayProjection(
        session_id=session_id,
        run_id=run_id,
        strategy=strategy,
        request=request,
        final_status=final_status,
        digest=digest,
        events=canonical_events,
        tool_calls=tool_calls,
        tool_results=tool_results,
        changed_files=sorted(changed_files),
        checkpoints=checkpoints,
        verification_evidence=verification,
        result=result,
    )


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 10.0
    descriptor: Optional[int] = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 30.0
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out acquiring event journal lock: {lock_path}"
                ) from None
            time.sleep(0.01)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii", errors="ignore"))
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except OSError:
            pass
