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
#
# --- VEX-CEILING-14: local-first, provider resilience, offline, privacy ----
#
# These keys are NOT in DEFAULTS on purpose. `runtime.model_router.call_model`
# enters the resilient pipeline (`runtime/provider_gateway.py`) only when at
# least one of these keys is PRESENT in the router context, so a value in
# DEFAULTS would silently switch every task onto the new path. "Absent" is
# therefore a real, meaningful state: it means unchanged behavior, and the
# pipeline carries its own internal defaults for the knobs below.
#
# Env equivalents read by the same code:
#   NEO_OFFLINE                  -> "offline"
#   NEO_CIRCUIT_BREAKER_THRESHOLD -> breaker failure_threshold (default 2)
#   NEO_CIRCUIT_BREAKER_RESET_S   -> breaker reset_seconds (default 30)
#   NEO_SETTINGS_WRITE_ATTEMPTS   -> locked config-write attempts (CLI tier)
#
# LOCAL-FIRST TIER (runtime/local_models.py). The cheap hints (retrieval /
# summarization / boilerplate) route to the local model; only the DECISIVE
# step (`hard`) escalates to a frontier model.
#   "local_model_profile"        {provider, model, api_base, api_key?,
#                                 context_window?, roles?, label?}
#   "local_model"                flat form: model name only
#   "local_provider"             flat form: default "ollama"
#   "local_api_base"             flat form: endpoint
#   "local_context_window"       flat form: a DECLARED window; the receipt
#                                 records the source as "declared", never as
#                                 a measurement
#   "local_first"                True/False forces the cheap tier on/off
#   "local_first_roles"          restrict which roles may go local
#   "local_first_decisive"       let the local model take the decisive step
#                                 (default: frontier keeps it)
#
# PROVIDER RESILIENCE (runtime/provider_resilience.py).
#   "provider_fallbacks"        ordered alternates:
#                               [{provider, model, api_key?, api_base?}]
#   "provider_fallback_max"      ceiling on ALTERNATES (default 3; the
#                               primary target is never dropped)
#   "provider_fallback_across_tiers"
#                               derive alternates from the remaining
#                               `model_tiers` entries in declaration order
#                               (default True once the key is present)
#   "model_calls_idempotent"     False -> the call is NEVER retried and NEVER
#                               failed over (it may create provider state)
#   "circuit_breaker_registry"   inject a BreakerRegistry (tests, or one
#                               long-lived process sharing a registry)
#
# OFFLINE / READ-ONLY (runtime/offline_mode.py).
#   "offline"                   True -> no remote target may be dialed; a
#                               local endpoint is still allowed
#   "offline_allow_local_models" False -> offline means NO egress at all
#   "read_only"                  documented intent; not itself the gate
#   durable queue path           <log_root>/<task_id>/offline_queue.jsonl
#
# PRIVACY (runtime/privacy_policy.py). Redaction is UNCONDITIONAL — the
# `secret` class is not a permission, it is a check.
#   "privacy_policy"             "local_only" | "source_ok" | "standard" |
#                               "zdr", or an inline mapping
#   "privacy_data_classes"       allowed classes: metadata, public, source,
#                               private, user, secret
#   "privacy_providers"          provider allow-list (empty = any)
#   "privacy_models"             model allow-list (empty = any)
#   "privacy_require_zdr"        only dial a provider with a DOCUMENTED
#                               zero-data-retention mode; "unknown" fails
#   "privacy_redact"             accepted for explicitness; cannot re-enable
#                               sending the `secret` class

DEFAULTS: Dict[str, Any] = {
    # Scheduler shape
    "concurrency": 10,  # target band 10-50 per project spec
    "max_wallclock_s": 900.0,  # matches harness/config.py's key+default
    "crash_retries": 1,  # scheduler extra attempts after worker crash/kill
    "resume": True,
    # Model routing defaults
    "adaptive_routing": False,  # ablation flag: single toggle on/off
    "difficulty_estimator": "heuristic",
    "use_mock_provider": False,
    # Provider congestion resilience (rate limit retry/backoff)
    "rate_limit_retries": 4,
    "rate_limit_backoff_s": 15.0,
    "orchestration_automation_enabled": False,
    # Planning (runtime.planning): 2..12 steps, configurable cap.
    "max_plan_steps": 12,
    "plan_step_turn_budget": 12,
    # Subagent bounds (runtime.subagents): the `task` tool is admitted
    # against exactly these limits; a child may not exceed them.
    "subagent_max_depth": 2,
    "subagent_max_children_per_parent": 4,
    "subagent_max_concurrent": 4,
    "subagent_max_spawn_requests": 16,
    "subagent_max_child_turns": 12,
    "subagent_max_child_cost_usd": 2.0,
    "subagent_max_total_cost_usd": 10.0,
    "subagent_summary_max_chars": 2048,
    "subagent_default_agent": "",
}

# Worker heartbeat cadence (s). The scheduler treats a heartbeat older
# than 3x this as process trouble, regardless of hang_heartbeat_stale_s.
HEARTBEAT_INTERVAL_S = 2.0

# Default state-stale window (s): how long a worker's state.json may go
# untouched before the scheduler's hang check fires. This lives HERE, not
# in scheduler.py, because R2-14 needs the SAME number in two places: the
# scheduler's kill threshold, and the worker's post-backoff grace (a worker
# whose provider backoff has just expired still needs one staleness window
# to land the retried call and write state). One constant, two readers, so
# the watchdog and the exemption cannot disagree about the window.
DEFAULT_HANG_STALE_S = 30.0

# Model tiers used when adaptive routing is ON but the task doesn't define
# its own tiers. Keys are difficulty hints; values target one litellm model
# string each. Costs here feed the ledger's price fallback table.
DEFAULT_MODEL_TIERS: Dict[str, Dict[str, str]] = {
    "easy": {"provider": "openai", "model": "gpt-4o-mini"},
    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
    "hard": {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"},
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
