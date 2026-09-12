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

  $VEX_TRACE_DIR (default logs/) _trace/{task_id}.jsonl

with records shaped

  {"ts": <epoch float>, "module": "runtime"|"execution"|"harness"|"mcp",
   "event": "<snake_case>", "task_id": "<id>", ...event fields}

DESIGN CONTRACTS (for every terminal emitting into it):
- NEVER RAISE: tracing is observability, not correctness — a tracing
  failure must never change a task's outcome. `emit()` swallows
  everything.
- OPT-IN via env: VEX_TRACE_DIR (a directory). Unset -> no-op, zero
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
  $env:VEX_TRACE_DIR = "logs"      # or any dir; created lazily
Then after a run: python -m shared.traceview <task_id>  (or the
reconstruct_task() API) renders the whole lifecycle chronologically,
merged with the harness's own trace.jsonl when present.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "TRACE_ENV",
    "emit",
    "emit_run",
    "enabled",
    "list_traced_task_ids",
    "read_run_events",
    "read_task_events",
    "safe_segment",
    "trace_dir",
]

TRACE_ENV = "VEX_TRACE_DIR"

_LOCK = threading.Lock()
_CACHED_DIR: Optional[str] = None  # "" means disabled (unset/blank)
_CACHED_STAMP = -1.0  # env snapshot time (os.environ identity can't be
#                    # hashed portably; a monotonic re-check interval
#                    # would be overkill — we cache per-process and
#                    # re-read only if never read. Tests that flip the
#                    # env mid-process call _reset_cache()).


def _resolve() -> Optional[Path]:
    """The active trace directory, or None when tracing is off.

    Reads VEX_TRACE_DIR once and caches ("" -> disabled). Assumes the
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

    Called when $VEX_TRACE_DIR is unset in THIS process — the process
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
            if path and Path(path).is_dir():
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
    """True iff `seg` is safe to join onto the trace root as ONE path
    segment — deliberately the SAME semantic contract as
    memory.paths.is_safe_task_id (Win32 hazards: separators, drive
    forms via ':', null bytes, edge whitespace, trailing dot/space
    aliases — 'x.' normalizes AS 'x' under Win32 so two ids must never
    alias one stream). One shared semantic, implemented locally so
    shared/ never imports from memory/ (dependency direction: shared
    is the bottom layer every module may import; it imports none of
    them)."""
    if not isinstance(seg, str) or not seg:
        return False
    if any(c in seg for c in '/\\:*?"<>|\x00'):
        return False
    if seg != seg.strip():
        return False
    if seg.rstrip(". ") != seg:  # trailing dots/spaces alias on Win32
        return False
    # 'x.', 'x..', 'x ' normalize AS 'x' on Win32 — two ids must never
    # alias one stream; the stripped form must also not be empty/./..
    return seg.rstrip(". ") not in ("", ".", "..")


def _task_path(task_id: str) -> Optional[Path]:
    root = _resolve()
    if root is None or not safe_segment(task_id):
        return None
    return root / "_trace" / f"{task_id}.jsonl"


def _run_path(run_id: str) -> Optional[Path]:
    root = _resolve()
    if root is None or not safe_segment(run_id):
        return None
    return root / "_trace" / f"_run-{run_id}.jsonl"


def _write(path: Path, record: Dict[str, Any]) -> None:
    """Append one record; create parents lazily; never raise."""
    try:
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
    """Append one normalized event to this task's unified trace stream.

    Assumes `module` is the emitting layer ("runtime", "execution",
    "harness", "mcp"), `event` a short snake_case discriminator, and
    task_id identifies the task whose lifecycle this event belongs to
    (run-level events with no task id: pass run_id instead — they land
    in the run file). No-op (cheap) when VEX_TRACE_DIR is unset. Never
    raises. Extra fields become top-level record fields (values must
    be JSON-serializable; non-serializable ones fall back to repr()).
    Reserved keys (ts/module/event/task_id/run_id) colliding through
    **fields raise TypeError at the CALL SITE (Python's own duplicate-
    argument behavior) — callers never legitimately pass them, and a
    loud failure there is unmissable in tests.
    """
    if not task_id and not run_id:
        return
    path = _task_path(task_id) if task_id else _run_path(run_id)
    if path is None:
        return
    record: Dict[str, Any] = {
        "ts": round(time.time(), 3),
        "module": str(module),
        "event": str(event),
    }
    if task_id:
        record["task_id"] = task_id
    if run_id:
        record["run_id"] = run_id
    fields.pop("ts", None)  # ts is reserved (never caller-supplied)
    for k, v in fields.items():
        try:
            json.dumps(v)
            record[k] = v
        except (TypeError, ValueError):
            record[k] = repr(v)
    _write(path, record)


def emit_run(module: str, event: str, run_id: str, **fields: Any) -> None:
    """emit() for run-scoped events (scheduler journal overlay)."""
    emit(module, event, run_id=run_id, **fields)


# ---------------------------------------------------------------------------
# Readers (traceview + tests)
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list:
    out: list = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
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
    d = root / "_trace"
    try:
        return sorted(
            p.stem for p in d.glob("*.jsonl") if not p.name.startswith("_run-")
        )
    except OSError:
        return []
