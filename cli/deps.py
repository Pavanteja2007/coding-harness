"""Dependency resolution for the CLI — mirrors the harness.deps pattern.

The CLI crosses module boundaries (Boundary 3/6): it calls Terminal 1's
run_task and Terminal 3's scheduler.run. Resolution order for each: an
injected override (tests) -> the real module -> a local stub with the
exact contract signature. The run_task boundary needs no stub (Terminal
1's real implementation is on disk); the scheduler boundary has one until
Terminal 3 lands runtime/scheduler.py.

Ceiling Prompt 14 (provider resilience / local-first / offline / privacy)
adds four more boundaries, all resolved the same way so a test can inject a
double and a caller never has to know whether the real module is importable:

- ``get_resilient_call_model`` — the Boundary-2 entry point WITH provider
  failover, local-first tiering, the offline gate, and pre-request
  redaction. The plain ``get_run_task`` path is untouched; this is what a
  caller opts into.
- ``get_privacy_policy`` / ``get_privacy_authorize`` — what may leave the
  machine, and the decision function.
- ``get_offline_config`` / ``get_offline_queue`` — whether egress is
  disabled, and the durable deferral queue.
- ``get_local_model_profile`` / ``get_local_frontier_report`` — the local
  tier, and the measured local-vs-frontier token/cost split.

Every resolver here is total: an import failure of the real module raises at
the CALL (never a silent fall back to a permissive default), because a
privacy or offline boundary that degrades to "allow" is worse than one that
is unavailable.
"""

from typing import Any, Callable, Optional

from shared.types import TaskResult

_run_task_override: Optional[Callable[..., TaskResult]] = None
_scheduler_run_override: Optional[Callable[..., list]] = None
_resilient_call_override: Optional[Callable[..., Any]] = None
_privacy_policy_override: Optional[Callable[..., Any]] = None
_offline_config_override: Optional[Callable[..., bool]] = None
_offline_queue_override: Optional[Callable[..., Any]] = None
_local_profile_override: Optional[Callable[..., Any]] = None


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


def get_resilient_call_model() -> Callable[..., Any]:
    """Return the Ceiling-14 Boundary-2 entry point (override -> real).

    A drop-in for ``runtime.model_router.call_model``: same signature, same
    return contract. It adds a per-provider circuit breaker, a bounded
    fallback chain, the local-first cheap tier, the offline egress gate, the
    privacy policy, and redaction of the outgoing messages.

    The CLI does NOT use this by default. ``runtime.model_router`` itself
    delegates to the same pipeline when a resilience key is present in the
    router context, so a caller gets the behavior from its config rather than
    from a different function. This resolver exists for a caller that wants
    the resilient boundary WITHOUT a router context (a one-shot question, an
    onboarding health check).
    """
    if _resilient_call_override is not None:
        return _resilient_call_override
    from runtime.provider_gateway import resilient_call_model

    return resilient_call_model


def get_privacy_policy() -> Callable[..., Any]:
    """Return the per-task privacy policy resolver (override -> real)."""
    if _privacy_policy_override is not None:
        return _privacy_policy_override
    from runtime.privacy_policy import policy_from_config

    return policy_from_config


def get_privacy_authorize() -> Callable[..., Any]:
    """Return the privacy decision function for one (policy, target) pair."""
    from runtime.privacy_policy import authorize

    return authorize


def get_privacy_public_view() -> Callable[..., Any]:
    """Return the redacted trace view for a status/share surface.

    Delegates to the shared privacy primitive rather than re-implementing a
    display filter, so what a user sees and what a provider would receive are
    filtered by one policy.
    """
    from shared.privacy import privacy_view

    return privacy_view


def get_offline_config() -> Callable[..., bool]:
    """Return the offline predicate (override -> real).

    Takes an optional config mapping and answers whether egress is disabled.
    """
    if _offline_config_override is not None:
        return _offline_config_override
    from runtime.offline_mode import offline_config

    return offline_config


def get_offline_queue() -> Callable[..., Any]:
    """Return the durable offline deferral queue class (override -> real)."""
    if _offline_queue_override is not None:
        return _offline_queue_override
    from runtime.offline_mode import OfflineQueue

    return OfflineQueue


def get_local_model_profile() -> Callable[..., Any]:
    """Return the local-model profile resolver (override -> real)."""
    if _local_profile_override is not None:
        return _local_profile_override
    from runtime.local_models import resolve_local_profile

    return resolve_local_profile


def get_local_frontier_report() -> Callable[..., Any]:
    """Return the local-vs-frontier token/cost report builder (override -> real).

    Reads a model ledger (or takes rows) and reports how much of a run stayed
    local. A caller surfaces this in ``/cost`` and ``--json`` so the split is
    measured rather than asserted.
    """
    from runtime.local_models import summarize

    return summarize


def get_breaker_snapshot() -> Callable[..., Any]:
    """Return the per-provider circuit-breaker snapshot (override -> real)."""
    from runtime.provider_resilience import snapshot

    return snapshot


def set_run_task(fn: Optional[Callable[..., TaskResult]]) -> None:
    """Inject a fake run_task (tests / demos). None clears it."""
    global _run_task_override
    _run_task_override = fn


def set_scheduler_run(fn: Optional[Callable[..., list]]) -> None:
    """Inject a fake scheduler run (tests / demos). None clears it."""
    global _scheduler_run_override
    _scheduler_run_override = fn


def set_resilient_call_model(fn: Optional[Callable[..., Any]]) -> None:
    """Inject a fake resilient Boundary-2 entry point. None clears it."""
    global _resilient_call_override
    _resilient_call_override = fn


def set_privacy_policy(fn: Optional[Callable[..., Any]]) -> None:
    """Inject a fake privacy policy resolver. None clears it."""
    global _privacy_policy_override
    _privacy_policy_override = fn


def set_offline_config(fn: Optional[Callable[..., bool]]) -> None:
    """Inject a fake offline predicate. None clears it."""
    global _offline_config_override
    _offline_config_override = fn


def set_offline_queue(cls: Optional[Callable[..., Any]]) -> None:
    """Inject a fake offline queue class. None clears it."""
    global _offline_queue_override
    _offline_queue_override = cls


def set_local_model_profile(fn: Optional[Callable[..., Any]]) -> None:
    """Inject a fake local-profile resolver. None clears it."""
    global _local_profile_override
    _local_profile_override = fn


def reset_overrides() -> None:
    """Clear all injected overrides (test teardown)."""
    global _run_task_override, _scheduler_run_override
    global _resilient_call_override, _privacy_policy_override
    global _offline_config_override, _offline_queue_override, _local_profile_override
    _run_task_override = None
    _scheduler_run_override = None
    _resilient_call_override = None
    _privacy_policy_override = None
    _offline_config_override = None
    _offline_queue_override = None
    _local_profile_override = None
