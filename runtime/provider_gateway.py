"""The resilient provider call: local-first, breaker-backed, bounded
fallback, offline-enforced, privacy-checked.

This module is the implementation behind Ceiling Prompt 14. It exists as its
own module (rather than as more logic inside ``runtime/model_router.py``)
for three concrete reasons, not for tidiness:

- ``runtime/model_router.py`` is the Boundary-2 public contract and is
  shared by every consumer; a resilience layer that is separately callable
  can be tested and adopted without changing the contract's shape.
- The pipeline is a *sequence of decisions* (redact -> offline -> privacy ->
  breaker -> dial -> classify), and each decision is independently testable
  when it is a function with one job.
- The router's own private helpers (tier resolution, usage extraction, error
  redaction, prompt-cache planning) are reused by late import instead of
  being re-implemented, so there is exactly one implementation of each.

The pipeline, in order, and why the order matters:

1. **Redact** the outgoing messages. Before anything else, because every
   later stage records a receipt about "what was sent", and a receipt that
   describes unredacted content is a leak of its own.
2. **Resolve candidates**: the primary target from the router's normal
   precedence, plus a BOUNDED fallback chain (explicit
   ``provider_fallbacks`` first, then the remaining configured tiers).
3. **Screen each candidate** — offline first (a local endpoint is fine, a
   remote one is refused before a request is built), then the privacy
   policy, then the circuit breaker. A refused or tripped candidate is
   recorded with a reason and skipped; the chain continues.
4. **Authorize, then dial.** The budget governor prices the NEXT call and
   refuses one that cannot fit inside the remaining budget (R2-14); the
   dial itself goes through ``runtime.budget_governor.governed_completion``,
   which owns the ONE backoff schedule, arms the supervision exemption for
   exactly the wait it is about to sleep, and treats an exhausted quota as
   terminal. Then classify the outcome: a retryable, idempotent failure
   counts against the provider's breaker and moves to the next candidate; a
   non-retryable failure (a rejected request) is NOT a provider outage and
   does not trip the breaker.

Fail-closed shapes, stated so no caller is surprised:

- Every candidate refused by privacy -> :class:`PrivacyPolicyBlocked`.
- Every candidate refused by offline -> :class:`OfflineEgressBlocked`.
- A call that does not fit the remaining budget ->
  :class:`runtime.budget_governor.BudgetRefused`, raised BEFORE any
  request is built.
- An exhausted quota -> :class:`runtime.budget_governor.QuotaExhausted` on
  the first attempt: no retry, no backoff, and no failover, because a
  second target on the same billing account spends the same absent money.
- Every candidate tried and failed -> the LAST provider exception, re-raised
  unchanged when there is only one candidate. That last part is a
  compatibility guarantee: with no fallbacks configured this module is
  transparent, including the exception object the caller sees.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import (
    budget_governor,
    local_models,
    model_capabilities,
    offline_mode,
    privacy_policy,
    prompt_cache,
    provider_resilience,
)

__all__ = [
    "Candidate",
    "ScreenResult",
    "StagedCall",
    "classify_outcome",
    "enforce_stream_gate",
    "prepare_call",
    "resilient_call_model",
    "resolve_candidates",
    "screen_candidates",
]


@dataclass
class Candidate:
    """One dialable target plus everything decided about it.

    ``skipped`` / ``skip_reason`` are how a refusal becomes evidence: a
    candidate the policy blocked is still reported, with the reason, instead
    of vanishing from the ledger.
    """

    index: int
    target: Dict[str, Any]
    source: str = "primary"
    identity: str = ""
    tier_class: str = "frontier"
    skipped: bool = False
    skip_reason: Optional[str] = None
    skip_detail: Optional[str] = None
    privacy: Optional[privacy_policy.PrivacyDecision] = None
    breaker_state: str = provider_resilience.CLOSED
    zdr_kwargs: Dict[str, Any] = field(default_factory=dict)

    @property
    def provider(self) -> Optional[str]:
        """The candidate's provider name."""
        return self.target.get("provider")

    @property
    def model(self) -> Optional[str]:
        """The candidate's model name."""
        return self.target.get("model")

    @property
    def api_base(self) -> Optional[str]:
        """The candidate's endpoint, if it has one."""
        return self.target.get("api_base")

    def as_dict(self) -> Dict[str, Any]:
        """Return a receipt with no credential and no raw endpoint URL."""
        return {
            "index": self.index,
            "source": self.source,
            "identity": self.identity,
            "provider": self.provider,
            "model": self.model,
            "api_base_sha256": local_models.endpoint_fingerprint(self.api_base),
            "tier_class": self.tier_class,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "skip_detail": self.skip_detail,
            "breaker_state": self.breaker_state,
            "privacy": self.privacy.as_dict() if self.privacy is not None else None,
        }


@dataclass
class ScreenResult:
    """The outcome of screening a candidate chain."""

    permitted: List[Candidate] = field(default_factory=list)
    refused: List[Candidate] = field(default_factory=list)
    ordered: List[Candidate] = field(default_factory=list)
    privacy_decisions: List[privacy_policy.PrivacyDecision] = field(
        default_factory=list
    )
    offline: Optional[offline_mode.OfflineIndicator] = None

    @property
    def has_refusals(self) -> bool:
        """True when at least one candidate was refused or tripped."""
        return bool(self.refused)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe receipt for the ledger / trace."""
        return {
            "permitted": [item.as_dict() for item in self.permitted],
            "refused": [item.as_dict() for item in self.refused],
            "ordered": [item.as_dict() for item in self.ordered],
            "offline": self.offline.as_dict() if self.offline is not None else None,
        }


@dataclass
class StagedCall:
    """A fully prepared call: sanitized messages plus a screened chain."""

    messages: Any
    redaction: Dict[str, Any]
    candidates: List[Candidate]
    screen: ScreenResult
    policy: privacy_policy.PrivacyPolicy
    local_profile_receipt: Optional[Dict[str, Any]] = None

    def permitted(self) -> List[Candidate]:
        """Return the candidates that may be dialed, in order."""
        return list(self.screen.permitted)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe receipt with no payload content."""
        return {
            "redaction": dict(self.redaction),
            "candidates": [item.as_dict() for item in self.candidates],
            "policy": self.policy.as_dict(),
            "local_model": self.local_profile_receipt,
            "offline": (
                self.screen.offline.as_dict()
                if self.screen.offline is not None
                else None
            ),
        }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def resolve_candidates(
    primary: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
    *,
    hint: Optional[str] = None,
    explicit: bool = False,
) -> List[Dict[str, Any]]:
    """Return the ordered, bounded target chain for one logical call.

    Sources, in precedence order:

    1. ``context["provider_fallbacks"]`` — an explicit, ordered list of
       alternate targets. This is the shape an operator uses to say "if
       OpenAI is down, use my own gateway".
    2. the configured ``model_tiers`` table, minus the primary's own target,
       in declaration order. A hard-tier call therefore falls forward to the
       cheaper tiers (cost protection) and a cheap-tier call falls forward
       to the more capable ones (recoverability) — which is why the order is
       the table's and not a guess.

    The chain is bounded by ``provider_fallback_max`` (default 3) and
    de-duplicated by provider identity. With no fallbacks and one tier the
    result is exactly ``[primary]``, which is what keeps this transparent.
    """
    ctx = dict(context or {})
    base = dict(primary or {})
    chain: List[Dict[str, Any]] = [base]
    explicit_fallbacks = ctx.get("provider_fallbacks")
    if isinstance(explicit_fallbacks, (list, tuple)) and explicit_fallbacks:
        for item in explicit_fallbacks:
            if not isinstance(item, Mapping):
                continue
            target = {
                "provider": item.get("provider"),
                "model": item.get("model"),
                "api_key": item.get("api_key"),
                "api_base": item.get("api_base") or item.get("base_url"),
                "routed_via_hint": hint or item.get("routed_via_hint"),
                "display_label": item.get("display_label") or item.get("label"),
                "input_cost_per_million": item.get("input_cost_per_million"),
                "output_cost_per_million": item.get("output_cost_per_million"),
                "source_tier": item.get("source_tier") or "provider_fallback",
            }
            chain.append(target)
    elif not explicit and _as_bool(ctx.get("provider_fallback_across_tiers"), True):
        tiers = ctx.get("model_tiers")
        if isinstance(tiers, Mapping):
            profile = ctx.get("provider_profile")
            if not isinstance(profile, Mapping):
                profile = {}
            for tier_hint, tier in tiers.items():
                if not isinstance(tier, Mapping) or not tier.get("model"):
                    continue
                chain.append(
                    {
                        "provider": tier.get("provider")
                        or profile.get("provider")
                        or base.get("provider"),
                        "model": tier.get("model"),
                        "api_key": tier.get("api_key") or profile.get("api_key"),
                        "api_base": tier.get("api_base")
                        or tier.get("base_url")
                        or base.get("api_base"),
                        "routed_via_hint": hint or tier_hint,
                        "display_label": base.get("display_label"),
                        "input_cost_per_million": tier.get("input_cost_per_million"),
                        "output_cost_per_million": tier.get("output_cost_per_million"),
                        "source_tier": tier.get("source_tier") or f"tier:{tier_hint}",
                    }
                )
    limit = _as_int(
        ctx.get("provider_fallback_max"), provider_resilience.DEFAULT_MAX_FALLBACKS
    )
    if ctx.get("provider_fallbacks") is not None and not isinstance(
        ctx.get("provider_fallbacks"), (list, tuple)
    ):
        # A malformed fallback list is a configuration error, not a silent
        # "no fallbacks": fall back to the primary alone but say so.
        limit = 0
    return provider_resilience.bound_fallbacks(chain, max_fallbacks=max(0, limit))


def _breaker_for(
    context: Optional[Mapping[str, Any]],
) -> provider_resilience.BreakerRegistry:
    """Return the breaker registry, honoring per-config thresholds.

    A caller can inject its own registry (a test, or a long-lived process
    that wants one registry across several task contexts) through
    ``context["circuit_breaker_registry"]``.
    """
    injected = (context or {}).get("circuit_breaker_registry")
    if isinstance(injected, provider_resilience.BreakerRegistry):
        return injected
    return provider_resilience.default_registry()


def screen_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    policy: Optional[privacy_policy.PrivacyPolicy] = None,
    config: Optional[Mapping[str, Any]] = None,
    registry: Optional[provider_resilience.BreakerRegistry] = None,
) -> ScreenResult:
    """Screen a candidate chain: offline, privacy, then the breaker.

    The order is load-bearing. Offline runs first because it is the strongest
    statement ("nothing may leave"), and because a local candidate is
    permitted by the strictest privacy policy anyway — screening privacy
    first would produce a confusing "local endpoint refused" outcome that
    never happens. The breaker runs last so its state is only consulted for a
    target the policy would actually have allowed.
    """
    resolved_policy = policy or privacy_policy.policy_from_config(config)
    breakers = registry or _breaker_for(config)
    result = ScreenResult()
    offline_active = offline_mode.offline_config(config)
    result.offline = offline_mode.offline_indicator(config)
    for index, raw in enumerate(candidates or ()):
        target = dict(raw or {})
        provider = target.get("provider")
        model = target.get("model")
        api_base = target.get("api_base")
        candidate = Candidate(
            index=index,
            target=target,
            source=str(
                target.get("source_tier") or ("primary" if index == 0 else "fallback")
            ),
            identity=provider_resilience.provider_identity(provider, api_base, model),
            tier_class=local_models.classify_target(provider, api_base),
        )
        result.ordered.append(candidate)
        if offline_active:
            try:
                offline_mode.require_local(
                    provider, model, api_base, offline=True, config=config
                )
            except offline_mode.OfflineEgressBlocked as exc:
                candidate.skipped = True
                candidate.skip_reason = "offline_egress_blocked"
                candidate.skip_detail = str(exc)
                result.refused.append(candidate)
                continue
        decision = privacy_policy.authorize(
            resolved_policy,
            provider,
            model,
            api_base,
            data_classes=target.get("data_classes"),
        )
        candidate.privacy = decision
        if not decision.allowed:
            candidate.skipped = True
            candidate.skip_reason = decision.reason
            candidate.skip_detail = decision.detail
            result.privacy_decisions.append(decision)
            result.refused.append(candidate)
            continue
        if decision.zdr_requested:
            candidate.zdr_kwargs = privacy_policy.zdr_kwargs(provider, api_base)
        permitted, reason = breakers.allow(candidate.identity)
        candidate.breaker_state = breakers.state(candidate.identity)
        if not permitted:
            candidate.skipped = True
            candidate.skip_reason = reason
            candidate.skip_detail = (
                "provider circuit is open; a bounded fallback target will be used"
                if reason == provider_resilience.CIRCUIT_OPEN_REASON
                else "provider recovery probe already in flight"
            )
            result.refused.append(candidate)
            continue
        result.permitted.append(candidate)
    return result


def prepare_call(
    messages: Any,
    *,
    context: Optional[Mapping[str, Any]] = None,
    target: Optional[Mapping[str, Any]] = None,
    hint: Optional[str] = None,
    api_key: Optional[str] = None,
    explicit: bool = False,
) -> StagedCall:
    """Redact, resolve the chain, and screen it — the whole pre-dial phase.

    Returns a :class:`StagedCall`. The caller is responsible for the dial and
    for the ledger/trace rows; :func:`resilient_call_model` is the reference
    implementation of that.
    """
    ctx = dict(context or {})
    policy = privacy_policy.policy_from_config(ctx)
    secrets = privacy_policy.scrub_environment_secrets(ctx)
    if api_key:
        secrets = [api_key, *secrets]
    sanitized, receipt = privacy_policy.redact_messages(
        messages, extra_secrets=secrets, enabled=True
    )
    chain = resolve_candidates(target or {}, ctx, hint=hint, explicit=explicit)
    screen = screen_candidates(chain, policy=policy, config=ctx)
    profile = local_models.resolve_local_profile(ctx)
    return StagedCall(
        messages=sanitized,
        redaction=receipt,
        candidates=list(screen.ordered),
        screen=screen,
        policy=policy,
        local_profile_receipt=profile.as_dict() if profile is not None else None,
    )


def classify_outcome(
    exc: BaseException,
    *,
    idempotent: bool = True,
    attempts_used: int = 0,
    max_retries: int = 0,
) -> provider_resilience.RetryDecision:
    """Classify one provider failure using the ROUTER's own heuristics.

    ``runtime.model_router`` already knows what a rate limit and what a
    gateway flake look like (it has to, for the existing retry loop). This
    passes those classifiers in so the repository has ONE notion of each,
    rather than a second, subtly different copy living in the resilience
    layer. A missing router is handled by falling back to this module's own
    heuristics, which is what makes it independently testable.
    """
    is_rate_limit: Optional[bool] = None
    is_transient: Optional[bool] = None
    try:
        from . import model_router

        is_rate_limit = model_router._is_rate_limit_error(exc)
        is_transient = model_router._is_transient_error(exc)
    except Exception:  # pragma: no cover - router always imports
        is_rate_limit = None
        is_transient = None
    return provider_resilience.should_retry(
        exc,
        idempotent=idempotent,
        attempts_used=attempts_used,
        max_retries=max_retries,
        backoff_exponent=attempts_used,
        is_transient=is_transient,
        is_rate_limit=is_rate_limit,
    )


def enforce_stream_gate(
    messages: Any,
    *,
    context: Optional[Mapping[str, Any]] = None,
    target: Optional[Mapping[str, Any]] = None,
    hint: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Any:
    """Run redaction + offline + privacy on a STREAMED call, then hand back.

    A streamed response cannot be failed over (a partially consumed stream
    must never be replayed as a second charge), but the GATE must still
    apply: otherwise ``stream=True`` would be a one-flag way to send an
    unredacted prompt to a provider the task's privacy policy forbids. This
    returns the sanitized messages and raises the same typed refusals the
    non-streaming path raises, so the caller continues into its own streaming
    dial with a payload that is already safe.
    """
    ctx = dict(context or {})
    resolved = dict(target or {})
    policy = privacy_policy.policy_from_config(ctx)
    secrets = privacy_policy.scrub_environment_secrets(ctx)
    if api_key:
        secrets = [api_key, *secrets]
    sanitized, receipt = privacy_policy.redact_messages(
        messages, extra_secrets=secrets, enabled=True
    )
    offline_active = offline_mode.offline_config(ctx)
    if offline_active:
        offline_mode.require_local(
            resolved.get("provider"),
            resolved.get("model"),
            resolved.get("api_base"),
            offline=True,
            config=ctx,
        )
    decision = privacy_policy.authorize(
        policy,
        resolved.get("provider"),
        resolved.get("model"),
        resolved.get("api_base"),
    )
    if not decision.allowed:
        raise privacy_policy.PrivacyPolicyBlocked([decision])
    _emit_gate_receipt(ctx, receipt, decision, offline_active)
    return sanitized


def _emit_gate_receipt(
    ctx: Mapping[str, Any],
    receipt: Mapping[str, Any],
    decision: privacy_policy.PrivacyDecision,
    offline: bool,
) -> None:
    """Emit the streamed-path gate receipt to the unified trace (best effort)."""
    task_id = str(ctx.get("task_id") or "")
    if not task_id:
        return
    try:
        from shared import tracing

        tracing.emit(
            "runtime",
            "provider_gate",
            task_id=task_id,
            streamed=True,
            offline=bool(offline),
            privacy=decision.as_dict(),
            redaction=dict(receipt),
        )
    except Exception:  # pragma: no cover - tracing never raises
        pass


def resilient_call_model(
    messages: list,
    difficulty_hint: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    tools: Optional[list] = None,
    tool_choice: Optional[str] = None,
    *,
    idempotent: bool = True,
    primary_target: Optional[Mapping[str, Any]] = None,
    effort: Any = None,
) -> Any:
    """Call a model through the full resilient pipeline.

    A drop-in for ``runtime.model_router.call_model``: same arguments, same
    return contract (a string, or the normalized native-tool-call mapping
    when the provider answers with tool calls), same ledger, same trace
    event. It differs in exactly one respect — a provider outage is survived
    when a fallback target exists, and a refusal is raised when none does.

    The router remains the implementation it was; this is the additive path
    that turns the router's per-call retry loop into a per-call *provider*
    failover. ``idempotent=False`` disables both the in-provider retry and
    the failover, because a replay of a non-idempotent operation is charged
    twice and may duplicate provider-side state.

    ``effort`` (AGT-08) is resolved PER CANDIDATE, not once for the chain: a
    fallback target may support a different provider knob than the primary,
    and a plan computed against the primary would be a lie for the model that
    actually answered. Each attempt's ledger row carries its own plan.
    """
    from . import model_router

    ctx = dict(model_router._CONTEXT.get() or {})
    ledger = model_router._LEDGER_PATH.get()
    normalized_hint = model_router._norm_hint(difficulty_hint)
    if normalized_hint is None and not (model or ctx.get("model")):
        predicted = model_router._maybe_predict_difficulty(messages, ctx)
        if predicted:
            difficulty_hint = predicted.get("hint")
            normalized_hint = model_router._norm_hint(difficulty_hint)

    # 1. Resolve the primary target with the router's normal precedence,
    #    then let the local-first tier substitute the cheap hints.
    if primary_target:
        primary = dict(primary_target)
    else:
        primary = model_router._resolve_target(difficulty_hint, provider, model, ctx)
    local_applied = False
    if local_models.should_use_local(
        ctx,
        normalized_hint,
        explicit_model=bool(model or ctx.get("model")),
        explicit_provider=bool(provider or ctx.get("provider")),
        adaptive_routing=_as_bool(ctx.get("adaptive_routing"), False),
    ):
        profile = local_models.resolve_local_profile(ctx)
        if profile is not None:
            primary = {
                **profile.as_target(),
                "routed_via_hint": normalized_hint or primary.get("routed_via_hint"),
                "local_first": True,
            }
            local_applied = True

    # 2. Redact + resolve the bounded chain + screen it.
    staged = prepare_call(
        messages,
        context=ctx,
        target=primary,
        hint=normalized_hint,
        api_key=api_key,
        explicit=bool(model or provider),
    )
    call_id = _new_call_id()
    started = time.time()
    offline_receipt = (
        staged.screen.offline.as_dict() if staged.screen.offline is not None else None
    )
    configured_tiers = (
        ctx.get("model_tiers") if isinstance(ctx.get("model_tiers"), dict) else {}
    )
    secrets = tuple(
        str(value)
        for value in (
            api_key,
            *(
                tier.get("api_key")
                for tier in configured_tiers.values()
                if isinstance(tier, dict)
            ),
        )
        if value
    )
    breakers = _breaker_for(ctx)
    rate_limit_retries = (
        0 if not idempotent else _as_int(ctx.get("rate_limit_retries"), 4)
    )
    backoff = float(ctx.get("rate_limit_backoff_s", 15.0))
    # AGT-08: the effort plan is resolved PER CANDIDATE and cached, so a
    # refused row and the dialed row for the same target can never disagree
    # about what was sent.
    effort_plans: Dict[int, model_capabilities.EffortPlan] = {}

    def plan_for(candidate: "Candidate") -> model_capabilities.EffortPlan:
        """Return (and memoise) the effort plan for one candidate target."""
        key = int(getattr(candidate, "index", -1))
        existing = effort_plans.get(key)
        if existing is not None:
            return existing
        plan = model_router._effort_plan(
            ctx,
            {
                "provider": candidate.provider,
                "model": candidate.model,
            },
            effort,
        )
        effort_plans[key] = plan
        return plan

    # R2-14: the budget governor is supplied by the worker through the router
    # context, or is the one installed for this execution context. It is
    # OPTIONAL in exactly the way the resilience pipeline is: with no
    # governor this function's retry/backoff behaviour is the governed loop
    # with no exemption writer and no budget check, which is still stricter
    # than the historical `_completion_with_retry` (quota is terminal).
    governor = ctx.get("budget_governor")
    if governor is None:
        governor = budget_governor.current_governor()
    if not isinstance(governor, budget_governor.BudgetGovernor):
        governor = None
    cache_ledger = prompt_cache.current_cache_ledger()
    cache_enabled = _as_bool(ctx.get("prompt_cache"), True)
    attempt_index = 0
    attempts: List[Dict[str, Any]] = []
    last_error: Optional[BaseException] = None

    def record(
        candidate: Candidate,
        outcome: str,
        *,
        error: Optional[str] = None,
        retry: Optional[Dict[str, Any]] = None,
        skip_reason: Optional[str] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float = 0.0,
        cost_source: str = "unknown",
        stop_reason: Optional[str] = None,
        cache_usage: Any = None,
        cache_plan: Any = None,
        capabilities: Optional[Dict[str, Any]] = None,
        effort_plan: Any = None,
    ) -> None:
        nonlocal attempt_index
        attempt_index += 1
        if cache_plan is not None:
            cache_ledger.record(
                cache_plan,
                prompt_cache.receipt_from_usage(cache_usage, cache_plan),
                model=str(candidate.model or ""),
                api_base_sha256=local_models.endpoint_fingerprint(candidate.api_base),
            )
        record_dict: Dict[str, Any] = {
            "ts": model_router.now_iso(),
            "call_id": call_id,
            "attempt": attempt_index,
            "outcome": outcome,
            "model": candidate.model or "",
            "provider": candidate.provider or "",
            "tier": candidate.target.get("routed_via_hint"),
            "tier_class": candidate.tier_class,
            "provider_identity": candidate.identity,
            "candidate_index": candidate.index,
            "api_base_sha256": local_models.endpoint_fingerprint(candidate.api_base),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tokens": prompt_tokens + completion_tokens,
            "cost_usd": round(float(cost), 8),
            "cost_source": cost_source,
            "elapsed_s": round(time.time() - started, 3),
            "routed_via_hint": candidate.target.get("routed_via_hint"),
            "difficulty_hint": normalized_hint,
            "fallback_used": candidate.index > 0,
            "idempotent": bool(idempotent),
            "breaker_state": candidate.breaker_state,
            "local_first": bool(local_applied),
            "offline": bool(offline_receipt and offline_receipt.get("offline")),
            "privacy": candidate.privacy.as_dict()
            if candidate.privacy is not None
            else None,
            "privacy_policy": staged.policy.name,
            "redaction": dict(staged.redaction),
        }
        if candidate.zdr_kwargs:
            record_dict["zdr_requested"] = True
            record_dict["zdr_parameter"] = (
                candidate.privacy.zdr_parameter
                if candidate.privacy is not None
                else sorted(candidate.zdr_kwargs)
            )
        if retry:
            record_dict["retry"] = retry
        if skip_reason:
            record_dict["skipped_reason"] = skip_reason
        if error:
            record_dict["error"] = error
        if stop_reason:
            record_dict["stop_reason"] = str(stop_reason)
        if capabilities is not None:
            record_dict["context_window"] = int(capabilities["context_window"])
            record_dict["context_window_source"] = capabilities["context_window_source"]
        if effort_plan is not None:
            # AGT-08: the per-candidate effort plan, on EVERY row for this
            # candidate including skips and failures. A refusal row that omits
            # it would read as "no effort was ever asked for".
            record_dict.update(effort_plan.to_dict())
        else:
            record_dict.update(plan_for(candidate).to_dict())
        if cache_plan is not None:
            record_dict["cache_requested"] = bool(cache_plan.requested)
            record_dict["cache_skip_reason"] = cache_plan.skip_reason
            record_dict["cache_provider_family"] = cache_plan.provider_family
        attempts.append(
            {
                "index": candidate.index,
                "identity": candidate.identity,
                "provider": candidate.provider,
                "model": candidate.model,
                "outcome": outcome,
                "error": error,
                "skip_reason": skip_reason,
            }
        )
        model_router._record_usage(record_dict, ctx, ledger)

    # 3. Record the refusals BEFORE dialing anything: a chain that a policy
    #    blocked must be legible even when it then succeeded on a later target.
    for candidate in staged.screen.refused:
        record(
            candidate,
            "skipped",
            error=model_router._safe_error(
                RuntimeError(
                    candidate.skip_detail or candidate.skip_reason or "refused"
                ),
                secrets,
            ),
            skip_reason=candidate.skip_reason,
        )
    if not staged.permitted():
        _raise_for_empty_chain(staged, idempotent=idempotent)

    # 4. Dial each permitted candidate in order.
    permitted = staged.permitted()
    for position, candidate in enumerate(permitted):
        has_next = position + 1 < len(permitted)
        cache_plan = prompt_cache.plan_cache(
            staged.messages,
            tools,
            provider=candidate.provider,
            model=str(candidate.model or ""),
            enabled=cache_enabled,
            min_prefix_tokens=int(
                ctx.get("prompt_cache_min_prefix_tokens")
                or prompt_cache.DEFAULT_MIN_PREFIX_TOKENS
            ),
            breakpoint_index=ctx.get("prompt_cache_breakpoint"),
        )
        request_messages, request_tools = prompt_cache.apply_cache_parameters(
            staged.messages, tools, cache_plan
        )
        capabilities = model_capabilities.resolve_context_window(
            str(candidate.model or ""),
            provider=candidate.provider,
            api_base=candidate.api_base,
            probe=ctx.get("context_window_probe")
            if callable(ctx.get("context_window_probe"))
            else None,
        )
        capabilities = {**capabilities, "tier_class": candidate.tier_class}
        kwargs: Dict[str, Any] = {
            "model": str(candidate.model or ""),
            "messages": request_messages,
        }
        if candidate.provider:
            prefix = f"{candidate.provider}/"
            kwargs["model"] = (
                str(candidate.model)
                if str(candidate.model).startswith(prefix)
                else f"{prefix}{candidate.model}"
            )
        effective_key = api_key or candidate.target.get("api_key") or ctx.get("api_key")
        effective_base = candidate.api_base or ctx.get("api_base")
        if effective_key:
            kwargs["api_key"] = effective_key
        if effective_base:
            kwargs["api_base"] = effective_base
        if ctx.get("max_completion_tokens"):
            kwargs["max_tokens"] = int(ctx["max_completion_tokens"])
        if request_tools:
            kwargs["tools"] = list(request_tools)
            if tool_choice:
                kwargs["tool_choice"] = str(tool_choice)
        for key, value in (candidate.zdr_kwargs or {}).items():
            kwargs.setdefault(key, value)
        # AGT-08: this candidate's OWN effort plan decides the request. A
        # candidate whose model has no knob contributes nothing, so a fallback
        # onto an unsupported target is reported rather than silently ignored.
        if plan_for(candidate).sent:
            kwargs.update(plan_for(candidate).parameters)

        if ctx.get("use_mock_provider"):
            try:
                content = model_router._mock_call(request_messages, candidate.target)
            except Exception as exc:
                last_error = exc
                record(
                    candidate,
                    "error",
                    error=model_router._safe_error(exc, secrets),
                    cache_plan=cache_plan,
                    capabilities=capabilities,
                )
                breakers.record_failure(candidate.identity, "mock_provider_error")
                continue
            prompt_tokens = (
                10
                + sum(
                    len(str(message.get("content", ""))) for message in request_messages
                )
                // 4
            )
            completion_tokens = len(content) // 4
            cost, cost_source = model_router._fallback_cost(
                candidate.target, prompt_tokens, completion_tokens
            )
            breakers.record_success(candidate.identity)
            record(
                candidate,
                "success",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=cost,
                cost_source=cost_source,
                cache_plan=cache_plan,
                capabilities=capabilities,
            )
            if governor is not None:
                governor.commit(cost)
            return content

        # R2-14 per-call budget: price THIS call and refuse it before the
        # provider is touched when it cannot fit. The attempt-level check in
        # harness.core stays as the backstop; this is what tightens the cap
        # from "cap + one whole attempt" to "cap + at most one call".
        reservation = 0.0
        if governor is not None:
            verdict = governor.authorize_call(
                messages=request_messages,
                target=candidate.target,
                # The caller's DECLARED completion bound, when it declared
                # one. A sound bound makes the reservation a sound bound;
                # without it the enforced guarantee is "cap + the price of
                # the final call", which is the honest statement.
                max_completion_tokens=ctx.get("max_completion_tokens"),
            )
            if not verdict.allowed:
                # Recorded with the measured numbers and then raised BEFORE
                # a request is built. The caller decides whether the run
                # ends; this module refuses to dial.
                record(
                    candidate,
                    "skipped",
                    error=verdict.reason,
                    skip_reason="budget_refused",
                    capabilities=capabilities,
                )
                raise budget_governor.BudgetRefused(verdict)
            reservation = verdict.reserved_usd

        try:
            # The default argument pins THIS iteration's request: the
            # governed loop may dial several times, and a late-bound
            # `kwargs` would be a different request than the one recorded.
            response = budget_governor.governed_completion(
                lambda _request=kwargs: model_router._litellm_completion()(**_request),
                max_retries=rate_limit_retries,
                base_backoff_s=backoff,
                governor=governor,
                idempotent=idempotent,
                on_attempt_failure=lambda _attempt, _elapsed, _exc: None,
                started=started,
            )
        except budget_governor.QuotaExhausted as exc:
            # Terminal: no retry (the governed loop already spent zero), no
            # fallback, no breaker trip. A billing wall is not an outage.
            record(
                candidate,
                "error",
                error=exc.failure.reason,
                skip_reason="quota_exhausted",
                cache_plan=cache_plan,
                capabilities=capabilities,
            )
            raise
        except Exception as exc:
            last_error = exc
            decision = classify_outcome(
                exc,
                idempotent=idempotent,
                attempts_used=rate_limit_retries,
                max_retries=rate_limit_retries,
            )
            if decision.provider_fault:
                breakers.record_failure(candidate.identity, decision.kind)
            record(
                candidate,
                "error",
                error=model_router._safe_error(exc, secrets),
                retry=decision.as_dict(),
                cache_plan=cache_plan,
                capabilities=capabilities,
            )
            if not idempotent or not has_next:
                # A non-idempotent operation is never handed to another
                # provider, and with no candidate left the original
                # exception is the honest thing to raise.
                raise
            continue
        finally:
            if governor is not None:
                # The reservation is settled either way; a leaked reservation
                # would make the cap fire early and lie about the budget.
                governor.release(reservation)

        breakers.record_success(candidate.identity)
        prompt_tokens, completion_tokens, reported_cost = model_router._extract_usage(
            response
        )
        cache_usage = getattr(response, "usage", None)
        cached_tokens, _creation, _write, _reported = (
            prompt_cache.cache_tokens_from_usage(cache_usage)
        )
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        finish_reason = getattr(choice, "finish_reason", None)
        tool_calls = model_router._extract_tool_calls(response)
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        if not content and not tool_calls:
            record(
                candidate,
                "error",
                error="provider returned empty assistant content",
                stop_reason=str(finish_reason or "empty_response"),
                cache_usage=cache_usage,
                cache_plan=cache_plan,
                capabilities=capabilities,
            )
            if not has_next:
                raise RuntimeError("provider returned empty assistant content")
            continue
        if reported_cost is None:
            cost, cost_source = model_router._fallback_cost(
                candidate.target, prompt_tokens, completion_tokens, cached_tokens
            )
        else:
            cost, cost_source = reported_cost, "provider"
        if governor is not None:
            # R2-14: the harness's ModelClient only ever sees the LAST
            # call's usage, so a fallback chain's earlier charges are
            # invisible to it. The governor accumulates every charge, and
            # `spent_usd` takes the MAX of the two, so the cap can only
            # fire earlier than either source alone -- never later.
            governor.commit(cost, model=str(candidate.model or ""))
        record(
            candidate,
            "success",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            cost_source=cost_source,
            stop_reason=finish_reason,
            cache_usage=cache_usage,
            cache_plan=cache_plan,
            capabilities=capabilities,
        )
        if tool_calls:
            return {
                "content": content,
                "text": content,
                "tool_calls": tool_calls,
                "finish_reason": str(finish_reason or ""),
            }
        return content

    if last_error is not None:
        raise last_error
    raise RuntimeError(
        "no provider target produced a response; attempts: "
        + json_safe_attempts(attempts)
    )


def json_safe_attempts(attempts: Sequence[Mapping[str, Any]]) -> str:
    """Return a compact, credential-free summary of a failed chain."""
    return (
        "; ".join(
            f"#{item.get('index')} {item.get('provider')}/{item.get('model')} "
            f"{item.get('outcome')}"
            + (f" [{item.get('skip_reason')}]" if item.get("skip_reason") else "")
            for item in attempts
        )
        or "none"
    )


def _raise_for_empty_chain(staged: StagedCall, *, idempotent: bool) -> None:
    """Raise the honest error for a chain with no dialable target.

    Offline refusals and privacy refusals produce DIFFERENT typed errors,
    because they call for different operator action: one means "you asked
    for no egress", the other means "this policy forbids this provider".
    Collapsing them into one error is how "it silently did nothing" bugs
    are born.
    """
    offline_refusals = [
        item
        for item in staged.screen.refused
        if item.skip_reason == "offline_egress_blocked"
    ]
    if offline_refusals:
        first = offline_refusals[0]
        raise offline_mode.OfflineEgressBlocked(
            first.provider, first.model, first.api_base, reason="offline:all_targets"
        )
    decisions = [
        item.privacy for item in staged.screen.refused if item.privacy is not None
    ]
    if decisions:
        raise privacy_policy.PrivacyPolicyBlocked(decisions)
    breaker_refusals = [
        item
        for item in staged.screen.refused
        if item.skip_reason != "offline_egress_blocked"
    ]
    raise RuntimeError(
        "no provider target was dialable ("
        + ", ".join(f"{item.identity}:{item.skip_reason}" for item in breaker_refusals)
        + "); no network request was made"
    )


def _new_call_id() -> str:
    import uuid

    return uuid.uuid4().hex
