"""Fake Boundary-3 `run_task` for scheduler fault-injection (pre-Terminal-1).

Mimics harness.core.run_task's contract — Task -> TaskResult — with
deterministic, config-driven behavior so the scheduler's concurrency,
timeout, crash-resume, and approval paths can be tested for real:
  use_fake_harness : True            — selects this fake (worker.py)
  fake_steps        : ["s1", ...]    — plan steps to "execute"
  fake_step_delay_s : float           — sleep per step
  fake_fail_step    : "s2" | None     — verification fails at this step
  fake_crash_step   : "s2" | None     — HARD KILL (os._exit) at this step,
                                        simulating a killed worker process
  fake_hang_step    : "s2" | None     — sleep 1e9 at this step (hang)
  fake_success      : bool            — overall success when reaching end
  fake_diff         : str             — the "proposed diff" for approval mode
  fake_state_dir    : path            — where to write a Boundary-4-shaped
                                        state.json (default logs/{task_id}/)

On resume (task.config["resume"] and a prior state.json with
completed_steps), it skips already-completed steps — the same resume
contract the real harness will implement. Heartbeats + checkpoints are
written by runtime/worker.py, not here (boundary of responsibilities).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared.types import Task, TaskResult, VerificationResult

DEFAULT_STEPS = ["plan", "retrieve", "edit", "verify", "git-output"]


def _state_path(task: Task) -> Path:
    d = task.config.get("fake_state_dir") or str(Path("logs") / task.task_id)
    return Path(d) / "state.json"


def _read_completed(state_path: Path) -> List[str]:
    if not state_path.exists():
        return []
    try:
        import json
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return list(data.get("completed_steps", []))
    except (OSError, ValueError):
        return []


def _write_state(state_path: Path, task_id: str, plan: List[str], completed: List[str]) -> None:
    import json

    state_path.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "task_id": task_id,
        "plan": plan,
        "completed_steps": completed,
        "files_touched": [],
        "decisions": [],
        "remaining_plan": [s for s in plan if s not in completed],
    }
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def run_task(task: Task) -> TaskResult:
    """Fake one harness run (Boundary 3 shape). Assumes task.config flags
    per the module docstring; unspecified flags give a clean success run.
    Never raises for expected outcomes — it returns TaskResult with an
    appropriate status; fake_crash_step kills the process instead."""
    cfg = task.config
    steps: List[str] = list(cfg.get("fake_steps", DEFAULT_STEPS))
    delay = float(cfg.get("fake_step_delay_s", 0.0))
    fail_step = cfg.get("fake_fail_step")
    crash_step = cfg.get("fake_crash_step")
    hang_step = cfg.get("fake_hang_step")
    success = bool(cfg.get("fake_success", True))
    diff = cfg.get("fake_diff", "diff --git a/fake.py b/fake.py\n+fixed\n")

    state_path = _state_path(task)

    resumed = False
    completed: List[str] = []
    if cfg.get("resume", True) and state_path.exists():
        completed = _read_completed(state_path)
        if completed:
            resumed = True

    plan_steps = steps
    _write_state(state_path, task.task_id, plan_steps, completed)

    attempts = 1 + (1 if resumed else 0)

    for step in plan_steps:
        if step in completed:
            continue
        if step == crash_step:
            _write_state(state_path, task.task_id, plan_steps, completed)
            os._exit(70)  # hard kill: no cleanup, no exception — a real crash
        if step == hang_step:
            # Windows time.sleep caps out well below 1e9s (OverflowError),
            # so hang as repeated short sleeps — same effect, portable.
            while True:
                time.sleep(60)
        if cfg.get("fake_model_calls"):
            # Exercise the real router (Boundary 2) once per step: one call
            # without a hint (lets the router predict difficulty from the
            # issue text) + one with an explicit hint if provided.
            from runtime.model_router import call_model

            call_model(
                [{"role": "user", "content": f"{task.issue_text}\n\nstep: {step}"}],
                difficulty_hint=cfg.get("fake_step_hints", {}).get(step),
            )
        time.sleep(delay)
        if step == fail_step:
            _write_state(state_path, task.task_id, plan_steps, completed)
            return _result(
                task, status="failed", attempts=attempts, diff=None,
                verification=VerificationResult(
                    target_test_passed=False, baseline_passed=True,
                    regression_passed=False, flaky=False,
                    raw_output=f"target test failed at step {step}",
                ),
            )
        completed.append(step)
        _write_state(state_path, task.task_id, plan_steps, completed)

    verification = None
    if success:
        verification = VerificationResult(
            target_test_passed=True, baseline_passed=True,
            regression_passed=True, flaky=False, raw_output="ok",
        )
    return _result(task, status="success" if success else "failed",
                  attempts=attempts, diff=diff, verification=verification)


def _result(task: Task, status: str, attempts: int, diff: Optional[str],
            verification: Optional[VerificationResult]) -> TaskResult:
    log_dir = Path("logs") / task.task_id
    log_dir.mkdir(parents=True, exist_ok=True)
    return TaskResult(
        task_id=task.task_id,
        status=status,  # type: ignore[arg-type]
        attempts=attempts,
        diff=diff,
        verification=verification,
        cost_usd=0.0,
        model_calls=[],
        log_path=str(log_dir / "trace.jsonl"),
    )
