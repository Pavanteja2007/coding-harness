"""Deterministic public event replay, reconnect, and live iteration."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

from harness.agent_kernel import (
    ReplayError,
    ReplayProjection,
    RunEventJournal,
    replay_run,
)
from shared.security import redact_secrets

from .errors import EventReplayError, RunNotFoundError, TransportError
from .models import Event, EventEnvelope

__all__ = [
    "EventReplay",
    "EventStream",
    "Events",
    "projection_from_dict",
    "validated_event_rows",
    "validated_replay",
]


def _projection_dict(value: ReplayProjection | Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted projection mapping for local or remote callers."""
    data = value.to_dict() if isinstance(value, ReplayProjection) else dict(value or {})
    return dict(redact_secrets(data))


def _projection_from_dict(
    value: Mapping[str, Any], *, verify_digest: bool = True
) -> ReplayProjection:
    """Build and validate the public kernel projection type from a wire mapping."""
    data = dict(value or {})
    if "schema_version" in data and int(data.get("schema_version", -1)) != 1:
        raise EventReplayError("unsupported remote replay schema version")
    raw_events = data.get("events", [])
    if not isinstance(raw_events, list):
        raise EventReplayError("remote replay events must be an array")
    events: list[dict[str, Any]] = []
    for index, item in enumerate(raw_events, start=1):
        if not isinstance(item, Mapping):
            raise EventReplayError("remote replay contains a non-object event")
        event = dict(item)
        if int(event.get("sequence", 0)) != index:
            raise EventReplayError(
                f"remote replay has a non-contiguous event at sequence {index}"
            )
        events.append(event)
    if data.get("run_id") and any(
        str(event.get("run_id", "")) != str(data.get("run_id")) for event in events
    ):
        raise EventReplayError("remote replay run identity mismatch")
    if data.get("session_id") and any(
        str(event.get("session_id", "")) != str(data.get("session_id"))
        for event in events
    ):
        raise EventReplayError("remote replay session identity mismatch")
    digest = str(data.get("digest", ""))
    if digest and verify_digest:
        canonical = [
            {
                "sequence": int(event.get("sequence", 0)),
                "session_id": str(event.get("session_id", "")),
                "run_id": str(event.get("run_id", "")),
                "turn_id": str(event.get("turn_id", "")),
                "event": str(event.get("event", "")),
                "payload": dict(event.get("payload", {}) or {}),
            }
            for event in events
        ]
        encoded = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise EventReplayError("remote replay digest mismatch")
    return ReplayProjection(
        session_id=str(data.get("session_id", "")),
        run_id=str(data.get("run_id", "")),
        strategy=str(data.get("strategy", "")),
        request=str(data.get("request", "")),
        final_status=str(data.get("final_status", data.get("status", ""))),
        digest=digest,
        events=events,
        tool_calls=[
            dict(item)
            for item in data.get("tool_calls", [])
            if isinstance(item, Mapping)
        ],
        tool_results=[
            dict(item)
            for item in data.get("tool_results", [])
            if isinstance(item, Mapping)
        ],
        changed_files=[str(item) for item in data.get("changed_files", [])],
        checkpoints=[
            dict(item)
            for item in data.get("checkpoints", [])
            if isinstance(item, Mapping)
        ],
        verification_evidence=[
            dict(item)
            for item in data.get("verification_evidence", [])
            if isinstance(item, Mapping)
        ],
        result=dict(data.get("result", {}))
        if isinstance(data.get("result", {}), Mapping)
        else {},
    )


projection_from_dict = _projection_from_dict


def validated_replay(
    path: Path | str,
    *,
    run_id: str = "",
    session_id: str = "",
) -> ReplayProjection:
    """Validate a trace through the public kernel replay API and redact its projection."""
    source = Path(path)
    if not source.is_file():
        raise EventReplayError(f"event journal does not exist: {source}")
    try:
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except ValueError as exc:
                    raise EventReplayError(
                        f"malformed event row {line_number}"
                    ) from exc
                if not isinstance(raw, Mapping):
                    raise EventReplayError(f"non-object event row {line_number}")
                if "schema_version" not in raw:
                    raise EventReplayError(
                        f"event row {line_number} has no schema_version"
                    )
                if int(raw.get("schema_version", -1)) != 1:
                    raise EventReplayError(
                        f"unsupported event schema at row {line_number}"
                    )
        projection = replay_run(source)
    except EventReplayError:
        raise
    except (ReplayError, ValueError, TypeError, OSError) as exc:
        raise EventReplayError(str(exc)) from exc
    if run_id and projection.run_id != str(run_id):
        raise EventReplayError(
            f"event journal run identity mismatch: expected {run_id}, found {projection.run_id}"
        )
    if session_id and projection.session_id != str(session_id):
        raise EventReplayError(
            f"event journal session identity mismatch: expected {session_id}, found {projection.session_id}"
        )
    data = _projection_dict(projection)
    canonical = [
        {
            "sequence": int(event.get("sequence", 0)),
            "session_id": str(event.get("session_id", "")),
            "run_id": str(event.get("run_id", "")),
            "turn_id": str(event.get("turn_id", "")),
            "event": str(event.get("event", "")),
            "payload": dict(event.get("payload", {}) or {}),
        }
        for event in data.get("events", [])
        if isinstance(event, Mapping)
    ]
    data["digest"] = hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return _projection_from_dict(data, verify_digest=False)


def validated_event_rows(
    transport: Any,
    run_id: str,
    *,
    session_id: str = "",
) -> tuple[list[Event], ReplayProjection]:
    """Read and validate all event rows for a run through its public transport."""
    try:
        path = transport.trace_path(run_id)
    except RunNotFoundError:
        raise
    except Exception as exc:
        raise EventReplayError(str(exc)) from exc
    if isinstance(path, str):
        try:
            projection = transport.replay(run_id)
            raw_rows = transport.read_events(
                run_id, session_id=session_id, after_sequence=0
            )
        except RunNotFoundError:
            raise
        except Exception as exc:
            if isinstance(exc, EventReplayError):
                raise
            raise EventReplayError(str(exc)) from exc
        rows = [Event.from_dict(item.to_dict()) for item in raw_rows]
        for index, row in enumerate(rows, start=1):
            if row.sequence != index:
                raise EventReplayError(
                    f"non-contiguous event sequence {row.sequence}; expected {index}"
                )
            if row.run_id != str(run_id):
                raise EventReplayError("event journal run identity mismatch")
            if session_id and row.session_id != str(session_id):
                raise EventReplayError("event journal session identity mismatch")
        return rows, projection
    projection: ReplayProjection | None = None
    raw_events: list[Any] = []
    last_error: EventReplayError | None = None
    for attempt in range(5):
        try:
            projection = validated_replay(path, run_id=run_id, session_id=session_id)
            journal = RunEventJournal(path)
            raw_events = journal.read_events()
            if len(raw_events) == len(projection.events):
                break
            last_error = EventReplayError("event journal projection row count mismatch")
        except EventReplayError as exc:
            last_error = exc
        if attempt < 4:
            time.sleep(0.005)
    if projection is None:
        raise last_error or EventReplayError(
            "event journal projection row count mismatch"
        )
    if len(raw_events) != len(projection.events):
        try:
            terminal = bool(transport.is_terminal(run_id))
        except Exception:
            terminal = False
        if terminal:
            raise last_error or EventReplayError(
                "event journal projection row count mismatch"
            )
    rows: list[Event] = []
    for raw in raw_events:
        rows.append(Event.from_dict(raw.to_dict()))
    for index, row in enumerate(rows, start=1):
        if row.sequence != index:
            raise EventReplayError(
                f"non-contiguous event sequence {row.sequence}; expected {index}"
            )
    return rows, projection


class EventReplay(list):
    """List-like event backlog with its deterministic projection attached."""

    def __init__(self, events: list[Event], projection: ReplayProjection) -> None:
        """Attach a replay projection to a public list of events."""
        super().__init__(events)
        self.projection = projection

    @property
    def events(self) -> list[Event]:
        """Return the list view expected by replay consumers."""
        return list(self)

    @property
    def final_status(self) -> str:
        """Return the terminal status from the validated projection."""
        return self.projection.final_status

    @property
    def digest(self) -> str:
        """Return the deterministic projection digest."""
        return self.projection.digest

    def to_dict(self) -> dict[str, Any]:
        """Return a replay mapping while retaining list iteration semantics."""
        return {
            "events": [event.to_dict() for event in self],
            "projection": self.projection.to_dict(),
        }


class Events:
    """A replayable, reconnectable sequence of public events for one run."""

    def __init__(
        self,
        transport: Any,
        run_id: str,
        *,
        session_id: str = "",
        after_sequence: int = 0,
    ) -> None:
        """Create an event view; ``after_sequence`` is exclusive."""
        if isinstance(transport, str) and not isinstance(run_id, str):
            transport, run_id = run_id, transport
        if hasattr(transport, "transport") and not hasattr(transport, "trace_path"):
            transport = transport.transport
        self.transport = transport
        self.run_id = str(run_id or "")
        self.session_id = str(session_id or "")
        self.after_sequence = max(0, int(after_sequence or 0))
        self._cursor = self.after_sequence
        self._closed = False

    @property
    def id(self) -> str:
        """Return the run identifier associated with this stream."""
        return self.run_id

    @property
    def sequence(self) -> int:
        """Return the current consumer cursor."""
        return self._cursor

    @property
    def last_sequence(self) -> int:
        """Return the latest validated sequence currently available."""
        rows, _ = self._read()
        return rows[-1].sequence if rows else self._cursor

    def close(self) -> None:
        """Stop future local iteration; already yielded events remain valid."""
        self._closed = True

    def replay(self) -> EventReplay:
        """Return the complete validated backlog as public Event objects."""
        rows, projection = self._read()
        return EventReplay(rows, projection)

    history = replay

    def poll_after_sequence(self, sequence: int | None = None) -> list[Event]:
        """Return the currently available contiguous backlog after a sequence."""
        cursor = self.after_sequence if sequence is None else max(0, int(sequence))
        rows, _ = self._read(allow_missing=True)
        selected: list[Event] = []
        for row in rows:
            if row.sequence <= cursor:
                continue
            if row.sequence != cursor + 1:
                raise EventReplayError(
                    f"event gap before sequence {row.sequence}; expected {cursor + 1}"
                )
            cursor = row.sequence
            selected.append(row)
        self._cursor = max(self._cursor, cursor)
        return selected

    def projection(self) -> ReplayProjection:
        """Return the deterministic semantic replay projection."""
        _, projection = self._read()
        return projection

    def iter_after_sequence(
        self,
        after_sequence: int | None = None,
        *,
        sequence: int | None = None,
        timeout: float | None = None,
        wait: bool = False,
        poll_interval: float = 0.05,
    ) -> Iterator[Event]:
        """Yield contiguous events after a sequence with reconnect deduplication."""
        if self._closed:
            return
        cursor = self.after_sequence if sequence is None else max(0, int(sequence))
        if after_sequence is not None:
            cursor = max(0, int(after_sequence))
        started = time.monotonic()
        reconnecting_sse = getattr(self.transport, "reconnecting_sse_events", None)
        if wait and callable(reconnecting_sse):
            while True:
                try:
                    for row in reconnecting_sse(
                        self.run_id,
                        after_sequence=cursor,
                        timeout=timeout,
                    ):
                        if row.sequence <= cursor:
                            continue
                        if row.sequence != cursor + 1:
                            raise EventReplayError(
                                f"event gap before sequence {row.sequence}; expected {cursor + 1}"
                            )
                        cursor = row.sequence
                        self._cursor = max(self._cursor, cursor)
                        yield row
                    if self._terminal() and cursor >= self.last_sequence:
                        return
                except (TransportError, OSError):
                    if timeout is not None and time.monotonic() - started >= float(
                        timeout
                    ):
                        return
                if timeout is not None and time.monotonic() - started >= float(timeout):
                    return
                if self._closed:
                    return
                time.sleep(max(0.005, float(poll_interval)))
        while True:
            rows, projection = self._read(allow_missing=True)
            for row in rows:
                if row.sequence <= cursor:
                    continue
                if row.sequence != cursor + 1:
                    raise EventReplayError(
                        f"event gap before sequence {row.sequence}; expected {cursor + 1}"
                    )
                cursor = row.sequence
                self._cursor = max(self._cursor, cursor)
                yield row
            terminal = self._terminal()
            if not wait or (terminal and (not rows or cursor >= rows[-1].sequence)):
                return
            if timeout is not None and time.monotonic() - started >= float(timeout):
                return
            if self._closed:
                return
            time.sleep(max(0.005, float(poll_interval)))
            if projection.final_status and terminal and not rows:
                return

    iter_after = iter_after_sequence
    since = iter_after_sequence
    after = iter_after_sequence
    iter_events = iter_after_sequence

    def __iter__(self) -> Iterator[Event]:
        """Iterate over the live stream, waiting for terminal completion."""
        return self.iter_after_sequence(self.after_sequence, wait=True)

    def subscribe(self, sequence: int = 0) -> Iterator[Event]:
        """Return an iterator suitable for a simple event subscription."""
        return self.iter_after_sequence(sequence, wait=True)

    def stream(self, sequence: int = 0) -> Iterator[Event]:
        """Return a live iterator from an exclusive sequence cursor."""
        return self.iter_after_sequence(sequence, wait=True)

    def envelopes(self, sequence: int = 0) -> Iterator[EventEnvelope]:
        """Yield versioned envelopes around the event stream."""
        for event in self.iter_after_sequence(sequence, wait=False):
            yield EventEnvelope(event=event)

    def _read(
        self, *, allow_missing: bool = False
    ) -> tuple[list[Event], ReplayProjection]:
        try:
            return validated_event_rows(
                self.transport, self.run_id, session_id=self.session_id
            )
        except RunNotFoundError:
            if not allow_missing or not self._is_known():
                raise
            return [], _empty_projection(self.run_id, self.session_id)
        except EventReplayError as exc:
            if not allow_missing:
                raise
            try:
                path = self.transport.trace_path(self.run_id)
            except Exception:
                path = None
            if isinstance(path, Path) and path.exists():
                message = str(exc).casefold()
                if "empty" not in message and "does not exist" not in message:
                    raise
                if self._terminal():
                    raise
            if not self._is_known():
                raise
            return [], _empty_projection(self.run_id, self.session_id)

    def _is_known(self) -> bool:
        checker = getattr(self.transport, "has_run", None)
        if callable(checker):
            try:
                return bool(checker(self.run_id))
            except Exception:
                return False
        return True

    def _terminal(self) -> bool:
        checker = getattr(self.transport, "is_terminal", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(self.run_id))
        except Exception:
            return False


EventStream = Events


def _empty_projection(run_id: str, session_id: str) -> ReplayProjection:
    """Return an empty projection used only while a new journal is being created."""
    return ReplayProjection(
        session_id=session_id,
        run_id=run_id,
        strategy="",
        request="",
        final_status="",
        digest="",
    )
