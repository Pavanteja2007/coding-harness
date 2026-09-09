"""Structured per-task state file: logs/{task_id}/state.json.

Implements INTERFACES.md Boundary 4. The schema is EXACTLY:

{
  "task_id": str,
  "plan": [str, ...],
  "completed_steps": [str, ...],
  "files_touched": [str, ...],
  "decisions": [str, ...],
  "remaining_plan": [str, ...]
}

Terminal 4 (memory) ingests the `decisions` field across tasks — do not
change this schema without updating INTERFACES.md (with a Change Log entry).
The file is rewritten whole on every update (never append-only), so it
always reflects the current state, not a history.
"""
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Exact key order/shape of the schema in INTERFACES.md Boundary 4.
STATE_KEYS = (
    "task_id",
    "plan",
    "completed_steps",
    "files_touched",
    "decisions",
    "remaining_plan",
)

# Harness-internal (NOT Boundary 4): persisted planner steps + attempt/cost
# bookkeeping, so a relaunched run can reuse the same plan, continue the
# interrupted attempt, and keep the budget cap honest across the relaunch.
# Lives beside state.json in logs/{task_id}/.
PLAN_FILE = "plan.json"


def read_state(log_dir: Path) -> Optional[Dict[str, Any]]:
    """Read an existing logs/{task_id}/state.json (Boundary 4 schema).

    Returns the parsed dict, or None when the file is missing, unreadable,
    or not a valid state object. Callers treat None as "no prior progress"
    (fresh start). Assumes log_dir is the task's log directory.
    """
    path = Path(log_dir) / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("task_id") is None:
        return None
    return data


def read_plan_bookkeeping(log_dir: Path) -> Optional[Tuple[List[Dict[str, Any]], int, float]]:
    """Read plan.json as (steps, in_flight_attempt, cost_usd); None if absent/broken.

    Module-level (not a TaskState method) so the loop controller can gate
    the resume decision on it BEFORE creating any objects. Assumes log_dir
    is the task's log directory; a return of None means "not resumable
    via plan reuse" (fresh start, or a crash before the plan was saved).
    """
    try:
        data = json.loads((Path(log_dir) / PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    try:
        attempts = max(1, int(data.get("attempts", 1)))
    except (TypeError, ValueError):
        attempts = 1
    try:
        cost = max(0.0, float(data.get("cost_usd", 0.0)))
    except (TypeError, ValueError):
        cost = 0.0
    return steps, attempts, cost


class TaskState:
    """In-memory holder + atomic writer for the structured state file.

    Assumes one instance per task run, owned by the loop controller. Every
    mutating method rewrites state.json atomically (tmp file + replace) so a
    concurrent reader — e.g. the memory team's ingestion — never sees a
    partial file.
    """

    def __init__(self, log_dir: Path, task_id: str, resume: bool = False) -> None:
        self.log_dir = log_dir
        self.task_id = task_id
        self.plan: List[str] = []
        self.completed_steps: List[str] = []
        self.files_touched: List[str] = []
        self.decisions: List[str] = []
        self.remaining_plan: List[str] = []
        self._lock = threading.Lock()
        self._path = log_dir / "state.json"
        if resume:
            prior = read_state(log_dir)
            if prior is not None:
                # Hydrate from the pre-crash state file instead of wiping
                # it: the relaunch must CONTINUE, not restart.
                self.plan = [str(s) for s in prior.get("plan", [])]
                self.completed_steps = [str(s) for s in prior.get("completed_steps", [])]
                self.files_touched = [str(s) for s in prior.get("files_touched", [])]
                self.decisions = [str(s) for s in prior.get("decisions", [])]
                self.remaining_plan = [str(s) for s in prior.get("remaining_plan", [])]
        self._write()  # create/refresh the file with the hydrated state

    # -- mutations (all thread-safe; called from the loop controller) ----

    def set_plan(self, plan: List[str]) -> None:
        """Set the task's plan. remaining_plan is the plan minus steps
        already completed (non-empty only on resume — a fresh run starts
        with all steps remaining).
        """
        with self._lock:
            self.plan = list(plan)
            self.completed_steps = [s for s in self.completed_steps if s in self.plan]
            self.remaining_plan = [s for s in plan if s not in self.completed_steps]
            self._write()

    def reset_completed(self) -> None:
        """Move all completed steps back to remaining (used when a retry
        restores the working copy to pristine — the rolled-back steps no
        longer exist, so the state file must not claim they're done).
        """
        with self._lock:
            self.remaining_plan = list(self.plan)
            self.completed_steps = []
            self._write()

    def complete_step(self, step: str) -> None:
        """Mark a step as completed. Assumes `step` is one of self.plan's
        entries; it is removed from remaining_plan.
        """
        with self._lock:
            self.completed_steps.append(step)
            if step in self.remaining_plan:
                self.remaining_plan.remove(step)
            self._write()

    def complete_all_ran_steps(self, steps: List[str]) -> None:
        """Mark the given plan steps complete (used when the FINAL verify
        confirms the whole fix: every step that ran in the winning attempt
        had its work subsumed by the verified diff — including steps that
        ended without SUBMIT, e.g. exhausted turns after their edits were
        already applied). Assumes every entry is a self.plan entry;
        duplicates and out-of-plan strings are ignored.
        """
        with self._lock:
            changed = False
            for step in steps:
                if step in self.plan and step not in self.completed_steps:
                    self.completed_steps.append(step)
                    changed = True
            if changed:
                self.remaining_plan = [
                    s for s in self.plan if s not in self.completed_steps]
                self._write()

    def record_file_touched(self, path: str) -> None:
        """Record a file the agent modified (repo-relative, posix-style).
        Duplicate calls for the same path are idempotent.
        """
        norm = path.replace("\\", "/")
        with self._lock:
            if norm not in self.files_touched:
                self.files_touched.append(norm)
                self._write()

    def record_decision(self, text: str) -> None:
        """Record a decision worth remembering across tasks (Boundary 4's
        `decisions` field — Terminal 4 ingests these). Assumes `text` is a
        self-contained sentence, e.g. "chose full-file rewrite over diff".
        """
        with self._lock:
            self.decisions.append(text)
            self._write()

    # -- persistence ---------------------------------------------------

    def save_plan_steps(
        self,
        steps: List[Dict[str, Any]],
        attempts: int = 1,
        cost_usd: float = 0.0,
    ) -> None:
        """Persist planner steps + attempt/cost bookkeeping to plan.json.

        A relaunched run re-plans from scratch (the model may propose a
        different decomposition), which would orphan the completed-step
        descriptions already in state.json; and the in-flight attempt
        number + spend so far are needed to continue the interrupted
        attempt without double-counting either. Written on every attempt
        (re)start and after the plan is set. Assumes `steps` is the parsed
        plan (list of step dicts) and attempts >= 1.
        """
        with self._lock:
            (self.log_dir / PLAN_FILE).write_text(
                json.dumps({
                    "steps": steps,
                    "attempts": int(attempts),
                    "cost_usd": float(cost_usd),
                }, indent=2),
                encoding="utf-8")

    def _write(self) -> None:
        obj = {
            "task_id": self.task_id,
            "plan": self.plan,
            "completed_steps": self.completed_steps,
            "files_touched": self.files_touched,
            "decisions": self.decisions,
            "remaining_plan": self.remaining_plan,
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def as_dict(self) -> Dict[str, Any]:
        """Return the current state as a dict in the Boundary 4 schema."""
        return {
            "task_id": self.task_id,
            "plan": list(self.plan),
            "completed_steps": list(self.completed_steps),
            "files_touched": list(self.files_touched),
            "decisions": list(self.decisions),
            "remaining_plan": list(self.remaining_plan),
        }
