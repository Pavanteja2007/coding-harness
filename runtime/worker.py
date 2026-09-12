"""One-task worker process — spawned by the scheduler via subprocess.

Entry: `python -m runtime.worker --task-json <path> --run-dir <path>`

Responsibilities:
  1. Load its Task from JSON (arguments file, not argv, so payloads of any
     size/content pass cleanly).
  2. Set the router call context (model cfg + per-call ledger) for THIS task.
  3. Run run_task (real harness if importable, fake otherwise), heartbeating
     throughout via a daemon thread.
  4. Enforce the approval gate when config["approval"] == "require".
  5. Write result.json (TaskResult dict) + final checkpoint; exit 0.

A killed worker leaves its last checkpoint + state.json intact — that IS
the crash-resume mechanism; the scheduler relaunches with the same
arguments file and the task resumes from completed steps.

Exit codes: 0 normal (any TaskResult.status), 70 fake-crash injection,
1 unexpected worker-level exception (logged to stderr + events).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# --- bootstrap: make repo root importable (worker runs as __main__) ------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime import approval as approval_mod
from runtime import mock_provider
from runtime.checkpoint import TaskCheckpoint
from runtime.config import HEARTBEAT_INTERVAL_S, apply_defaults
from runtime.fsutil import atomic_write_json, now_iso, read_json_or_none
from runtime.model_router import set_call_context
from runtime.paths import harness_log_root, runtime_root, state_json_path
from runtime.serialize import result_to_dict

from shared import tracing
from shared.types import Task, TaskResult


def _state_completed_steps(task: Task, cfg: Dict[str, Any]) -> list:
    """Best-effort read of state.json's completed_steps (Boundary 4)."""
    state = read_json_or_none(state_json_path(task.task_id, cfg))
    if isinstance(state, dict):
        return list(state.get("completed_steps", []))
    return []


def _start_heartbeat(cp: TaskCheckpoint, stop: threading.Event) -> None:
    def _beat() -> None:
        while not stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                cp.beat()
            except OSError:
                pass  # heartbeats are best-effort liveness, not correctness

    threading.Thread(target=_beat, daemon=True).start()


def _load_run_task(config: Dict[str, Any]):
    """Resolve the run_task callable (Boundary 3) for this worker.

    Real harness first (harness.core.run_task); fake otherwise — unless
    use_fake_harness is explicitly True, which pins the fake even if the
    real one exists (scheduler tests need deterministic behavior).
    """
    if config.get("use_fake_harness", False):
        from runtime.fake_harness import run_task

        return run_task
    try:
        from harness.core import run_task

        return run_task
    except ImportError:
        from runtime.fake_harness import run_task

        return run_task


def _call_run_task(run_task_fn, task: Task, log_root: Path):
    """Call run_task, passing log_root when the callable accepts it.

    The real harness.core.run_task takes an optional log_root (INTERFACES
    Change Log 2026-09-07) so its logs/{task_id}/ lands inside the task's
    resume_dir — the same root the runtime reads state.json from. The fake
    harness (and any Boundary-3-pure callable) takes only (task), so we
    probe the signature once and call accordingly.
    """
    import inspect

    try:
        params = inspect.signature(run_task_fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "log_root" in params:
        return run_task_fn(task, log_root)
    return run_task_fn(task)


def run_worker(task_json_path: str, run_dir: str) -> int:
    """Worker main: run one task to completion (or its own demise).

    Assumes: task_json_path holds a JSON-serialized Task; run_dir is this
    attempt's private directory (result.json lives there). Returns the
    worker exit code. Never both writes a result AND crashes — a crash
    means no result.json, which the scheduler reads as "retry/resume".
    """
    with open(task_json_path, "r", encoding="utf-8") as f:
        task_dict = json.load(f)
    task = Task(
        task_id=str(task_dict["task_id"]),
        repo_path=str(task_dict.get("repo_path", "")),
        issue_text=str(task_dict.get("issue_text", "")),
        config=dict(task_dict.get("config", {})),
    )

    cfg = apply_defaults(task.config)
    task.config = cfg  # harness sees defaults too (max_retries etc.)

    # Runtime bookkeeping lives in logs/{task_id}.runtime/ (sibling of the
    # harness's logs/{task_id}/, which core._fresh_paths archives on every
    # relaunch — anything inside it would be swept away mid-task).
    runtime_dir = runtime_root(task.task_id, cfg)
    cp = TaskCheckpoint(str(runtime_dir))
    result_path = Path(run_dir) / "result.json"

    existing = cp.load()
    # state.json (Boundary 4) is the progress authority. After a mid-run
    # crash the runtime checkpoint's completed_steps is stale (written at
    # worker start), so resume decisions key off state.json, not the
    # checkpoint.
    state_completed = _state_completed_steps(task, cfg)

    # ANY relaunch (checkpoint exists from a prior worker run) disarms the
    # fault injections: they are one-shot by design, else a respawned
    # worker would re-crash/re-hang at the same step until its budget died.
    if existing is not None:
        cfg.pop("fake_crash_step", None)
        cfg.pop("fake_hang_step", None)

    finished = bool(existing) and existing.get("status") == "finished"
    resuming = bool(cfg.get("resume", True)) and bool(state_completed) and not finished
    if resuming:
        cfg["resume"] = True
    else:
        cfg["resume"] = False

    attempt = int((existing or {}).get("attempt", 0)) + (1 if resuming else 0)
    cp.log_event(
        "worker_start",
        {"task_id": task.task_id, "resume": resuming, "attempt": attempt},
    )
    # Cross-module structured tracing (shared.tracing) — worker lifecycle
    # markers on the unified per-task stream. No-op without VEX_TRACE_DIR.
    tracing.emit(
        "runtime",
        "worker_start",
        task_id=task.task_id,
        resume=resuming,
        attempt=attempt,
    )
    cp.save(
        {
            "task_id": task.task_id,
            "attempt": attempt,
            "started_at": (existing or {}).get("started_at", now_iso()),
            "last_heartbeat": now_iso(),
            # state.json is the progress authority (see comment above)
            "completed_steps": state_completed
            or (existing or {}).get("completed_steps", []),
            "result": None,
            "status": "running",
        }
    )
    cp.beat()
    stop_event = threading.Event()
    _start_heartbeat(cp, stop_event)

    set_call_context(
        {
            "adaptive_routing": cfg.get("adaptive_routing", False),
            "model_tiers": cfg.get("model_tiers"),
            "difficulty_estimator": cfg.get("difficulty_estimator", "heuristic"),
            "difficulty_llm": cfg.get("difficulty_llm"),
            "provider": cfg.get("provider"),
            "model": cfg.get("model"),
            "api_key": cfg.get("api_key"),
            "api_base": cfg.get("api_base"),
            "use_mock_provider": cfg.get("use_mock_provider", False),
            "rate_limit_retries": cfg.get("rate_limit_retries", 4),
            "rate_limit_backoff_s": cfg.get("rate_limit_backoff_s", 15.0),
            # task_id rides the router context so per-call routing decisions can
            # land on the unified per-task trace stream (shared.tracing); the
            # router treats it as opaque passthrough — no routing semantics.
            "task_id": task.task_id,
            # Opt-in completion-token budget for the router (see
            # runtime/model_router.py call_model): reasoning-style endpoints
            # can exhaust an unbounded default on hidden reasoning tokens and
            # return no content. Set "max_completion_tokens" in Task.config to
            # enable; absent = endpoint default (previous behavior).
            "max_completion_tokens": cfg.get("max_completion_tokens"),
        },
        ledger_dir=str(runtime_dir / "model_ledger.jsonl"),
    )
    if cfg.get("use_mock_provider"):
        script_spec = cfg.get("mock_script")
        if script_spec:
            # Scripted harness-model driver (see runtime.mock_provider.
            # install_script): plan + per-step bash commands from a plain
            # dict, so the REAL harness loop runs deterministically with
            # zero network. Callables can't cross the process boundary —
            # the spec dict can.
            mock_provider.install_script(dict(script_spec))
        else:
            mock_provider.install(cfg.get("mock_responses") or {})

    run_task_fn = _load_run_task(cfg)
    result = _call_run_task(run_task_fn, task, harness_log_root(cfg))

    # -- approval gate (before the result is applied/reported) -----------
    # The gate can park the worker for a long human-scale time. The
    # harness isn't running, so state.json goes stale — which the
    # scheduler's hang check would misread as a hang and kill mid-gate.
    # The worker therefore marks "awaiting_approval" in its checkpoint
    # (the heartbeat daemon KEEPS beating — it stops only after the
    # gate); the scheduler exempts a gate-parked, heart-beating worker
    # from the state-stale kill. The marker is cleared in the FINAL
    # checkpoint write (atomically with status="finished") — a separate
    # finally-clear would reopen the race: marker=False + status=running
    # + stale state.json + not-yet-exited process = scheduler kill during
    # teardown. Heartbeat death or the wall-clock cap still kill it.
    if cfg.get("approval") == "require" and result.status == "success":
        gate_dir = str(runtime_dir / "approval")
        cp.log_event("approval_wait", {})
        tracing.emit("runtime", "approval_wait", task_id=task.task_id)
        cp.update(awaiting_approval=True)
        try:
            approval_mod.request_approval(
                gate_dir=gate_dir,
                task_id=task.task_id,
                diff=result.diff or "",
                issue_text=task.issue_text,
                summary="Proposed fix for review",
                timeout_s=cfg.get("approval_timeout_s"),
            )
            cp.log_event("approval_granted", {})
            tracing.emit("runtime", "approval_granted", task_id=task.task_id)
        except approval_mod.ApprovalRejected as exc:
            cp.log_event("approval_rejected", {"error": str(exc)})
            tracing.emit(
                "runtime", "approval_rejected", task_id=task.task_id, error=str(exc)
            )
            result.status = "failed"
            result.diff = None
            result.verification = None
        except approval_mod.ApprovalTimeout as exc:
            cp.log_event("approval_timeout", {"error": str(exc)})
            tracing.emit(
                "runtime", "approval_timeout", task_id=task.task_id, error=str(exc)
            )
            result.status = "timeout"
            result.diff = None
            result.verification = None

    stop_event.set()
    _emit(result_path, result)

    cp.update(
        last_heartbeat=now_iso(),
        completed_steps=_state_completed_steps(task, cfg),
        result=result_to_dict(result),
        status="finished",
        awaiting_approval=False,  # atomically with status: no False+running
    )
    cp.log_event("worker_finish", {"status": result.status})
    tracing.emit("runtime", "worker_finish", task_id=task.task_id, status=result.status)
    return 0


def _emit(result_path: Path, result: TaskResult) -> None:
    atomic_write_json(result_path, result_to_dict(result))


def main() -> int:
    parser = argparse.ArgumentParser(prog="runtime.worker")
    parser.add_argument("--task-json", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    try:
        return run_worker(args.task_json, args.run_dir)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — worker must never die silently
        print(f"worker-level exception: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
