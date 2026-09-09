"""Config handling for the harness.

All tunables come from task.config (project convention: no hardcoded
constants), with these defaults applied for anything missing. The defaults
match the project spec's Phase 1 guidance (~3 retries, cheap default model).
"""
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    # Stopping conditions (checked in harness.core.run_task)
    "max_retries": 3,            # full attempts at the whole task, total
    "budget_cap_usd": 2.0,       # hard cap on model spend per task
    "max_wallclock_s": 900.0,    # per-task wall-clock cap in seconds
    "command_timeout_s": 120,    # per bash command inside a step session
    "verify_timeout_s": 300,     # for the verifier's test runs

    # Model / routing knobs (consumed by the model boundary / router).
    # None = let the router decide (adaptive routing needs this); an
    # explicit value in task.config pins that model for every call.
    "model": None,
    "provider": None,
    "api_key": None,

    # Harness behavior
    "max_step_turns": 15,        # bash-command turns per planner sub-step
    "max_output_chars": 3000,    # cap on tool output fed back to the model
    "context_files_cap": 4,      # files injected into a step session's context
    "context_lines_cap": 60,     # lines per file in that injection
    "max_file_bytes": 200_000,   # refuse to read/edit files larger than this
    "test_command": None,        # e.g. "python -m pytest -x"; None = autodetect
    "target_test": None,         # e.g. "tests/test_mathutil.py::test_mean"
    "baseline_reruns": 1,        # reruns for flake detection in verify()
    "protected_paths": [],       # globs the agent must not modify, e.g. tests/
    "work_subdir": "logs",       # where logs/{task_id}/ lives (repo-relative)

    # Product-grade output (spec items 26/29)
    "git_output": True,          # on verified success: branch + commit + PR
                                  # description via execution.git_output (in the
                                  # harness's PRIVATE work copy — never the
                                  # original repo); failure degrades to a trace
                                  # event, never fails the verified fix
    "rationale_log": True,       # write logs/{task_id}/rationale.md (one
                                  # grounded paragraph, all outcomes) via
                                  # execution.rationale
    "branch_name": None,         # explicit branch name; None = harness/fix-<slug>

    # Reversible compaction — on-demand reinjection (spec item 13).
    # state.json is the compacted view; trace.jsonl keeps everything; a
    # step session's RECALL <terms> message pulls older detail back.
    "max_recalls_per_step": 3,   # RECALL budget per step session (a step
                                  # must still do its WORK in bash turns)
    "recall_results_cap": 5,      # max trace entries returned per RECALL
    "recall_max_chars": 4000,     # combined char cap on RECALL results
}


def get_config(task_config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a config dict merging DEFAULTS with task.config overrides.

    Assumes task_config is a plain dict (Task.config); unknown keys are
    passed through untouched so other terminals can extend without this
    module needing changes. Values from task.config always win.
    """
    merged: Dict[str, Any] = dict(DEFAULTS)
    merged.update(task_config or {})
    return merged
