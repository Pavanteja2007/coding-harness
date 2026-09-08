"""STUB for the scheduler boundary (INTERFACES.md Boundary 6) — Terminal 3
owns the real runtime.scheduler.run.

Concurrent fan-out via ThreadPoolExecutor calling run_task per task, with
per-task status lines printed to stdout — enough to exercise the CLI's
run-benchmark path end-to-end today. When runtime/scheduler.py lands,
cli.deps.get_scheduler_run() picks it up automatically (import-probe
first, stub second) — no CLI code changes.

Signature contract (Boundary 6):
    run(tasks: list[Task], concurrency: int = 10, **kwargs) -> list[TaskResult]

Differences from the future real scheduler (documented for Terminal 3):
- No checkpoint/resume, no adaptive routing, no per-task worker processes
  (threads, not processes — fine for a stub, since run_task is thread-safe
  by its own docstring).
- Benchmarks: --subset is handled in the CLI; this stub receives the
  already-materialized Task list.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List

from shared.types import Task, TaskResult


def run(
    tasks: List[Task],
    concurrency: int = 10,
    run_task: Callable[[Task], TaskResult] | None = None,
    **kwargs: Any,
) -> List[TaskResult]:
    """STUB: run tasks concurrently via a thread pool; returns results in
    task-list order (not completion order) for deterministic output.

    Assumes `tasks` is a list of Task with distinct task_ids (run_task's
    thread-safety requirement) and concurrency >= 1. Optionally accepts
    an injected run_task (the CLI passes the resolved Boundary 3 callable
    so tests can stub one level down).
    """
    if not tasks:
        return []
    concurrency = max(1, min(int(concurrency), len(tasks)))
    fn = run_task or _resolve_run_task()
    results: Dict[str, TaskResult] = {}
    lock = threading.Lock()
    done_count = [0]

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fn, t): t for t in tasks}
        for fut in as_completed(futures):
            task = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # a crashed worker must not lose the batch
                result = TaskResult(
                    task_id=task.task_id,
                    status="error",
                    attempts=0,
                    diff=None,
                    verification=None,
                    cost_usd=0.0,
                    model_calls=[],
                    log_path="",
                )
                with lock:
                    print(f"[scheduler-stub] task {task.task_id} crashed: {exc}")
            with lock:
                results[task.task_id] = result
                done_count[0] += 1
                status = getattr(result, "status", "?")
                print(
                    f"[scheduler-stub] [{done_count[0]}/{len(tasks)}] "
                    f"{task.task_id}: {status}"
                )

    return [results[t.task_id] for t in tasks]


def _resolve_run_task() -> Callable[[Task], TaskResult]:
    """Same resolution as cli.deps (kept local to avoid an import cycle
    when cli.deps itself probes this stub)."""
    from harness.core import run_task

    return run_task
