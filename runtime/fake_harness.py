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
  fake_backoff_step : "s2" | None     — run the REAL budget governor's dial
                                        loop against a 429-raising provider for
                                        `fake_backoff_429s` seconds, so the
                                        scheduler's hang kill and the provider
                                        backoff are exercised together
  fake_quota_step   : "s2" | None     — the REAL governor's dial loop against a
                                        provider reporting an exhausted quota
  fake_success      : bool            — overall success when reaching end
  fake_diff         : str             — the "proposed diff" for approval mode
  fake_state_dir    : path            — where to write a Boundary-4-shaped
                                        state.json (default {log_root}/
                                        {task_id}/ — the pinned log_root,
                                        NOT the repo-CWD ./logs)

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


class FakeRateLimitError(Exception):
    """A 429 shaped like litellm's, for the REAL governor's dial loop.

    The class name and status code are the parts the classifier reads, so
    this exercises `runtime.budget_governor.classify_provider_failure`
    rather than a test-local shortcut.
    """

    status_code = 429


class FakeQuotaExhaustedError(Exception):
    """An exhausted-quota error, for the same reason as above."""

    status_code = 429


def _fault_step(cfg: Dict[str, Any], task: Task) -> None:
    """Run the requested provider-fault step through the REAL governor.

    R2-14. This is deliberately not a stubbed retry: it calls
    ``runtime.budget_governor.governed_completion`` — the exact function
    ``runtime.provider_gateway`` dials through — with a dial that raises the
    requested provider error. The governor the worker installed therefore
    arms the supervision exemption in the real runtime checkpoint and
    sleeps the real backoff, and the scheduler's hang kill is tested against
    a genuine provider backoff rather than against a `sleep()`.

    Assumes a governor is installed for this execution context (the worker
    installs one before calling run_task). With none, the loop still runs
    and the backoff still happens, but nothing excuses the worker from the
    hang kill — which is the honest OFF arm.
    """
    from runtime import budget_governor

    step = cfg.get("fake_backoff_step")
    quota = bool(cfg.get("fake_quota_step"))
    if not step and not quota:
        return
    seconds = float(cfg.get("fake_backoff_429s", 0.0))
    if quota:
        step = cfg.get("fake_quota_step")
    governor = budget_governor.current_governor()
    attempts = {"n": 0}

    def _dial() -> str:
        attempts["n"] += 1
        if quota:
            raise FakeQuotaExhaustedError(
                "litellm.RateLimitError: You exceeded your current quota, "
                "check your plan and billing details (insufficient_quota)"
            )
        if seconds > 0.0 and attempts["n"] <= int(cfg.get("fake_backoff_429_count", 1)):
            raise FakeRateLimitError("RateLimitError: 429 Too Many Requests")
        return "ok"

    budget_governor.governed_completion(
        _dial,
        max_retries=int(cfg.get("rate_limit_retries", 4) or 0),
        base_backoff_s=seconds or 1.0,
        governor=governor,
    )


def _state_path(task: Task) -> Path:
    """state.json path for this fake run — resolved EXACTLY like the
    worker/scheduler resolve it (runtime.paths.state_json_path), so a
    scheduler that pins log_root (non-default --log-root / run-benchmark)
    has writer and reader agree on one tree. The old default (./logs/
    {task_id} under the repo CWD) diverged from the pinned log_root and
    silently broke resume outside the repo root.
    """
    from runtime.paths import state_json_path

    return state_json_path(task.task_id, task.config)


def _read_completed(state_path: Path) -> List[str]:
    if not state_path.exists():
        return []
    try:
        import json

        data = json.loads(state_path.read_text(encoding="utf-8"))
        return list(data.get("completed_steps", []))
    except (OSError, ValueError):
        return []


def _write_state(
    state_path: Path, task_id: str, plan: List[str], completed: List[str]
) -> None:
    # via fsutil.atomic_write_json: the replace can transiently fail
    # with PermissionError on Windows when a supervisor concurrently
    # reads state.json (killers/hang checks) — the bounded retry lives
    # in one place, not in every caller.
    from runtime.fsutil import atomic_write_json

    atomic_write_json(
        state_path,
        {
            "task_id": task_id,
            "plan": plan,
            "completed_steps": completed,
            "files_touched": [],
            "decisions": [],
            "remaining_plan": [s for s in plan if s not in completed],
        },
    )


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
    backoff_step = cfg.get("fake_backoff_step")
    quota_step = cfg.get("fake_quota_step")
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
        if step in (backoff_step, quota_step):
            # R2-14: the REAL governor dial loop, so the backoff and the
            # scheduler's hang kill are measured against each other.
            _fault_step(cfg, task)
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
                task,
                status="failed",
                attempts=attempts,
                diff=None,
                verification=VerificationResult(
                    target_test_passed=False,
                    baseline_passed=True,
                    regression_passed=False,
                    flaky=False,
                    raw_output=f"target test failed at step {step}",
                ),
            )
        completed.append(step)
        _write_state(state_path, task.task_id, plan_steps, completed)

    verification = None
    if success:
        verification = VerificationResult(
            target_test_passed=True,
            baseline_passed=True,
            regression_passed=True,
            flaky=False,
            raw_output="ok",
        )
    return _result(
        task,
        status="success" if success else "failed",
        attempts=attempts,
        diff=diff,
        verification=verification,
    )


def _result(
    task: Task,
    status: str,
    attempts: int,
    diff: Optional[str],
    verification: Optional[VerificationResult],
) -> TaskResult:
    log_dir = _state_path(task).parent  # same tree as the state file
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
