"""Cross-module structured tracing (Observability round, Task A).

THE PROBLEM THIS MODULE SOLVES: a single task's lifecycle was previously
scattered across half a dozen files in three trees —

  logs/{task_id}/trace.jsonl         harness loop events (T1, kind-keyed,
                                     epoch ts)
  logs/{task_id}.runtime/events.jsonl   worker events (T3, event-keyed,
                                     ISO ts)
  logs/{task_id}.runtime/model_ledger.jsonl  routing decisions (T3, flat
                                     rows, ISO ts)
  logs/{run_id}/events.jsonl         scheduler run journal (T3, event-
                                     keyed, ISO ts)
  + harness state.json / git.json / rationale.md alongside the trace.

Reconstructing "what happened to task X" meant knowing all of these.
This module gives every module ONE append-only, normalized event stream
per task — same schema, same ts format, one directory:

  $NEO_TRACE_DIR (default logs/) _trace/{task_id}.jsonl

with records shaped

  {"ts": <epoch float>, "module": "runtime"|"execution"|"harness"|"mcp",
   "event": "<snake_case>", "task_id": "<id>", ...event fields}

DESIGN CONTRACTS (for every terminal emitting into it):
- NEVER RAISE: tracing is observability, not correctness — a tracing
  failure must never change a task's outcome. `emit()` swallows
  everything.
- OPT-IN via env: NEO_TRACE_DIR (a directory). Unset -> no-op, zero
  overhead beyond one env read per emit (cached). Tests stay quiet by
  default; a run that wants the unified stream sets the env var (the
  CLI/eval harness do this automatically).
- ONE FILE PER TASK (plus optional run-level files): the harness's own
  trace.jsonl stays THE authoritative full record (T1's contract,
  untouched); this stream is the cross-module OVERLAY that makes the
  scheduler/worker/router/sandbox/memory layers reconstructible next
  to it. Events here are compact (no full prompts) — the depth lives
  where it already lives.
- ts is time.time() (epoch, matches harness trace.jsonl so a merged
  view sorts consistently); every record also carries module + event +
  task_id.

How to turn it on:
  $env:NEO_TRACE_DIR = "logs"      # or any dir; created lazily
Then after a run: python -m shared.traceview <task_id>  (or the
reconstruct_task() API) renders the whole lifecycle chronologically,
merged with the harness's own trace.jsonl when present.

GenAI SPAN SEMANTICS (added for the measured-evidence round): the same
stream also carries OpenTelemetry-shaped GenAI spans. A span is a
start/end pair correlated by ``span_id`` and carrying run id, task id,
session id, and model on BOTH halves, so a whole run is reconstructible
from one file:

  with tracing.span("model", "chat.completions", task_id=tid, model=m) as s:
      s["attributes"] = {"genai.usage.input_tokens": 12}

Non-context callers (routers, sandboxes, verifiers) use the explicit
`emit_span_start` / `emit_span_end` pair instead. Spans are never
silently dropped: an unpaired start is reported with ``open=True`` by
`read_genai_spans`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .security import redact_secrets, redact_text, safe_path
from .security import safe_segment as _safe_segment

__all__ = [
    "GENAI_SPAN_EVENTS",
    "GENAI_SPAN_KINDS",
    "TRACE_ENV",
    "emit",
    "emit_run",
    "emit_span_end",
    "emit_span_start",
    "enabled",
    "list_traced_task_ids",
    "read_genai_spans",
    "read_run_events",
    "read_task_events",
    "safe_segment",
    "span",
    "span_lifecycle",
    "trace_dir",
    "trace_id_for",
]

TRACE_ENV = "NEO_TRACE_DIR"

#: The GenAI span kinds a run must be reconstructible from. Every kind is
#: a measured, non-inferred part of the run lifecycle.
GENAI_SPAN_KINDS: tuple = ("model", "tool", "retrieval", "verify", "routing", "cost")

#: Stream event name prefix per span kind (start + end per kind).
GENAI_SPAN_EVENTS: Dict[str, tuple] = {
    kind: (f"genai_{kind}_start", f"genai_{kind}_end") for kind in GENAI_SPAN_KINDS
}

_SPAN_MODULE = "genai"

_LOCK = threading.Lock()
_CACHED_DIR: Optional[str] = None  # "" means disabled (unset/blank)
_CACHED_STAMP = -1.0  # env snapshot time (os.environ identity can't be
#                    # hashed portably; a monotonic re-check interval
#                    # would be overkill — we cache per-process and
#                    # re-read only if never read. Tests that flip the
#                    # env mid-process call _reset_cache()).


def _resolve() -> Optional[Path]:
    """The active trace directory, or None when tracing is off.

    Reads NEO_TRACE_DIR once and caches ("" -> disabled). Assumes the
    env var stays stable for a process's lifetime — the documented
    contract; tests use _reset_cache().
    """
    global _CACHED_DIR, _CACHED_STAMP
    with _LOCK:
        if _CACHED_STAMP < 0:
            raw = (os.environ.get(TRACE_ENV) or "").strip()
            _CACHED_DIR = raw or None
            _CACHED_STAMP = time.monotonic()
        if not _CACHED_DIR:
            return None
        return Path(_CACHED_DIR)


def _reset_cache() -> None:
    """Test hook: forget the cached env read (so a flipped env is seen)."""
    global _CACHED_DIR, _CACHED_STAMP
    with _LOCK:
        _CACHED_DIR = None
        _CACHED_STAMP = -1.0


def _set_fallback_dir(path: Path) -> None:
    """Post-hoc reader fallback (traceview): resolve the stream root.

    Called when $NEO_TRACE_DIR is unset in THIS process — the process
    that ran the task set it, not the one reconstructing it later. The
    given directory is used as the trace root IF it exists (a wrong
    guess reads nothing; never raises, never mkdirs). Assumes `path` is
    the logs root the caller already located (its `_trace/` subdir is
    the stream root per the module's default layout).
    """
    global _CACHED_DIR, _CACHED_STAMP
    with _LOCK:
        if _CACHED_STAMP < 0:
            raw = (os.environ.get(TRACE_ENV) or "").strip()
            _CACHED_DIR = raw or None
            _CACHED_STAMP = time.monotonic()
        if _CACHED_DIR:
            return  # an explicit env var always wins over the fallback
        try:
            if path and Path(path).is_dir() and not Path(path).is_symlink():
                _CACHED_DIR = str(path)
        except (TypeError, ValueError, OSError):
            pass


def trace_dir() -> Optional[Path]:
    """Public read of the active trace root (None = tracing off).

    The directory is NOT created by this call (a read must not mkdir);
    emit() creates it lazily on first write.
    """
    return _resolve()


def enabled() -> bool:
    """True when the unified trace stream is active for this process."""
    return _resolve() is not None


def safe_segment(seg: str) -> bool:
    """True iff ``seg`` is a safe single trace-path segment.

    The shared security implementation adds credential-shaped rejection and
    is the single semantic source for every trace path.
    """
    return _safe_segment(seg)


def _task_path(task_id: str) -> Optional[Path]:
    root = _resolve()
    if root is None or not safe_segment(task_id):
        return None
    return safe_path(root, Path("_trace") / f"{task_id}.jsonl")


def _run_path(run_id: str) -> Optional[Path]:
    root = _resolve()
    if root is None or not safe_segment(run_id):
        return None
    return safe_path(root, Path("_trace") / f"_run-{run_id}.jsonl")


def _write(path: Path, record: Dict[str, Any]) -> None:
    """Append one record; create parents lazily; never raise."""
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def emit(
    module: str,
    event: str,
    task_id: str = "",
    run_id: str = "",
    **fields: Any,
) -> None:
    """Append one redacted normalized event to the unified trace stream.

    Unsafe task/run identities, symlinked destinations, and credential-shaped
    fields are refused or redacted. Observability remains best-effort and never
    raises into the caller: the whole body is guarded, so a failure anywhere in
    the write path (including a substituted writer) cannot change a task's
    outcome.
    """
    try:
        if not task_id and not run_id:
            return
        path = _task_path(task_id) if task_id else _run_path(run_id)
        if path is None:
            return
        record: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "module": redact_text(str(module)),
            "event": redact_text(str(event)),
        }
        if task_id:
            record["task_id"] = redact_text(task_id)
        if run_id:
            record["run_id"] = redact_text(run_id)
        fields.pop("ts", None)
        safe_fields = redact_secrets(fields)
        if not isinstance(safe_fields, dict):
            safe_fields = {"payload": safe_fields}
        for key, value in safe_fields.items():
            record[str(key)] = value
        _write(path, record)
        try:
            from .telemetry import observe_event

            observe_event(record, root=_resolve())
        except Exception:
            pass
    except Exception:
        pass


def emit_run(module: str, event: str, run_id: str, **fields: Any) -> None:
    """emit() for run-scoped events (scheduler journal overlay)."""
    emit(module, event, run_id=run_id, **fields)


# ---------------------------------------------------------------------------
# GenAI span semantics — OpenTelemetry-shaped, correlated, never silently lost
# ---------------------------------------------------------------------------

_SPAN_LOCK = threading.Lock()
_SPAN_SEQ = 0


def _next_span_id() -> str:
    """Process-unique 16-hex span id (OTel span-id width)."""
    global _SPAN_SEQ
    with _SPAN_LOCK:
        _SPAN_SEQ += 1
        seq = _SPAN_SEQ
    return hashlib.sha256(f"neo-genai-span:{seq}".encode("utf-8")).hexdigest()[:16]


def trace_id_for(task_id: str = "", run_id: str = "") -> str:
    """Deterministic 32-hex trace id shared by every span of one task/run.

    A task-scoped trace id keeps one task's spans in one trace even when
    the process also served other tasks; a run id is the fallback for
    run-scoped spans.
    """
    seed = f"task:{task_id}" if task_id else f"run:{run_id}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _span_kind(kind: Any) -> str:
    value = str(kind or "").strip().lower()
    return value if value in GENAI_SPAN_KINDS else "model"


def _span_identity(
    task_id: str, run_id: str, session_id: str, model: str, parent_span_id: str
) -> Dict[str, Any]:
    return {
        "task_id": str(task_id or ""),
        "run_id": str(run_id or ""),
        "session_id": str(session_id or ""),
        "model": str(model or ""),
        "parent_span_id": str(parent_span_id or ""),
    }


def emit_span_start(
    kind: str,
    name: str,
    *,
    task_id: str = "",
    run_id: str = "",
    session_id: str = "",
    model: str = "",
    parent_span_id: str = "",
    span_id: str = "",
    attributes: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> str:
    """Emit the start half of a GenAI span and return its ``span_id``.

    ``kind`` must be one of :data:`GENAI_SPAN_KINDS`; an unknown kind is
    normalized to ``model`` rather than dropped, so a caller bug cannot
    silently remove a span from the lifecycle. Correlation fields
    (task/run/session/model/parent) ride on both halves. Assumes the
    unified stream is enabled — a disabled stream is a documented no-op
    and yields an id that no reader will find.
    """
    span_kind = _span_kind(kind)
    span_id = str(span_id or _next_span_id())
    identity = _span_identity(task_id, run_id, session_id, model, parent_span_id)
    payload: Dict[str, Any] = dict(fields)
    if isinstance(attributes, dict):
        payload["attributes"] = dict(attributes)
    payload.update(
        {
            "span_id": span_id,
            "trace_id": trace_id_for(task_id, run_id),
            "genai_span_kind": span_kind,
            "genai_span_name": str(name or span_kind),
            "genai_span_status": "started",
        }
    )
    payload.update(identity)
    emit(
        _SPAN_MODULE,
        GENAI_SPAN_EVENTS[span_kind][0],
        task_id=payload.pop("task_id"),
        run_id=payload.pop("run_id"),
        **payload,
    )
    return span_id


def emit_span_end(
    kind: str,
    span_id: str,
    *,
    task_id: str = "",
    run_id: str = "",
    session_id: str = "",
    model: str = "",
    parent_span_id: str = "",
    status: str = "ok",
    duration_ms: Optional[float] = None,
    attributes: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> None:
    """Emit the end half of a GenAI span.

    ``status`` should be one of ``ok``/``error``. An unrecorded start is
    never repaired here: the reader reports the span as open.
    """
    span_kind = _span_kind(kind)
    identity = _span_identity(task_id, run_id, session_id, model, parent_span_id)
    payload: Dict[str, Any] = dict(fields)
    if isinstance(attributes, dict):
        payload["attributes"] = dict(attributes)
    if duration_ms is not None:
        try:
            payload["duration_ms"] = round(float(duration_ms), 3)
        except (TypeError, ValueError):
            pass
    payload.update(
        {
            "span_id": str(span_id or ""),
            "trace_id": trace_id_for(task_id, run_id),
            "genai_span_kind": span_kind,
            "genai_span_status": str(status or "ok"),
        }
    )
    payload.update(identity)
    emit(
        _SPAN_MODULE,
        GENAI_SPAN_EVENTS[span_kind][1],
        task_id=payload.pop("task_id"),
        run_id=payload.pop("run_id"),
        **payload,
    )


@contextlib.contextmanager
def span(
    kind: str,
    name: str,
    *,
    task_id: str = "",
    run_id: str = "",
    session_id: str = "",
    model: str = "",
    parent_span_id: str = "",
    attributes: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> Iterator[Dict[str, Any]]:
    """Context manager emitting one correlated GenAI span around a block.

    The yielded record is a plain dict the caller may set ``status``,
    ``attributes``, and ``duration_ms`` on; anything else is ignored. An
    exception is recorded as an ``error`` span carrying ``error_class``
    and then re-raised — the span layer never swallows a caller error,
    it only makes sure the failure is visible in the lifecycle.
    """
    span_id = emit_span_start(
        kind,
        name,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        model=model,
        parent_span_id=parent_span_id,
        attributes=attributes,
        **fields,
    )
    record: Dict[str, Any] = {"span_id": span_id, "kind": _span_kind(kind)}
    started = time.monotonic()
    try:
        yield record
    except BaseException as exc:
        error_attributes = dict(record.get("attributes") or {})
        error_attributes.setdefault("error_class", type(exc).__name__)
        emit_span_end(
            kind,
            span_id,
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            model=model,
            parent_span_id=parent_span_id,
            status="error",
            duration_ms=(time.monotonic() - started) * 1000.0,
            attributes=error_attributes,
        )
        raise
    emit_span_end(
        kind,
        span_id,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        model=model,
        parent_span_id=parent_span_id,
        status=str(record.get("status") or "ok"),
        duration_ms=record.get("duration_ms") or (time.monotonic() - started) * 1000.0,
        attributes=record.get("attributes"),
    )


def _span_rows(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair genai start/end rows out of a chronological unified stream."""
    starts: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    ends: Dict[str, Dict[str, Any]] = {}
    for event in events:
        name = str(event.get("event") or "")
        if not name.startswith("genai_"):
            continue
        span_id = str(event.get("span_id") or "")
        if not span_id:
            continue
        if name.endswith("_start"):
            if span_id not in starts:
                order.append(span_id)
            starts[span_id] = dict(event)
        elif name.endswith("_end"):
            ends[span_id] = dict(event)
    rows: List[Dict[str, Any]] = []
    for span_id in order:
        start = starts.get(span_id) or {}
        end = ends.get(span_id) or {}
        kind = str(
            end.get("genai_span_kind") or start.get("genai_span_kind") or "model"
        )
        attributes: Dict[str, Any] = {}
        for source in (start, end):
            candidate = source.get("attributes")
            if isinstance(candidate, dict):
                attributes.update(candidate)
        start_ts = start.get("ts")
        end_ts = end.get("ts")
        try:
            start_ts = float(start_ts) if start_ts is not None else None
        except (TypeError, ValueError):
            start_ts = None
        try:
            end_ts = float(end_ts) if end_ts is not None else None
        except (TypeError, ValueError):
            end_ts = None
        duration = end.get("duration_ms")
        if duration is None and start_ts is not None and end_ts is not None:
            duration = round((end_ts - start_ts) * 1000.0, 3)
        rows.append(
            {
                "span_id": span_id,
                "trace_id": end.get("trace_id") or start.get("trace_id") or "",
                "parent_span_id": end.get("parent_span_id")
                or start.get("parent_span_id")
                or "",
                "kind": kind,
                "name": start.get("genai_span_name") or kind,
                "task_id": start.get("task_id") or end.get("task_id") or "",
                "run_id": start.get("run_id") or end.get("run_id") or "",
                "session_id": end.get("session_id") or start.get("session_id") or "",
                "model": end.get("model") or start.get("model") or "",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "duration_ms": duration,
                "status": str(end.get("genai_span_status") or "open"),
                "open": not bool(end),
                "attributes": attributes,
            }
        )
    return rows


def read_genai_spans(task_id: str = "", run_id: str = "") -> List[Dict[str, Any]]:
    """Reconstruct the GenAI spans of one task (or run) from the stream.

    A start without an end is returned with ``open=True`` rather than
    dropped, so a crashed run is visibly incomplete instead of quietly
    short. Assumes the same stream root contract as the other readers.
    """
    if task_id:
        return _span_rows(read_task_events(task_id))
    if run_id:
        return _span_rows(read_run_events(run_id))
    return []


def span_lifecycle(spans: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Coverage + integrity summary for a reconstructed span set.

    Reports the observed kind histogram, how many spans are open, and
    whether correlation (trace id, task id, session id, model) is
    complete. ``reconstructable`` is true only when every required kind
    is present and nothing is open.
    """
    rows = [dict(span) for span in spans if isinstance(span, dict)]
    kinds: Dict[str, int] = {}
    for span in rows:
        key = str(span.get("kind") or "model")
        kinds[key] = kinds.get(key, 0) + 1
    open_spans = [span for span in rows if span.get("open")]
    missing_correlation = [
        span.get("span_id")
        for span in rows
        if not span.get("trace_id")
        or not (span.get("task_id") or span.get("run_id"))
        or not span.get("session_id")
        or not span.get("model")
    ]
    missing_kinds = [kind for kind in GENAI_SPAN_KINDS if not kinds.get(kind)]
    return {
        "n_spans": len(rows),
        "by_kind": kinds,
        "required_kinds": list(GENAI_SPAN_KINDS),
        "missing_kinds": missing_kinds,
        "open_span_count": len(open_spans),
        "open_span_ids": [span.get("span_id") for span in open_spans],
        "missing_correlation_count": len(missing_correlation),
        "missing_correlation_span_ids": missing_correlation,
        "trace_ids": sorted(
            {str(span.get("trace_id")) for span in rows if span.get("trace_id")}
        ),
        "reconstructable": bool(rows) and not missing_kinds and not open_spans,
    }


# ---------------------------------------------------------------------------
# Readers (traceview + tests)
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list:
    out: list = []
    try:
        if path.is_symlink() or not path.is_file():
            return out
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    out.append(value)
    except OSError:
        return []
    return out


def read_task_events(task_id: str) -> list:
    """All unified-stream events for one task, chronological.

    Assumes task_id names a traced task; a missing/empty stream -> []
    (never raises — a reader must not crash on a partially-written file).
    """
    path = _task_path(task_id)
    if path is None:
        return []
    events = _read_jsonl(path)
    events.sort(key=lambda e: e.get("ts", 0))
    return events


def read_run_events(run_id: str) -> list:
    """All unified-stream events for one scheduler run, chronological."""
    path = _run_path(run_id)
    if path is None:
        return []
    events = _read_jsonl(path)
    events.sort(key=lambda e: e.get("ts", 0))
    return events


def list_traced_task_ids() -> list:
    """Every task id that has a unified trace stream on disk, sorted."""
    root = _resolve()
    if root is None:
        return []
    d = safe_path(root, "_trace", directory=True)
    if d is None:
        return []
    try:
        values = []
        for path in d.glob("*.jsonl"):
            if path.name.startswith("_run-") or path.is_symlink():
                continue
            if safe_path(root, Path("_trace") / path.name) is None:
                continue
            values.append(path.stem)
        return sorted(values)
    except OSError:
        return []
