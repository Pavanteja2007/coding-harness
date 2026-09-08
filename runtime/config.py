"""Runtime configuration defaults and task.config key definitions.

All knobs live in the Task.config dict (project convention — no hardcoded
constants scattered in logic). This module documents the keys runtime reads
and provides defaults for a full `run(...)` invocation. Same-dict wiring:
CLI/Runtime passes Task.config entries through unchanged, and the worker
also feeds call_model config (provider/model/api_key) via the same dict.
"""
from __future__ import annotations

from typing import Any, Dict

# --- task.config keys owned/READ by the runtime -------------------------

# Execution / supervision (keys match harness/config.py where shared):
#   "max_wallclock_s"    hard per-task wall-clock kill (scheduler-side);
#                        same key+harness meaning as harness/config.py
#   "crash_retries"      scheduler-level extra attempts after a worker
#                        crash/kill (NOT the harness's own "max_retries",
#                        which counts verification retries inside run_task)
#   "resume"             True  -> resume from last checkpoint if one exists
#                        False -> always start fresh
#   "resume_dir"         where the runtime keeps its own resume files for
#                        this task. Default: logs/{task_id}/runtime/
#   "approval"           "require" -> worker pauses before finishing (the
#                        diff-approval gate; see runtime/approval.py)
#   "approval_timeout_s" how long the approval gate may block before giving
#                        up (None = block forever)
#
# Model routing (consumed via runtime/model_router.py, Boundary 2):
#   "provider" / "model" / "api_key"        explicit target (overrides hint)
#   "api_base"                               optional custom endpoint base URL
#                                           for the explicit target (BYO
#                                           gateway / openai-compatible
#                                           router; tiers may each carry
#                                           their own api_key/api_base)
#   "adaptive_routing"                       True/False master toggle
#   "model_tiers"                           {hint: {provider, model, api_key?,
#                                           api_base?}}
#   "difficulty_estimator"                  "heuristic" | "llm" | "off"
#   "difficulty_llm"                        {provider, model, api_key} for
#                                           the "llm" estimator
#   "use_mock_provider"                      True -> offline mock (tests)
#   "mock_responses"                        {model: content} for the mock
#   "rate_limit_retries"                     bounded retries on provider
#                                           rate-limit errors (429s) before
#                                           giving up; 0 disables retry
#   "rate_limit_backoff_s"                   first backoff wait, exponential
#                                           (15s, 30s, 60s, 120s for 4)

DEFAULTS: Dict[str, Any] = {
    # Scheduler shape
    "concurrency": 10,            # target band 10-50 per project spec
    "max_wallclock_s": 900.0,    # matches harness/config.py's key+default
    "crash_retries": 1,          # scheduler extra attempts after worker crash/kill
    "resume": True,
    # Model routing defaults
    "adaptive_routing": False,    # ablation flag: single toggle on/off
    "difficulty_estimator": "heuristic",
    "use_mock_provider": False,
    # Provider congestion resilience (rate limit retry/backoff)
    "rate_limit_retries": 4,
    "rate_limit_backoff_s": 15.0,
}

# Worker heartbeat cadence (s). The scheduler treats a heartbeat older
# than 3x this as process trouble, regardless of hang_heartbeat_stale_s.
HEARTBEAT_INTERVAL_S = 2.0

# Model tiers used when adaptive routing is ON but the task doesn't define
# its own tiers. Keys are difficulty hints; values target one litellm model
# string each. Costs here feed the ledger's price fallback table.
DEFAULT_MODEL_TIERS: Dict[str, Dict[str, str]] = {
    "easy":   {"provider": "openai",     "model": "gpt-4o-mini"},
    "medium": {"provider": "openai",     "model": "gpt-4o-mini"},
    "hard":   {"provider": "anthropic",  "model": "claude-3-5-sonnet-20241022"},
}


def apply_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge user task.config over DEFAULTS (user wins).

    Assumes `config` is a plain dict (or None); non-dict input is treated as
    empty. Never mutates the caller's dict.
    """
    merged = dict(DEFAULTS)
    if isinstance(config, dict):
        merged.update(config)
    return merged
