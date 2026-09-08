"""Single source of truth for per-task path layout (worker + scheduler).

Layout (production defaults):

  logs/                              harness log_root (core.run_task nests
                                     {task_id}/ inside it, and ARCHIVES that
                                     dir on every relaunch via _fresh_paths)
  logs/{task_id}/state.json          Boundary-4 progress (Terminal 1 owns)
  logs/{task_id}.runtime/            runtime bookkeeping — SIBLING of the
                                     harness dir, so the harness's
                                     archive-on-relaunch never sweeps it:
                                     checkpoint.json, heartbeat.json,
                                     events.jsonl, model_ledger.jsonl,
                                     approval/

Both worker and scheduler resolve paths through these helpers only —
never hand-build "{task_id}/runtime" elsewhere (that drift bug already
happened once).

Overrides (task.config):
  "resume_dir"   -> runtime bookkeeping dir (replaces the default sibling)
  "fake_state_dir" -> where the fake harness writes state.json (tests)
  "log_root"     -> harness log_root passed to the real run_task
The scheduler pins both resume_dir and log_root to its own logs_root at
spawn time (unless the task config overrides them), so scheduler and
worker always share one tree — the worker's defaults are only for
standalone invocation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


def runtime_root(task_id: str, cfg: Dict[str, Any],
                  logs_root: Path = Path("logs")) -> Path:
    """Runtime bookkeeping dir for this task (checkpoint/heartbeat/ledger).

    Resolution precedence: explicit cfg["resume_dir"] (scheduler pins it to
    its logs_root at spawn) -> default sibling layout under logs_root.
    """
    override = cfg.get("resume_dir")
    if override:
        return Path(override)
    return logs_root / f"{task_id}.runtime"


def harness_log_root(cfg: Dict[str, Any],
                     logs_root: Path = Path("logs")) -> Path:
    """log_root to pass the real harness.run_task (it nests {task_id}/).

    Resolution: explicit cfg["log_root"] wins; else the caller's
    logs_root (scheduler passes its own so its tmp/test trees stay
    self-contained; the worker default is the production ./logs).
    """
    return Path(cfg.get("log_root") or logs_root)


def state_json_path(task_id: str, cfg: Dict[str, Any],
                    logs_root: Path = Path("logs")) -> Path:
    """Path to the Boundary-4 state.json for this task.

    Resolution: explicit fake_state_dir (fake harness writes there) ->
    harness layout {log_root}/{task_id}/state.json. Missing-file handling
    belongs to the caller (read_json_or_none / exists()).
    """
    fake_dir = cfg.get("fake_state_dir")
    if fake_dir:
        return Path(fake_dir) / "state.json"
    return harness_log_root(cfg, logs_root) / task_id / "state.json"
