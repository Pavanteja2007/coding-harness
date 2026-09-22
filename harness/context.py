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

Round 2 (T1+T4 memory-informed planning): an ADDITIVE `repo_path` key is
written after the six Boundary 4 keys. Terminal 4's ingest_state_file
already read `data.get("repo_path")` from day one — the reader side of
this contract existed before the writer did. Consumers must treat extra
keys as ignorable per the Boundary 4 note in INTERFACES.md.

Improvement Round 2 (coordinated multi-file changes): a second ADDITIVE
key `change_groups` is written after `repo_path` when the task's plan
declares atomic file groups (planner steps carrying a shared
`change_group` name — see prompts.py). Value shape:

    "change_groups": {"<group-name>": ["file1.py", "file2.py", ...], ...}

Absent when no group was declared (single-file-fix runs are unchanged).
Consumers must treat it as ignorable exactly like repo_path; the
Boundary 4 six-key prefix order is untouched by both.
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

# The six Boundary 4 keys, plus the additive repo_path (see module
# docstring). Tests assert the six-key prefix order via STATE_KEYS.
STATE_KEYS_WITH_REPO = (*STATE_KEYS, "repo_path")

# Additive change_groups key (Improvement Round 2; see module docstring):
# declared atomic file groups from the plan, written after repo_path.
STATE_KEYS_WITH_GROUPS = (*STATE_KEYS_WITH_REPO, "change_groups")

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


def read_plan_bookkeeping(
    log_dir: Path,
) -> Optional[Tuple[List[Dict[str, Any]], int, float]]:
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

    def __init__(
        self, log_dir: Path, task_id: str, repo_path: str = "", resume: bool = False
    ) -> None:
        self.log_dir = log_dir
        self.task_id = task_id
        self.repo_path = repo_path or ""
        self.plan: List[str] = []
        self.completed_steps: List[str] = []
        self.files_touched: List[str] = []
        self.decisions: List[str] = []
        self.remaining_plan: List[str] = []
        # Additive (Improvement Round 2): declared atomic change groups
        # {name: [files]} from the plan; {} when the plan declares none.
        self.change_groups: Dict[str, List[str]] = {}
        # Additive (Modes round): the mode that produced this task
        # ("fix"|"build"|"question"|"research"); "" = unset (fix-mode
        # runs never set it — schema unchanged for them).
        self.mode: str = ""
        # Additive (steering round): the formal state machine's live
        # phase (state_machine.ALL_STATES name); "" = unset (the
        # TaskStateMachine on_transition hook writes it via set_phase).
        self.phase: str = ""
        self._lock = threading.Lock()
        self._path = log_dir / "state.json"
        if resume:
            prior = read_state(log_dir)
            if prior is not None:
                # Hydrate from the pre-crash state file instead of wiping
                # it: the relaunch must CONTINUE, not restart.
                self.plan = [str(s) for s in prior.get("plan", [])]
                self.completed_steps = [
                    str(s) for s in prior.get("completed_steps", [])
                ]
                self.files_touched = [str(s) for s in prior.get("files_touched", [])]
                self.decisions = [str(s) for s in prior.get("decisions", [])]
                self.remaining_plan = [str(s) for s in prior.get("remaining_plan", [])]
                self._hydrate_change_groups(prior)
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
                    s for s in self.plan if s not in self.completed_steps
                ]
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

    def clear_files_touched(self, paths: List[str]) -> None:
        """Remove the given files from files_touched (Improvement Round 2:
        a coordinated group that rolled back as an atomic unit no longer
        counts as touched work — state.json must not claim edits that no
        longer exist in work/). Assumes paths are repo-relative posix;
        absent paths are a no-op."""
        gone = {p.replace("\\", "/") for p in (paths or [])}
        with self._lock:
            if gone and any(f in self.files_touched for f in gone):
                self.files_touched = [f for f in self.files_touched if f not in gone]
                self._write()

    def record_decision(self, text: str) -> None:
        """Record a decision worth remembering across tasks (Boundary 4's
        `decisions` field — Terminal 4 ingests these). Assumes `text` is a
        self-contained sentence, e.g. "chose full-file rewrite over diff".
        """
        with self._lock:
            self.decisions.append(text)
            self._write()

    def set_change_groups(self, groups: Dict[str, List[str]]) -> None:
        """Record the plan's declared atomic change groups (Improvement
        Round 2, additive state key — see module docstring).

        Assumes groups maps group names to repo-relative posix file
        lists (as declared by planner steps sharing a `change_group`
        name). An empty dict clears the groups (e.g. a re-plan that
        declares none); writing is idempotent per name (last write for
        a name wins, matching how the plan supersedes itself).
        """
        norm: Dict[str, List[str]] = {}
        for name, files in (groups or {}).items():
            norm[str(name)] = sorted({str(f).replace("\\", "/") for f in (files or [])})
        with self._lock:
            self.change_groups = norm
            self._write()

    def _hydrate_change_groups(self, prior: Dict[str, Any]) -> None:
        """Best-effort restore of change_groups from a prior state file.

        Malformed values (non-dict, non-list members) degrade to empty
        — resume must never crash over an additive key's shape."""
        raw = prior.get("change_groups")
        if not isinstance(raw, dict):
            return
        groups: Dict[str, List[str]] = {}
        for name, files in raw.items():
            if isinstance(files, list):
                groups[str(name)] = [str(f) for f in files if isinstance(f, str)]
        self.change_groups = groups

    def set_mode(self, mode: str) -> None:
        """Record the task's mode (Modes round, additive state key).

        Assumes mode is one of fix|build|question|research (or "" to
        unset). Only non-fix modes set it, so fix-mode state.json files
        are byte-identical to the pre-modes schema (the key is omitted
        when empty — consumers must treat extra keys as ignorable per
        Boundary 4).
        """
        with self._lock:
            self.mode = str(mode or "")
            self._write()

    def set_phase(self, phase: str) -> None:
        """Mirror the formal state machine's live phase (steering
        round, additive state key).

        Written by run_task's TaskStateMachine on_transition hook —
        state.json's "phase" is the live-status surface (`vex status`,
        dashboard) per harness.state_machine's documented contract.
        Assumes phase is a state name from state_machine.ALL_STATES
        (or "" to unset); the key is omitted when empty, so consumers
        must treat it as ignorable per the Boundary 4 additive-key
        rule. Never raises into the machine (the hook guards anyway).
        """
        with self._lock:
            self.phase = str(phase or "")
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
                json.dumps(
                    {
                        "steps": steps,
                        "attempts": int(attempts),
                        "cost_usd": float(cost_usd),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def _write(self) -> None:
        obj = {
            "task_id": self.task_id,
            "plan": self.plan,
            "completed_steps": self.completed_steps,
            "files_touched": self.files_touched,
            "decisions": self.decisions,
            "remaining_plan": self.remaining_plan,
        }
        if self.repo_path:
            # Additive key (see module docstring): lets Terminal 4's
            # ingestion stamp decisions with the repo they belong to, so
            # planner-time queries can scope to THIS repo.
            obj["repo_path"] = self.repo_path
        if self.change_groups:
            # Additive key (Improvement Round 2): the plan's declared
            # atomic file groups — see module docstring for the shape.
            obj["change_groups"] = {
                name: list(files) for name, files in self.change_groups.items()
            }
        if self.mode:
            # Additive key (Modes round): the mode that produced this
            # task. Omitted for fix-mode runs (schema unchanged there).
            obj["mode"] = self.mode
        if self.phase:
            # Additive key (steering round): the state machine's live
            # phase (planning/editing/testing/repairing/...). Omitted
            # when unset — consumers treat it as ignorable per
            # Boundary 4's additive-key rule.
            obj["phase"] = self.phase
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def as_dict(self) -> Dict[str, Any]:
        """Return the current state as a dict in the Boundary 4 schema."""
        d = {
            "task_id": self.task_id,
            "plan": list(self.plan),
            "completed_steps": list(self.completed_steps),
            "files_touched": list(self.files_touched),
            "decisions": list(self.decisions),
            "remaining_plan": list(self.remaining_plan),
        }
        if self.repo_path:
            d["repo_path"] = self.repo_path
        if self.change_groups:
            d["change_groups"] = {
                name: list(files) for name, files in self.change_groups.items()
            }
        if self.mode:
            d["mode"] = self.mode
        if self.phase:
            d["phase"] = self.phase
        return d
