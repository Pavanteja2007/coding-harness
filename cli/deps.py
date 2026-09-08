"""Dependency resolution for the CLI — mirrors the harness.deps pattern.

The CLI crosses module boundaries (Boundary 3/6): it calls Terminal 1's
run_task and Terminal 3's scheduler.run. Resolution order for each: an
injected override (tests) -> the real module -> a local stub with the
exact contract signature. The run_task boundary needs no stub (Terminal
1's real implementation is on disk); the scheduler boundary has one until
Terminal 3 lands runtime/scheduler.py.
"""
from typing import Callable, Optional

from shared.types import Task, TaskResult

_run_task_override: Optional[Callable[..., TaskResult]] = None
_scheduler_run_override: Optional[Callable[..., list]] = None


def get_run_task() -> Callable[..., TaskResult]:
    """Return the Boundary 3 run_task callable.

    Resolution order: override -> harness.core.run_task (Terminal 1, real).
    No stub exists by design — the real implementation is a project module.
    """
    if _run_task_override is not None:
        return _run_task_override
    from harness.core import run_task

    return run_task


def get_scheduler_run() -> Callable[..., list]:
    """Return the Boundary 6 scheduler run callable.

    Resolution order: override -> runtime.scheduler.run (Terminal 3, real)
    -> cli._stubs.scheduler.run (ThreadPoolExecutor fan-out, same
    signature). Swap happens automatically once Terminal 3's module is
    importable — no CLI code changes.
    """
    if _scheduler_run_override is not None:
        return _scheduler_run_override
    try:
        from runtime.scheduler import run  # type: ignore

        return run
    except ImportError:
        from cli._stubs.scheduler import run

        return run


def set_run_task(fn: Optional[Callable[..., TaskResult]]) -> None:
    """Inject a fake run_task (tests / demos). None clears it."""
    global _run_task_override
    _run_task_override = fn


def set_scheduler_run(fn: Optional[Callable[..., list]]) -> None:
    """Inject a fake scheduler run (tests / demos). None clears it."""
    global _scheduler_run_override
    _scheduler_run_override = fn


def reset_overrides() -> None:
    """Clear all injected overrides (test teardown)."""
    global _run_task_override, _scheduler_run_override
    _run_task_override = None
    _scheduler_run_override = None
