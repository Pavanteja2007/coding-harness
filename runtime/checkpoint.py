"""Per-task resume bookkeeping for the scheduler (runtime-owned).

Key design: the runtime does NOT duplicate Terminal 1's per-step state
(state.json, Boundary 4). Instead the worker watches that file's
completed_steps, and persists its own small checkpoint describing where
run_task got to and what it produced. On resume, the worker passes
resume=True in task.config, and Terminal 1's harness is expected to read
its own state.json and skip completed steps (the resume contract; see
runtime/AGENTS.md "What Terminal 1 needs to provide").

Files, under logs/{task_id}/runtime/:
  checkpoint.json — the checkpoint itself (atomic, whole-file)
  heartbeat.json  — worker liveness, rewritten every ~5s while running
  events.jsonl    — append-only worker event journal (start, checkpoint,
                    resume, approval, finish; see worker.py)
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fsutil import append_jsonl, atomic_write_json, now_iso, now_epoch, read_json_or_none


class TaskCheckpoint:
    """Read/write the runtime-owned checkpoint for one task.

    Assumes one worker process per task at a time (scheduler guarantees
    this), writing under logs/{task_id}/runtime/.

    Checkpoint content:
      task_id            — id of the owning task
      attempt            — which attempt number this run is (0-based)
      started_at         — ISO ts of first start ever
      last_heartbeat     — ISO ts (informational; scheduler uses heartbeat.json)
      completed_steps    — steps reported complete at last checkpoint
                          (mirror of state.json's completed_steps)
      result             — last TaskResult dict (only if run_task returned)
      status             — "running" | "finished" | "killed" | "pending"
    """

    def __init__(self, runtime_dir: str) -> None:
        self.dir = Path(runtime_dir)
        self.path = self.dir / "checkpoint.json"
        self.heartbeat_path = self.dir / "heartbeat.json"
        self.events_path = self.dir / "events.jsonl"

    # -- lifecycle ------------------------------------------------------

    def load(self) -> Optional[Dict[str, Any]]:
        """Return the stored checkpoint dict, or None if none/corrupt."""
        data = read_json_or_none(self.path)
        return data if isinstance(data, dict) else None

    def save(self, cp: Dict[str, Any]) -> None:
        """Atomically persist the checkpoint dict for this task."""
        atomic_write_json(self.path, cp)

    def update(self, **fields: Any) -> Optional[Dict[str, Any]]:
        """Load-modify-save the checkpoint; returns the updated dict or
        None if no checkpoint exists yet."""
        cp = self.load()
        if cp is None:
            return None
        cp.update(fields)
        self.save(cp)
        return cp

    # -- heartbeat ------------------------------------------------------

    def beat(self, payload: Optional[Dict[str, Any]] = None) -> None:
        """Write a fresh heartbeat (worker calls this every few seconds).

        The scheduler kills a worker whose heartbeat is older than
        max_wallclock_s (hang detection) — heartbeat freshness distinguishes
        a slow-but-alive worker from a hung one.
        """
        hb = {
            "ts": now_iso(),
            "epoch": now_epoch(),
            **(payload or {}),
        }
        atomic_write_json(self.heartbeat_path, hb)

    def heartbeat_age_s(self) -> Optional[float]:
        """Seconds since the last heartbeat; None if no heartbeat file."""
        hb = read_json_or_none(self.heartbeat_path)
        if not isinstance(hb, dict) or "epoch" not in hb:
            start = read_json_or_none(self.dir / "start_epoch.json")
            if isinstance(start, dict) and "epoch" in start:
                return now_epoch() - float(start["epoch"])
            return None
        return now_epoch() - float(hb["epoch"])

    # -- events ---------------------------------------------------------

    def log_event(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Append an event to the task's worker journal (audit trail)."""
        append_jsonl(self.events_path, {"ts": now_iso(), "event": event, "data": data or {}})


# -- resume decision helpers (used by scheduler + worker) -----------------

def should_resume(config: Dict[str, Any], checkpoint: Optional[Dict[str, Any]]) -> bool:
    """Decide whether a task's run should resume rather than restart.

    Assumes config is the task's (defaults-applied) config and checkpoint
    is the loaded checkpoint dict (or None). A task resumes when resume is
    enabled AND a checkpoint exists AND it shows un-finished work (i.e. it
    was interrupted mid-run — status "running" with completed steps, or
    "killed").
    """
    if not config.get("resume", True):
        return False
    if checkpoint is None:
        return False
    status = checkpoint.get("status")
    if status in ("finished",):
        return False
    completed = checkpoint.get("completed_steps") or []
    return bool(completed) or status == "killed"
