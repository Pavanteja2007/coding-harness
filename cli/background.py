"""Detach / attach / watch: background runs over the event journal
(VEX-CEILING-10, part 4).

A run that is alive must not be hostage to the window it was started in.
The three surfaces are thin and share ONE authority — the run's own
``trace.jsonl`` plus a small ``background.json`` control record next to
it:

``/detach``
    Write the control record and hand the run's lifetime to its worker
    thread. The projection stops; the run does not. No cancellation, no
    process kill, no partial state written.

``neo watch <task-id>``
    A headless follower. It replays the journal from the beginning,
    renders the same phase/live-text projection the TUI renders, and
    exits when the run reaches a terminal event. It is the surface that
    survives the TUI dying.

``/attach``
    Rebind the projection to a detached run, replaying the journal it
    missed so the user sees no gap. The replay is a real replay of real
    rows, not a summary: the attached view's event count equals the
    journal's.

Invariants this module holds:

* **Process death resumes from the journal.** Every surface reconstructs
  its state by folding rows; nothing is held only in memory. A follower
  started against a finished run reconstructs the finished run's facts
  (including the fail-closed terminal status) without a live process.
* **The journal is the authority.** ``background.json`` is a *control*
  record (who detached, when, which pid) and never a source of run
  status. A corrupted or absent control record degrades to "no
  background session", never to a wrong one.
* **Honest status.** The terminal status comes from the journal's own
  result/task_end rows through ``cli.runview``'s fail-closed projection.
  ``completed_unverified`` is never reported as verified.
* **No secret in the control record.** Only ids, modes, pids, and
  timestamps are written.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cli import streamview

#: The control record's name, next to the run's own artifacts.
CONTROL_FILE = "background.json"

#: Version of the control record. A future shape change is a refusal, not
#: a best-effort read of fields this version does not define.
CONTROL_VERSION = 1

#: How long a detached run's heartbeat may go unrefreshed before `watch`
#: calls the run *unresponsive* rather than *running*. A stale heartbeat
#: with no terminal event is the only honest evidence available; claiming
#: "running" forever would be a lie a user cannot act on.
STALE_HEARTBEAT_S = 120.0

#: How many polls `watch` will make waiting for a journal that has not
#: appeared yet. This exists so a caller whose clock is not advancing
#: (a test double, a frozen monotonic source) cannot spin forever: the
#: wait is bounded by POLL COUNT as well as by the caller's timeout.
JOURNAL_WAIT_POLLS = 120


@dataclass
class BackgroundRecord:
    """The control record for one detached run.

    Deliberately minimal: this is not run state, it is "there is a run
    here that a UI was watching".
    """

    task_id: str
    mode: str = "fix"
    detached_at: float = 0.0
    pid: int = 0
    version: int = CONTROL_VERSION
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["BackgroundRecord"]:
        """Parse a control record, returning ``None`` when unusable.

        A wrong version, a missing id, or a non-mapping payload all return
        ``None``: an unreadable control record must not be guessed at.
        """
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != CONTROL_VERSION:
            return None
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            return None
        try:
            detached_at = float(payload.get("detached_at") or 0.0)
        except (TypeError, ValueError):
            detached_at = 0.0
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        return cls(
            task_id=task_id,
            mode=str(payload.get("mode") or "fix"),
            detached_at=detached_at,
            pid=pid,
            note=str(payload.get("note") or ""),
        )


def task_dir(log_root: Path, task_id: str) -> Optional[Path]:
    """Resolve a run's directory through the shared traversal guard.

    A task id is untrusted input from a slash command; it must go through
    ``memory.paths.safe_task_dir`` like every other consumer, never
    straight into a path join.
    """
    try:
        from cli import interactive as _iv

        resolved = _iv._safe_task_dir(task_id, Path(log_root))
    except Exception:
        resolved = None
    if resolved is None:
        return None
    return Path(resolved)


def write_control(log_root: Path, record: BackgroundRecord) -> Optional[Path]:
    """Persist the control record atomically. Returns the path, or None."""
    directory = task_dir(log_root, record.task_id)
    if directory is None:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / CONTROL_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def read_control(log_root: Path, task_id: str) -> Optional[BackgroundRecord]:
    """Read the control record, or ``None`` when absent/unusable."""
    directory = task_dir(log_root, task_id)
    if directory is None:
        return None
    path = directory / CONTROL_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return BackgroundRecord.from_dict(payload)


def clear_control(log_root: Path, task_id: str) -> bool:
    """Remove the control record. Returns whether a file was removed."""
    directory = task_dir(log_root, task_id)
    if directory is None:
        return False
    try:
        (directory / CONTROL_FILE).unlink()
        return True
    except OSError:
        return False


def list_detached(log_root: Path, limit: int = 50) -> List[BackgroundRecord]:
    """List detached runs, newest first, tolerating unreadable roots."""
    root = Path(log_root)
    if not root.is_dir():
        return []
    found: List[BackgroundRecord] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        record = read_control(root, child.name)
        if record is not None:
            found.append(record)
    found.sort(key=lambda item: item.detached_at, reverse=True)
    return found[: max(1, int(limit))]


def detach(
    log_root: Path, task_id: str, mode: str = "fix", note: str = ""
) -> Optional[Path]:
    """Leave a run alive: write the control record and return its path.

    Returns ``None`` when the record could not be written — the caller
    must then say so honestly rather than claiming the run is detached.
    """
    return write_control(
        log_root,
        BackgroundRecord(
            task_id=str(task_id),
            mode=str(mode or "fix"),
            detached_at=time.time(),
            pid=os.getpid(),
            note=str(note or ""),
        ),
    )


def attach(log_root: Path, task_id: str) -> Dict[str, Any]:
    """Rebind to a detached run and replay its journal.

    The returned receipt is what the caller renders. It contains the
    replayed facts (``events``, ``phase``, ``status``) and, when the run
    cannot be attached, an explicit reason.
    """
    directory = task_dir(log_root, task_id)
    if directory is None:
        return {
            "attached": False,
            "task_id": str(task_id),
            "reason": "invalid task id (expected a single contained path segment)",
        }
    trace = directory / "trace.jsonl"
    if not trace.exists():
        return {
            "attached": False,
            "task_id": str(task_id),
            "reason": "no event journal for this run — it never started",
        }
    projection = replay(trace)
    projection["attached"] = True
    projection["task_id"] = str(task_id)
    projection["log_dir"] = str(directory)
    record = read_control(log_root, task_id)
    projection["was_detached"] = record is not None
    if record is not None:
        projection["detached_at"] = record.detached_at
        projection["detach_age_s"] = (
            max(0.0, time.time() - record.detached_at) if record.detached_at else None
        )
    return projection


def replay(
    trace: Path, on_event: Optional[Callable[[str, dict], None]] = None
) -> Dict[str, Any]:
    """Fold a whole ``trace.jsonl`` into the shared stream projection.

    This is the "no gaps" mechanism: attach, watch, and the TUI's
    reconnect path all go through this one function, so they cannot
    disagree about what happened. Reads only the journal; never calls a
    model, never executes a tool.
    """
    coalescer = streamview.StreamCoalescer(window_ms=streamview.MIN_WINDOW_MS)
    projector = streamview.PhaseProjector()
    events = 0
    status = ""
    model_calls = 0
    gaps = 0
    try:
        with Path(trace).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    gaps += 1
                    continue
                if not isinstance(row, dict):
                    gaps += 1
                    continue
                kind = str(row.get("kind") or row.get("event") or "")
                data = row.get("data")
                if not isinstance(data, dict):
                    data = (
                        row.get("payload")
                        if isinstance(row.get("payload"), dict)
                        else {}
                    )
                events += 1
                if on_event is not None:
                    try:
                        on_event(kind, data)
                    except Exception:
                        pass
                projector.consume(kind, data)
                if kind in ("model_delta", "response_delta", "text_delta"):
                    coalescer.push_delta(
                        str(
                            data.get("delta")
                            or data.get("text")
                            or data.get("content")
                            or ""
                        )
                    )
                elif kind in ("model_request",):
                    coalescer.reset()
                    model_calls += 1
                elif kind in ("task_end", "run_finished"):
                    status = str(data.get("status") or row.get("status") or "")
                elif kind == "result":
                    status = str(data.get("status") or "")
    except OSError:
        return {
            "events": 0,
            "status": "",
            "phase": streamview.RunPhase.IDLE.value,
            "live_text": "",
            "gaps": 0,
            "model_calls": 0,
            "attached": False,
            "reason": "event journal could not be read",
        }
    phase = projector.state()
    return {
        "events": events,
        "status": status,
        "phase": phase.phase.value,
        "phase_label": phase.label(),
        "live_text": coalescer.live_text(),
        "gaps": gaps,
        "model_calls": model_calls,
        "frame_receipt": coalescer.frame_cost_receipt(),
    }


def watch(
    log_root: Path,
    task_id: str,
    *,
    interval_s: float = 0.25,
    timeout_s: float = 0.0,
    on_frame: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_frames: int = 0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Follow a run's journal until it reaches a terminal event.

    ``on_frame`` receives the same projection dictionary the TUI renders,
    so a follower and a TUI cannot drift. ``max_frames`` exists so a test
    and a CI lane can bound the loop; ``timeout_s=0`` means "no wall-clock
    bound", which is the interactive default. Returns a final receipt that
    names the terminal status and whether it was seen in the journal.
    """
    directory = task_dir(log_root, task_id)
    if directory is None:
        return {
            "watched": False,
            "task_id": str(task_id),
            "reason": "invalid task id (expected a single contained path segment)",
        }
    trace = directory / "trace.jsonl"
    if not trace.exists():
        # Nothing to follow yet, but the run may be starting: wait for the
        # journal rather than refusing. Bounded by BOTH the caller's
        # timeout and JOURNAL_WAIT_POLLS, so a non-advancing clock cannot
        # turn this into an infinite wait.
        deadline = clock() + timeout_s if timeout_s > 0 else float("inf")
        polls = 0
        while not trace.exists() and clock() < deadline and polls < JOURNAL_WAIT_POLLS:
            sleep(min(0.25, max(0.01, interval_s)))
            polls += 1
        if not trace.exists():
            return {
                "watched": False,
                "task_id": str(task_id),
                "reason": "no event journal for this run — it never started",
                "waited_polls": polls,
            }
    state = _WatchState()
    started = clock()
    last_now = started
    stuck_polls = 0
    frames = 0
    while True:
        rows = _read_new(trace, state)
        for kind, data in rows:
            state.events += 1
            state.projector.consume(kind, data)
            if kind in ("model_delta", "response_delta", "text_delta"):
                state.coalescer.push_delta(
                    str(
                        data.get("delta")
                        or data.get("text")
                        or data.get("content")
                        or ""
                    )
                )
            elif kind == "model_request":
                state.coalescer.reset()
                state.model_calls += 1
            elif kind in ("task_end", "run_finished", "result"):
                state.status = str(data.get("status") or "")
                if kind in ("task_end", "run_finished") or str(
                    data.get("status") or ""
                ):
                    state.terminal_seen = True
        frame = state.snapshot()
        frames += 1
        if on_frame is not None:
            try:
                on_frame(frame)
            except Exception:
                pass
        if state.terminal_seen:
            return {
                "watched": True,
                "task_id": str(task_id),
                "frames": frames,
                "terminal_seen": True,
                "elapsed_s": round(clock() - started, 3),
                **frame,
            }
        if max_frames and frames >= max_frames:
            return {
                "watched": True,
                "task_id": str(task_id),
                "frames": frames,
                "terminal_seen": False,
                "reason": f"frame bound {max_frames} reached before a terminal event",
                "elapsed_s": round(clock() - started, 3),
                **frame,
            }
        if timeout_s > 0 and (clock() - started) >= timeout_s:
            return {
                "watched": True,
                "task_id": str(task_id),
                "frames": frames,
                "terminal_seen": False,
                "reason": f"watch timeout after {timeout_s}s without a terminal event",
                "elapsed_s": round(clock() - started, 3),
                **frame,
            }
        # A clock that never advances would make an unbounded follow loop
        # spin forever. Detect that and say so, rather than hanging: the
        # caller's clock is a fact this function is entitled to check.
        now = clock()
        stuck_polls = stuck_polls + 1 if now <= last_now else 0
        last_now = now
        if stuck_polls >= JOURNAL_WAIT_POLLS:
            return {
                "watched": True,
                "task_id": str(task_id),
                "frames": frames,
                "terminal_seen": False,
                "reason": "the supplied clock did not advance; watch stopped",
                "elapsed_s": round(now - started, 3),
                **frame,
            }
        sleep(min(0.25, max(0.01, interval_s)))


@dataclass
class _WatchState:
    """Incremental journal follower state (bytes, not lines)."""

    offset: int = 0
    carry: bytes = b""
    events: int = 0
    model_calls: int = 0
    status: str = ""
    terminal_seen: bool = False
    coalescer: streamview.StreamCoalescer = field(
        default_factory=lambda: streamview.StreamCoalescer(
            window_ms=streamview.MIN_WINDOW_MS
        )
    )
    projector: streamview.PhaseProjector = field(
        default_factory=streamview.PhaseProjector
    )

    def snapshot(self) -> Dict[str, Any]:
        phase = self.projector.state()
        return {
            "events": self.events,
            "status": self.status,
            "phase": phase.phase.value,
            "phase_label": phase.label(),
            "live_text": self.coalescer.live_text(),
            "model_calls": self.model_calls,
        }


def _read_new(trace: Path, state: "_WatchState") -> List[tuple[str, Dict[str, Any]]]:
    """Read only the rows appended since the last call.

    Incremental by byte offset with a carry buffer, so a half-written
    final line is retried rather than parsed as a truncated row — the
    same discipline the TUI's tailer uses. A journal that shrank (rotated)
    is re-read from the start, which is the honest response.
    """
    rows: List[tuple[str, Dict[str, Any]]] = []
    try:
        size = trace.stat().st_size
        if size < state.offset:
            state.offset = 0
            state.carry = b""
        with trace.open("rb") as handle:
            handle.seek(state.offset)
            chunk = handle.read()
            state.offset = handle.tell()
    except OSError:
        return rows
    if not chunk:
        return rows
    state.carry += chunk
    lines = state.carry.split(b"\n")
    state.carry = lines.pop() if lines else b""
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        try:
            row = json.loads(text.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or row.get("event") or "")
        data = row.get("data")
        if not isinstance(data, dict):
            data = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        rows.append((kind, data))
    return rows


def heartbeat_age_s(log_root: Path, task_id: str) -> Optional[float]:
    """Seconds since the run's ``state.json`` was last written, or None.

    ``None`` means the run has no state file — an honest "unknown", which
    the caller renders as unknown rather than as healthy.
    """
    directory = task_dir(log_root, task_id)
    if directory is None:
        return None
    path = directory / "state.json"
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def describe(log_root: Path, task_id: str) -> Dict[str, Any]:
    """One receipt describing a background run's liveness and status.

    Used by both ``watch`` and the TUI's detach banner so the two cannot
    disagree about whether a run is still alive.
    """
    projection = attach(log_root, task_id)
    age = heartbeat_age_s(log_root, task_id)
    record = read_control(log_root, task_id)
    status = str(projection.get("status") or "")
    terminal = bool(projection.get("phase") in ("done", "failed"))
    if terminal:
        liveness = "finished"
    elif age is None:
        liveness = "unknown"
    elif age >= STALE_HEARTBEAT_S:
        liveness = "unresponsive"
    else:
        liveness = "running"
    return {
        "task_id": str(task_id),
        "liveness": liveness,
        "status": status,
        "phase": projection.get("phase", ""),
        "phase_label": projection.get("phase_label", ""),
        "events": projection.get("events", 0),
        "heartbeat_age_s": None if age is None else round(age, 3),
        "detached": record is not None,
        "attached": projection.get("attached", False),
    }
