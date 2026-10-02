"""Boundary 2 (INTERFACES.md): the model/provider abstraction layer.

`call_model(messages, difficulty_hint, provider, model, api_key) -> str`
with litellm underneath so any provider works with a user-supplied key.
Every call's model, provider, tokens, and cost are recorded to a JSONL
ledger (cost transparency + reproducibility) and to get_last_usage() for
Terminal 1's ModelClient.

Routing logic:
  - Explicit provider+model always wins (caller knows best).
  - difficulty_hint + adaptive routing ON -> pick the tier's model
    (easy -> cheap, hard -> expensive).
  - Otherwise -> config default (passed through from task.config by the
    harness) or DEFAULT_MODEL_TIERS["medium"].

Prompt caching (VEX-CEILING-09): the request's frozen prefix — the leading
run of system messages plus the tool schemas — is digested by
`runtime.prompt_cache`, and the provider is asked to cache it (Anthropic-
family endpoints get an explicit `cache_control` breakpoint; OpenAI-
compatible and Google endpoints cache a stable prefix implicitly, so an
unknown parameter is never sent to them). Every ledger row and every
`model_routed` trace event carries the cache receipt: status, cached input
tokens, cache-creation tokens, and the prefix digest. A provider that
reports no cache field is recorded as `unreported`, never as a hit.
Context windows: `runtime.model_capabilities` resolves and CACHES the
model's window per (base URL, model), degrading to a documented positive
floor rather than to zero. The resolved window and its source ride the
ledger row so a context-budget consumer can tell a measured window from
a floor.

Capability registry and calibrated routing (R2-13): the same module is the
ONE authority for a model's context window, tool-calling support,
reasoning-content support, streaming support, and PRICE. Two rules follow
from that authority and are enforced HERE, before the provider is touched:

  * **A model with no price row is `unpriced`, never `$0`.** The price
    ladder picks the cheapest tier by comparing numbers, so an absent row
    compared as zero makes an unpriced model the CHEAPEST one and the cost
    report under-reports in the direction that spends money. Every ledger
    row and `get_last_usage()` record therefore carries `price_state`
    (`priced` | `free` | `unpriced`) and `cost_priced` next to `cost_usd`.
  * **A model that cannot emit tool calls is not selected for a
    tool-driven loop, regardless of price.** That is a capability
    constraint, not a preference, and it escalates to a more capable tier
    rather than downgrading.

The gate is strictly OPT-IN by KEY PRESENCE (`capability_gate` and friends
in `_CAPABILITY_CONFIG_KEYS`); nothing is in `DEFAULTS`, so an unconfigured
caller keeps the byte-identical pre-R2-13 path and its receipt records that
the gate was considered and found off. With the gate on, a router-chosen
unpriced target is REFUSED (a caller may name the model explicitly, or set
`capability_allow_unpriced`, to opt in) and having nothing eligible raises
`model_capabilities.CapabilityRoutingRefused` rather than quietly handing
the cheapest-looking tier to a loop that cannot use it.

Reasoning content (R2-14 protocol): a response whose visible content is
empty because the completion budget went to `reasoning_content` is recorded
as the NAMED outcome `truncated_reasoning`, distinct from a genuinely empty
response. Reasoning text is never substituted for the answer.

Effort ladder (AGT-08): `effort` is a first-class call/context keyword
(`auto | low | medium | high | xhigh | max`). It is mapped by
`runtime.model_capabilities.map_effort` onto the target's REAL provider
parameter, and the resulting `EffortPlan` rides every ledger row, every
`get_last_usage()` record and every `model_routed` trace event, so "set to
high" is either a parameter on the request or an explicit
`unsupported_*` report. Effort changes how hard the model thinks and NEVER
whether the answer was verified; nothing on this path reads a completion
status.


Local-first tier (VEX-CEILING-14): `runtime.local_models` resolves an
OPT-IN local model profile. When one is configured, the cheap hints
(retrieval / summarization / boilerplate) route to it and only the decisive
step (`hard`) escalates to a frontier model. Every resolved target is
classified `local` or `frontier` from its ENDPOINT, not its name, and the
split (tokens + cost per class) rides every ledger row and trace event, so
"how much of this run stayed local" is measurable rather than asserted.

Provider resilience (VEX-CEILING-14): `runtime.provider_resilience` supplies
a per-provider circuit breaker, a BOUNDED fallback chain, and an
idempotency-aware retry classifier. A provider outage no longer ends a
recoverable task: the breaker stops the call from being re-dialed, the chain
tries the next configured target, and the refusal is recorded rather than
swallowed. A caller-declared non-idempotent operation is never replayed.

Offline + privacy (VEX-CEILING-14): `runtime.offline_mode` refuses any
remote target before a request is built, and `runtime.privacy_policy`
decides what may leave the machine and REDACTS the outgoing messages on
every request. A privacy refusal and an offline refusal are both recorded on
the ledger row and in the trace.

Config wiring: the harness passes provider/model/api_key in task.config;
the router reads task.config-derived entries from an execution-context-local
value (see set_call_context / runtime/worker.py). Difficulty prediction for
unrated steps (hint=None but routing on) happens HERE via
runtime.difficulty, so the harness never needs routing code.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from shared import tracing

from . import latency, model_capabilities, prompt_cache, streaming
from .config import DEFAULT_MODEL_TIERS
from .fsutil import append_jsonl, now_iso
from .redaction import redact_provider_text

HINTS = ("easy", "medium", "hard")

# The ONE price table (USD per 1M tokens as ``(input, output)``). The authority
# moved to ``runtime.model_capabilities`` (R2-13) so the routing ladder and the
# cost report read the SAME rows; this name is kept as a re-export of that exact
# object because ``runtime/ablation.py`` and the cost tests address it here.
#
# A model's ABSENCE from this table means ``unpriced`` — it is never ``$0``.
# ``_fallback_cost`` reports the state alongside the number so a call that was
# never priced cannot be reported as a free call.
_PRICES: Dict[str, tuple] = model_capabilities.MODEL_PRICES

_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "neo_model_router_context", default=None
)
_LEDGER_PATH: contextvars.ContextVar[Optional[Path]] = contextvars.ContextVar(
    "neo_model_router_ledger", default=None
)
_LAST_USAGE: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "neo_model_router_last_usage", default=None
)
_ESTIMATOR_GUARD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "neo_model_router_estimator_guard", default=False
)


def get_cache_summary() -> Dict[str, Any]:
    """Return this execution context's prompt-cache ledger summary.

    The cache hit rate is a per-context fact (concurrent tasks must not share
    one), so the answer comes from the context-local ledger. An empty context
    yields honest zeros rather than raising.
    """
    return prompt_cache.current_cache_ledger().summary()


def set_call_context(
    config: Optional[Dict[str, Any]], ledger_dir: Optional[str] = None
) -> None:
    """Install isolated router config for the current thread or task.

    Assumes callers that run concurrently set their own context before
    calling the model. Passing None clears only the current execution
    context. ``ledger_dir`` enables that context's JSONL call ledger. A fresh
    prompt-cache ledger is installed with the context so cache accounting
    starts at zero for the new task rather than inheriting a previous one.
    """
    normalized = dict(config or {})
    if isinstance(normalized.get("provider_profile"), dict):
        normalized["provider_profile"] = normalize_provider_profile(
            normalized["provider_profile"]
        )
    _CONTEXT.set(normalized)
    _LEDGER_PATH.set(Path(ledger_dir) if ledger_dir else None)
    _LAST_USAGE.set({})
    prompt_cache.set_cache_ledger(None)


def normalize_provider_profile(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize a provider profile for the router's context contract.

    ``base_url`` is accepted as an additive alias for ``api_base`` and
    named OpenAI-compatible routers become ``openai`` when an endpoint is
    present. The public ``call_model`` signature is unchanged.
    """
    out = dict(profile or {})
    base = out.pop("base_url", None)
    if base and not out.get("api_base"):
        out["api_base"] = base
    provider = str(out.get("provider") or "").strip().lower()
    if out.get("api_base") and provider in {"agentrouter", "openrouter", "tokenrouter"}:
        out["provider"] = "openai"
    return out


def get_last_usage() -> Dict[str, Any]:
    """Return usage from this execution context's most recent model call.

    The result is empty before a call, and concurrent callers never observe
    one another's last-usage record.
    """
    return dict(_LAST_USAGE.get() or {})


def _norm_hint(hint: Optional[str]) -> Optional[str]:
    """Normalize a caller-supplied difficulty hint to easy/medium/hard.

    Boundary 2 documents "easy" | "hard" | None; "medium" is our added
    middle tier. Unknown/None hints -> None (meaning: don't route).
    """
    if hint is None:
        return None
    h = str(hint).strip().lower()
    if h in HINTS:
        return h
    return None


def _resolve_target(
    difficulty_hint: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Decide the (provider, model, api_key, api_base, routed) target.

    Precedence: explicit provider/model > tier table for hint > context
    defaults > built-in medium tier. A model_tiers entry may carry its own
    api_key/api_base (e.g. routing across different providers' endpoints —
    each tier hits its own gateway); context-level api_key/api_base apply
    to non-routed calls. Returns dict for logging; never raises on
    unknown hints.
    """
    ctx = dict(ctx or {})
    profile = ctx.get("provider_profile")
    if not isinstance(profile, dict):
        profile = {}
    tiers = ctx.get("model_tiers") or profile.get("model_tiers") or DEFAULT_MODEL_TIERS
    if not isinstance(tiers, dict):
        tiers = DEFAULT_MODEL_TIERS

    explicit_model = model or ctx.get("model") or profile.get("model")
    explicit_provider = provider or ctx.get("provider") or profile.get("provider")
    profile_base = profile.get("api_base") or profile.get("base_url")
    context_base = ctx.get("api_base") or profile_base
    if (
        isinstance(explicit_provider, str)
        and context_base
        and explicit_provider.strip().lower()
        in {"agentrouter", "openrouter", "tokenrouter"}
    ):
        explicit_provider = "openai"

    hint = _norm_hint(difficulty_hint)
    if hint and ctx.get("adaptive_routing") and not explicit_model:
        # Adaptive routing picks the tier model (explicit always wins).
        tier = tiers.get(hint)
        if isinstance(tier, dict):
            tier_model = tier.get("model")
            if tier_model:
                return {
                    "provider": provider or tier.get("provider") or explicit_provider,
                    "model": tier_model,
                    "routed_via_hint": hint,
                    "api_key": tier.get("api_key") or profile.get("api_key"),
                    "api_base": tier.get("api_base")
                    or tier.get("base_url")
                    or context_base,
                    "input_cost_per_million": tier.get("input_cost_per_million"),
                    "output_cost_per_million": tier.get("output_cost_per_million"),
                    "display_label": profile.get("display_label")
                    or profile.get("label"),
                    "source_tier": profile.get("source_tier") or ctx.get("source_tier"),
                }
    resolved_model = explicit_model or DEFAULT_MODEL_TIERS["medium"]["model"]
    configured_prices = ctx.get("model_prices")
    price_entry = (
        configured_prices.get(resolved_model)
        if isinstance(configured_prices, dict)
        else None
    )
    if not isinstance(price_entry, dict):
        price_entry = {}
    return {
        "provider": explicit_provider,
        "model": resolved_model,
        "routed_via_hint": None if explicit_model else "medium-default",
        "api_key": profile.get("api_key") or ctx.get("api_key"),
        "api_base": context_base,
        "display_label": profile.get("display_label") or profile.get("label"),
        "source_tier": profile.get("source_tier") or ctx.get("source_tier"),
        "input_cost_per_million": price_entry.get("input_cost_per_million"),
        "output_cost_per_million": price_entry.get("output_cost_per_million"),
    }


#: Task.config keys that switch ``call_model`` onto the R2-13 capability gate.
#: Exactly like ``_RESILIENCE_CONFIG_KEYS`` below, this is a KEY test and not a
#: VALUE test, and NOTHING here is in ``DEFAULTS``: a value in DEFAULTS is
#: merged into every task config, so a truthy default would silently switch
#: every task and every eval arm onto a gate that can REFUSE a call. "Absent"
#: is the meaningful state that means unchanged behaviour.
_CAPABILITY_CONFIG_KEYS = (
    "capability_gate",
    "capability_allow_unpriced",
    "capability_strict_tools",
    "capability_tool_driven",
    "model_capabilities",
)


def _capability_requested(ctx: Dict[str, Any]) -> bool:
    """True when the caller configured any R2-13 capability behaviour.

    Deliberately a KEY test, mirroring :func:`_resilience_requested`: a config
    carrying ``capability_gate: False`` still opts in, because the operator who
    wrote the key wants the pipeline (and its receipts) and explicitly turned
    the gate off inside it.
    """
    return any(key in ctx for key in _CAPABILITY_CONFIG_KEYS)


def _declared_capabilities(ctx: Dict[str, Any]) -> None:
    """Register any capability rows the caller declared in the task config.

    ``Task.config["model_capabilities"]`` accepts a mapping keyed by model name
    (or a list of row mappings) and is the seam for bring-your-own-model
    operators: a model nobody has described is UNKNOWN, and the honest fix is
    for the operator to say what it is rather than for the harness to guess.
    A malformed row raises ``CapabilityError`` — a broken declaration is
    refused loudly rather than silently leaving the model unknown.
    """
    declared = ctx.get("model_capabilities")
    if not declared:
        return
    if isinstance(declared, Mapping):
        rows = []
        for name, value in declared.items():
            if isinstance(value, Mapping):
                rows.append({**value, "model": value.get("model") or name})
    elif isinstance(declared, list):
        rows = [row for row in declared if isinstance(row, Mapping)]
    else:
        raise model_capabilities.CapabilityError(
            "model_capabilities must be a mapping or a list of row mappings, "
            f"got {type(declared).__name__}",
            reason="invalid_declaration",
        )
    for row in rows:
        model_capabilities.register_capability(row, source="config")


def _tier_candidates(
    target: Dict[str, Any], hint: Optional[str], ctx: Dict[str, Any]
) -> list:
    """Return the ordered candidate list the capability screen may choose from.

    The router's own choice is FIRST (its economics are the caller's), followed
    by the remaining tier-table entries in ASCENDING difficulty order. That
    order is what makes a capability refusal an ESCALATION rather than a
    downgrade: a tool-incapable cheap model hands the call to a more capable
    tier instead of to a cheaper-but-also-incapable one.
    """
    candidates = [dict(target)]
    ctx = dict(ctx or {})
    profile = ctx.get("provider_profile")
    if not isinstance(profile, Mapping):
        profile = {}
    tiers = ctx.get("model_tiers") or profile.get("model_tiers") or DEFAULT_MODEL_TIERS
    if not isinstance(tiers, dict):
        tiers = DEFAULT_MODEL_TIERS
    start = HINTS.index(hint) if hint in HINTS else 0
    for name in HINTS[start:]:
        tier = tiers.get(name)
        if not isinstance(tier, Mapping):
            continue
        model = tier.get("model")
        if not model:
            continue
        if any(str(c.get("model")) == str(model) for c in candidates):
            continue
        candidates.append(
            {
                "provider": tier.get("provider") or target.get("provider"),
                "model": model,
                "routed_via_hint": name,
                "api_key": tier.get("api_key") or target.get("api_key"),
                "api_base": tier.get("api_base")
                or tier.get("base_url")
                or target.get("api_base"),
                "input_cost_per_million": tier.get("input_cost_per_million"),
                "output_cost_per_million": tier.get("output_cost_per_million"),
                "display_label": target.get("display_label"),
                "source_tier": target.get("source_tier"),
            }
        )
    return candidates


def _capability_screen(
    target: Dict[str, Any],
    ctx: Dict[str, Any],
    *,
    hint: Optional[str],
    tool_driven: bool,
    explicit_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Screen the router's target against the capability registry (R2-13).

    Returns a receipt ALWAYS (``enabled`` False when the gate is off) so the
    ledger records that the gate was considered and found off, which is
    distinguishable from a run where nobody looked. When the gate is on and a
    candidate is refused, the receipt names every rejection and
    ``runtime.model_capabilities.CapabilityRoutingRefused`` propagates if
    nothing is eligible — the router fails closed rather than handing a
    tool-incapable or unpriced model a tool-driven loop.

    An EXPLICITLY NAMED model is exempt from the unpriced refusal only: the
    prompt is to refuse to *route* on an unpriced model, and a caller who
    pinned the model — at call level or through the context/profile — is not
    routing. The exemption is recorded as ``explicit_unpriced_bypass`` and
    never silent. The tool constraint still applies to an explicit model,
    because a loop that needs tool calls cannot use a model that cannot make
    them regardless of who named it.
    """
    receipt: Dict[str, Any] = {
        "enabled": _capability_requested(ctx),
        "gate": "capability_gate",
        "tool_driven": bool(tool_driven),
        "refusals": [],
        "refusal_count": 0,
        "considered": [],
        "explicit_unpriced_bypass": False,
    }
    if not receipt["enabled"]:
        return receipt
    _declared_capabilities(ctx)
    profile = ctx.get("provider_profile")
    if not isinstance(profile, Mapping):
        profile = {}
    explicit = bool(explicit_model or ctx.get("model") or profile.get("model"))
    allow_unpriced = bool(ctx.get("capability_allow_unpriced")) or explicit
    strict_tools = bool(ctx.get("capability_strict_tools"))
    candidates = _tier_candidates(target, hint, ctx)
    try:
        screen = model_capabilities.screen_candidates(
            candidates,
            tool_driven=bool(tool_driven),
            allow_unpriced=allow_unpriced,
            strict_tools=strict_tools,
        )
    except model_capabilities.CapabilityRoutingRefused as refusal:
        receipt["refusals"] = list(refusal.alternatives)
        receipt["refusal_count"] = len(refusal.alternatives)
        receipt["refused_reason"] = refusal.reason
        receipt["considered"] = [
            {"provider": row.get("provider"), "model": row.get("model")}
            for row in candidates
        ]
        raise
    receipt["refusals"] = [item.to_dict() for item in screen.refusals]
    receipt["refusal_count"] = len(screen.refusals)
    receipt["considered"] = [dict(row) for row in screen.considered]
    receipt["explicit_unpriced_bypass"] = bool(explicit)
    receipt["allow_unpriced"] = bool(allow_unpriced)
    receipt["strict_tools"] = bool(strict_tools)
    receipt["selected_model"] = screen.selected.get("model")
    receipt["selected_provider"] = screen.selected.get("provider")
    receipt["routed_up"] = bool(
        screen.selected.get("model")
        and target.get("model")
        and screen.selected.get("model") != target.get("model")
    )
    return receipt


def _structural_calibrated() -> bool:
    """Whether a HELD-OUT-VALIDATED structural calibration exists.

    The R2-13 discipline is that the structural predictor is the CHALLENGER and
    only replaces the incumbent on evidence. This reads the sibling artifact
    ``runtime/difficulty_structural_calibration.json``, which is written only
    by :func:`python -m evals.difficulty_holdout --apply` and only when
    ``compare_predictors`` reports ``ship: true``. No such file ships, so
    ``difficulty_features="auto"`` resolves to the incumbent.
    """
    try:
        path = Path(__file__).with_name("difficulty_structural_calibration.json")
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        bands = data.get("bands")
        return bool(
            data.get("shipped") is True
            and isinstance(bands, list)
            and len(bands) == 2
            and all(isinstance(value, int) for value in bands)
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _maybe_predict_difficulty(messages: list, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Predict difficulty from message content when routing is ON but no
    hint was supplied by the caller.

    This is the novel mechanism's ingress: the harness (or any Boundary-2
    caller) can pass difficulty_hint=None and the router still adapts. The
    "llm" estimator's own model call runs with the recursion guard set so
    it can't re-enter prediction; on any estimator failure the empty dict
    is returned (call proceeds with the default tier — routing never blocks
    a task).
    """
    if not ctx.get("adaptive_routing"):
        return {}
    if _ESTIMATOR_GUARD.get():
        return {}
    estimator = ctx.get("difficulty_estimator") or "heuristic"
    if estimator == "off":
        return {}
    if not any(
        isinstance(m, dict) and str(m.get("content", "")).strip() for m in messages
    ):
        return {}
    from .difficulty import (
        _first_user_content,
        _issue_text_from,
        predict_difficulty,
        predict_structural,
        routing_text,
    )

    selector = str(ctx.get("difficulty_features") or "").strip().lower()
    if selector == "auto":
        # The structural predictor becomes reachable through "auto" ONLY once
        # a held-out-validated calibration exists. None ships, so "auto" is the
        # incumbent today and the fallback is recorded rather than silent.
        selector = "structural" if _structural_calibrated() else ""
    if selector == "structural":
        guard_token = _ESTIMATOR_GUARD.set(True)
        try:
            context = {
                "issue_text": _issue_text_from(_first_user_content(messages)),
                "repo_path": ctx.get("repo_path"),
                "target_test": ctx.get("target_test"),
                "changed_files": ctx.get("changed_files"),
                "touched_symbols": ctx.get("touched_symbols"),
            }
            hint, info = predict_structural(context)
        except Exception as exc:  # a challenger must never break a task
            # Redacted before it reaches the ledger row. `model_ledger.jsonl`
            # is appended through the RAW `fsutil.append_jsonl` -- no redaction
            # at the writer -- so an unredacted value here is durable on disk.
            # This is the one non-numeric free-text field the ledger carries,
            # and it is also the only one that was not going through the
            # provider path, which is exactly why it was easy to leave alone.
            info = {
                "estimator": "structural",
                "fallback_reason": redact_provider_text(
                    f"{type(exc).__name__}: {exc}",
                    limit=200,
                    label="structural estimator fallback",
                ),
                "features": {},
            }
            hint, base = predict_difficulty(
                routing_text(messages),
                estimator="heuristic",
                llm_cfg=ctx.get("difficulty_llm"),
                messages=messages,
            )
            info = {**info, "fallback_hint": base}
        finally:
            _ESTIMATOR_GUARD.reset(guard_token)
        return {"hint": hint, "info": info}

    guard_token = _ESTIMATOR_GUARD.set(True)
    try:
        hint, info = predict_difficulty(
            routing_text(messages),
            estimator=estimator,
            llm_cfg=ctx.get("difficulty_llm"),
            messages=messages,
        )
        return {"hint": hint, "info": info}
    finally:
        _ESTIMATOR_GUARD.reset(guard_token)


def _cost_record(
    target: Dict[str, Any],
    prompt_tokens: int,
    completion_tokens: int,
    cached_input_tokens: int = 0,
    provider_cost_usd: Optional[float] = None,
) -> model_capabilities.CostEstimate:
    """Return the call's cost AND the price state that makes it meaningful.

    Delegates the arithmetic to ``runtime.model_capabilities.estimate_cost``,
    which is the same authority the capability registry uses, so the number on
    the ledger and the state on the receipt can never disagree. A model with no
    price row yields ``cost_usd == 0.0`` with ``price_state == "unpriced"`` and
    ``priced is False`` — the honest "we do not know" instead of "$0".
    """
    return model_capabilities.estimate_cost(
        target.get("model"),
        prompt_tokens,
        completion_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_read_discount=prompt_cache.DEFAULT_CACHE_READ_DISCOUNT,
        input_cost_per_million=target.get("input_cost_per_million"),
        output_cost_per_million=target.get("output_cost_per_million"),
        provider_cost_usd=provider_cost_usd,
    )


def _fallback_cost(
    target: Dict[str, Any],
    prompt_tokens: int,
    completion_tokens: int,
    cached_input_tokens: int = 0,
) -> tuple[float, str]:
    """Estimate cost from configured prices or the shared price table.

    Cached input tokens are billed at ``cache_read_discount`` of the normal
    input rate (Anthropic's published 0.1x), so a cache hit produces a
    measurably cheaper call instead of a decorative counter. The source string
    says the discount was applied, so a reader never mistakes the estimate for
    a provider invoice — and an UNPRICED model now reports ``"unpriced"``
    instead of the old ``"unknown"``, which is a price STATE rather than an
    admission that a number was produced.

    The historical two-tuple return is unchanged so every existing caller (and
    ``runtime.provider_gateway``) keeps working; use :func:`_cost_record` when
    the price state itself matters.
    """
    record = _cost_record(target, prompt_tokens, completion_tokens, cached_input_tokens)
    return record.cost_usd, record.source


def _cost_fallback(
    target: Dict[str, Any],
    prompt_tokens: int,
    completion_tokens: int,
    cached_input_tokens: int = 0,
) -> tuple[float, str]:
    """Backward-compatible alias for the current fallback-cost helper."""
    return _fallback_cost(target, prompt_tokens, completion_tokens, cached_input_tokens)


def _safe_error(exc: BaseException, secrets: tuple[str, ...]) -> str:
    """Return a bounded, redacted provider error with known credentials removed.

    Delegates to :mod:`runtime.redaction`, which is the ONE runtime boundary
    where provider text becomes a product value. This function used to
    re-implement redaction with its own regexes; it now owns only the
    ``(exc, secrets)`` shape its callers already had, so there is one answer
    to "what is a secret" rather than two that can disagree.

    What the old implementation got right and is preserved: the known-secret
    VALUES are replaced, not just credential-shaped substrings -- a gateway
    that echoes a key back does not always echo it in a recognisable shape.
    What ``runtime.redaction`` adds, and why it is not optional here:

    * escapes are stripped BEFORE redaction, so a key carrying its own ANSI
      cannot hide inside one and be reassembled by the strip;
    * the result is LENGTH-CAPPED after redaction, because a multi-KB HTML
      error page is its own denial of service;
    * a broken or unavailable redactor produces ``(withheld: <reason>)``
      rather than the raw value.
    """
    return redact_provider_text(exc, secrets=secrets, label="provider error")


def _endpoint_fingerprint(api_base: Optional[str]) -> Optional[str]:
    """Return a stable non-secret identifier for a provider base URL."""
    if not api_base:
        return None
    return hashlib.sha256(str(api_base).encode("utf-8")).hexdigest()[:12]


def _record_usage(
    record: Dict[str, Any],
    ctx: Optional[Dict[str, Any]] = None,
    ledger: Optional[Path] = None,
) -> None:
    """Publish one model attempt to context-local usage, ledger, and trace.

    The optional context and ledger arguments preserve the legacy one-argument
    helper contract while allowing the production path to pass explicit
    values. The normalized trace event is always emitted, but a ledger write
    failure is never swallowed: it is re-raised after the trace so the cost
    record cannot silently disappear.
    """
    context = dict(_CONTEXT.get() or {}) if ctx is None else dict(ctx or {})
    selected_ledger = _LEDGER_PATH.get() if ledger is None else ledger
    _LAST_USAGE.set(dict(record))
    ledger_error: Optional[OSError] = None
    if selected_ledger is not None:
        try:
            append_jsonl(selected_ledger, record)
        except OSError as exc:
            ledger_error = exc
    task_id = str(context.get("task_id") or "")
    if task_id:
        tracing.emit(
            "runtime",
            "model_routed",
            task_id=task_id,
            **{
                key: record.get(key)
                for key in (
                    "call_id",
                    "attempt",
                    "outcome",
                    "model",
                    "provider",
                    "tier",
                    "tokens",
                    "cost_usd",
                    "elapsed_s",
                    "routed_via_hint",
                    "difficulty_hint",
                    "api_base_sha256",
                    # Prompt-cache receipt: the same numbers the ledger and
                    # /cost read, so the trace can explain a cost delta.
                    "cache_status",
                    "cache_hit",
                    "cached_input_tokens",
                    "cache_creation_input_tokens",
                    "cache_prefix_sha256",
                    "context_window",
                    "context_window_source",
                    # R2-13: the price STATE and the capability receipt, so a
                    # trace reader can tell an unpriced call from a free one
                    # and can see which model was refused and why.
                    "price_state",
                    "cost_priced",
                    "capability_refusal_count",
                    "reasoning_content_present",
                    "reasoning_tokens",
                    # AGT-08: which effort level was asked for, whether a real
                    # provider parameter was sent, and which one. A trace
                    # reader must be able to tell "high" from "high, ignored".
                    "effort",
                    "effort_status",
                    "effort_sent",
                    "effort_parameter",
                    "effort_model",
                    "effort_detail",
                    # P1/W1 (T3): latency, with the STATES. A trace reader
                    # that sees `latency_ttft_s: null` must be able to tell
                    # "not measured" from "measured as nothing", so the state
                    # and the reason ride beside the value.
                    "latency_ttft_s",
                    "latency_ttft_state",
                    "latency_ttft_reason",
                    "latency_duration_s",
                    "latency_overhead_s",
                    "latency_overhead_state",
                    "latency_overhead_reason",
                    "latency_provider_s",
                    "latency_provider_state",
                    "latency_import_wait_s",
                    "latency_input_tokens_per_s",
                    "latency_output_tokens_per_s",
                )
            },
        )
    if ledger_error is not None:
        raise ledger_error


def _extract_usage(response: Any) -> tuple:
    """Pull (prompt_tokens, completion_tokens, cost_usd|None) from a
    litellm response. Assumes a litellm ModelResponse; falls back to
    0 tokens and a price-table cost when the shape is unexpected."""
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    cost = None
    # litellm exposes cost via response._hidden_params["response_cost"] or
    # the completion_cost helper; try both, fall back to the price table.
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, dict):
        raw = hidden.get("response_cost")
        if raw is not None:
            try:
                cost = float(raw)
            except (TypeError, ValueError):
                cost = None
    return prompt_tokens, completion_tokens, cost


def _cache_capability(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the model's context window once per (endpoint, model).

    The probe is a caller-supplied callable (config ``context_window_probe``);
    this module never opens a network connection on its own, because a
    capability probe is a billable call. The resolved window is cached per
    endpoint+model by ``runtime.model_capabilities`` and is always positive.
    """
    probe = ctx.get("context_window_probe")
    if not callable(probe):
        probe = None
    provider = ctx.get("provider")
    if ctx.get("use_mock_provider"):
        # The mock serves the response regardless of the configured provider
        # name, so the model is a harness-implemented one for capability
        # purposes too. Saying so lets the resolver skip the litellm rung
        # instead of importing a provider SDK to look up a model that has no
        # provider metadata to find.
        provider = "mock"
    return model_capabilities.resolve_context_window(
        str(ctx.get("_resolved_model") or ctx.get("model") or ""),
        provider=provider,
        api_base=ctx.get("api_base"),
        probe=probe,
    )


def _mock_call(messages: list, target: Dict[str, Any]) -> str:
    """Serve a canned/dynamic mock response (runtime/mock_provider.py).

    Raises RuntimeError when the mock has no response for the resolved
    model — tests install responses deliberately so a silent empty string
    would hide routing mistakes.
    """
    from . import mock_provider

    content = mock_provider.synthesize(target["model"], messages)
    if content is None:
        raise RuntimeError(
            f"mock provider has no response for model {target['model']!r} — "
            "install one via runtime.mock_provider.install()"
        )
    return content


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Best-effort classification: does this exception look like a
    provider rate limit (retryable after a wait)? String-matches the
    exception chain because litellm error classes vary by version and
    provider (RateLimitError / 429 / request limit / Too Many Requests)."""
    seen: set = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = f"{type(cur).__name__} {cur}".lower()
        if (
            "ratelimit" in text
            or "rate limit" in text
            or "429" in text
            or "too many requests" in text
            or "request limit" in text
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_transient_error(exc: BaseException) -> bool:
    """Best-effort classification: transient upstream failures worth a
    bounded retry — 5xx InternalServerError, connection errors, and the
    observed gateway-flake signature (BadRequestError with an EMPTY
    message: real bad requests carry a reason; an empty one means the
    gateway returned an unparseable/degraded response — measured in the
    Round-2 ablation, where one such flake killed a task's planner call
    while the identical request replayed fine)."""
    seen: set = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        lname = name.lower()
        chain = f"{name}: {cur}".strip()
        text = f"{cur}".strip()
        # litellm formats exceptions as "ClassName: message"; a gateway
        # flake carries no message text after the separator (observed:
        # "BadRequestError - " / "BadRequestError:" with nothing after).
        reason = re.split(r"[:\-\u2013]", text, maxsplit=1)[-1].strip() if text else ""
        if "internalservererror" in lname or "apiconnectionerror" in lname:
            return True
        if "badrequesterror" in chain.lower() and not reason:
            return True  # empty body = gateway flake, not a real 400
        if lname.endswith("error") and any(
            f" {code} " in f" {text} " or text.startswith(code)
            for code in ("500", "502", "503", "504")
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _extract_tool_calls(response: Any) -> list:
    """Return normalized provider-native tool calls from a litellm response.

    Provider adapters disagree about shape, so this accepts a native
    ``message.tool_calls`` list, a ``function`` mapping, a plain mapping with
    ``name``/``arguments``, or a JSON-arguments string, and returns a uniform
    ``{id, type, function: {name, arguments}}`` list. Every value is JSON
    safe and an unparseable payload becomes an empty argument object rather
    than an exception: the caller validates the schema and feeds the error
    back to the model.
    """
    try:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return []
        message = getattr(choices[0], "message", None)
        if message is None and isinstance(choices[0], dict):
            message = choices[0].get("message")
        raw = None
        if message is not None:
            raw = getattr(message, "tool_calls", None)
            if raw is None and isinstance(message, dict):
                raw = message.get("tool_calls")
        if not raw:
            return []
    except Exception:
        return []
    calls: list = []
    for index, item in enumerate(raw):
        try:
            function = getattr(item, "function", None)
            if function is None and isinstance(item, dict):
                function = item.get("function")
            name = getattr(function, "name", None)
            arguments = getattr(function, "arguments", None)
            if isinstance(function, dict):
                name = function.get("name")
                arguments = function.get("arguments")
            if name is None:
                name = getattr(item, "name", None) or (
                    item.get("name") if isinstance(item, dict) else None
                )
            if arguments is None and isinstance(item, dict):
                arguments = item.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except ValueError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            identifier = getattr(item, "id", None)
            if identifier is None and isinstance(item, dict):
                identifier = item.get("id")
            calls.append(
                {
                    "id": str(identifier or f"call_{index + 1}"),
                    "type": "function",
                    "function": {"name": str(name or ""), "arguments": arguments},
                }
            )
        except Exception:
            continue
    return calls


def _extract_reasoning(response: Any) -> Dict[str, Any]:
    """Return the R2-14 reasoning receipt for one provider response.

    Some providers return the model's thinking in a SEPARATE field
    (``reasoning_content``) and leave the visible answer in ``content``. The
    failure this exists to make nameable: a call that burned its whole
    completion budget on hidden reasoning returns ``content=None`` with
    ``finish_reason="length"`` — identical, from the router's point of view, to
    a gateway that returned nothing useful, which is how the historical
    "provider returned empty assistant content" message hid a real budget
    problem.

    Assumes a litellm-shaped response; anything unexpected yields
    ``present=False`` with zero counts. **The reasoning text is never returned
    as the answer**: this function only reports THAT it was there and how much
    of it there was, because reasoning is not a reply to a tool-driven prompt.
    """
    receipt = {
        "present": False,
        "chars": 0,
        "tokens": 0,
        "source": "none",
    }
    try:
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        if message is None and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
        if message is None:
            return receipt
        raw = getattr(message, "reasoning_content", None)
        if raw is None and isinstance(message, dict):
            raw = message.get("reasoning_content")
        if raw is None:
            raw = getattr(message, "reasoning", None)
        if raw is None and isinstance(message, dict):
            raw = message.get("reasoning")
        if raw is not None:
            receipt["present"] = True
            receipt["chars"] = len(str(raw))
            receipt["source"] = "message_field"
    except Exception:
        return {"present": False, "chars": 0, "tokens": 0, "source": "none"}
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    for holder in (details, usage):
        if holder is None:
            continue
        raw = getattr(holder, "reasoning_tokens", None)
        if raw is None and isinstance(holder, dict):
            raw = holder.get("reasoning_tokens")
        try:
            if raw is not None and int(raw) > 0:
                receipt["tokens"] = int(raw)
                receipt["present"] = True
                receipt["source"] = (
                    "usage" if receipt["source"] == "none" else receipt["source"]
                )
                break
        except (TypeError, ValueError):
            continue
    return receipt


def _latency_receipt(
    call: Any,
    import_before: float,
    *,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    streamed: bool,
    stream_stats: Any = None,
    tokens_estimated: bool = False,
) -> Dict[str, Any]:
    """Build the ledger's ``latency`` block from a :class:`CallLatency`.

    The four flat top-level keys (``latency_ttft_s`` and friends) exist
    because a JSONL ledger row is read by grep, ``jq`` and spreadsheet-shaped
    tooling, and a consumer that must not know this module's receipt shape
    still needs the numbers. The nested ``latency`` block carries the states
    and the reasons, which is the part that makes the numbers interpretable.

    ``import_wait_s`` is a DELTA of the process-wide preload wait, not the
    total: it answers "did *this* call pay for the import", which is the only
    form of the number that means anything per call.
    """
    call.import_wait_s = max(0.0, _preload_snapshot() - float(import_before))
    call.streamed = bool(streamed)
    call.input_tokens = prompt_tokens
    call.output_tokens = completion_tokens
    call.tokens_estimated = bool(tokens_estimated)
    if stream_stats is not None:
        call.ttft_s = getattr(stream_stats, "first_token_s", None)
    receipt = call.to_dict()
    ttft = receipt.get("ttft") or {}
    overhead = receipt.get("overhead") or {}
    provider = receipt.get("provider") or {}
    flat = {
        "latency_ttft_s": ttft.get("value"),
        "latency_ttft_state": ttft.get("state", "unavailable"),
        "latency_ttft_reason": ttft.get("reason", ""),
        "latency_duration_s": receipt.get("duration_s"),
        "latency_overhead_s": overhead.get("value"),
        "latency_overhead_state": overhead.get("state", "unavailable"),
        "latency_overhead_reason": overhead.get("reason", ""),
        "latency_provider_s": provider.get("value"),
        "latency_provider_state": provider.get("state", "unavailable"),
        "latency_import_wait_s": receipt.get("import_wait_s", 0.0),
        "latency_input_tokens_per_s": (receipt.get("input_tokens_per_s") or {}).get(
            "value"
        ),
        "latency_output_tokens_per_s": (receipt.get("output_tokens_per_s") or {}).get(
            "value"
        ),
    }
    out: Dict[str, Any] = {"latency": receipt}
    out.update(flat)
    try:
        latency.latency_window().merge_window(call)
    except Exception:  # pragma: no cover - observability must not fail a call
        pass
    return out


def _preload_snapshot() -> float:
    """Read the import's cumulative wait. A private seam for the test suite.

    The receipt is deliberately reduced to one comparable float rather than
    passed around whole, so a caller cannot accidentally merge one call's
    import receipt into another's latency receipt.
    """
    try:
        return float(latency.preload_state().get("waited_s") or 0.0)
    except Exception:
        return 0.0


def _completion_with_retry(
    kwargs: Dict[str, Any],
    max_retries: int,
    base_backoff_s: float,
    on_failure: Optional[Callable[[int, float, BaseException], None]] = None,
) -> Any:
    """Call litellm with bounded retries and per-attempt failure records."""
    litellm = latency.ensure_litellm()

    notify = on_failure or (lambda _attempt, _elapsed, _exc: None)
    max_retries = max(0, min(int(max_retries), 20))
    base_backoff_s = max(0.0, min(float(base_backoff_s), 300.0))
    rl_attempt = 0
    tr_attempt = 0
    transient_budget = 2
    provider_attempt = 0
    started = time.time()
    while True:
        try:
            return litellm.completion(**kwargs)
        except Exception as exc:
            provider_attempt += 1
            notify(provider_attempt, time.time() - started, exc)
            if _is_rate_limit_error(exc):
                if rl_attempt >= max_retries:
                    raise
                wait = base_backoff_s * (2**rl_attempt)
                time.sleep(wait)
                rl_attempt += 1
                continue
            if _is_transient_error(exc) and tr_attempt < transient_budget:
                time.sleep(5.0)
                tr_attempt += 1
                continue
            raise


def _litellm_completion() -> Callable[..., Any]:
    """Return litellm's ``completion`` callable, imported lazily.

    Kept separate from `_completion_with_retry` so the streaming path can
    dial the provider without inheriting the retry loop: a stream that was
    already partially consumed must never be replayed as a second charge.

    P1/W1: the import goes through ``runtime.latency.ensure_litellm``, the
    ONE choke point, so a background preload and this foreground call can
    never interleave and neither can observe a half-initialised module. The
    call contract is unchanged — this still returns a callable and still
    raises whatever the import raises.
    """
    return latency.ensure_litellm().completion


def _deliver_mock_stream(
    content: str,
    on_delta: Callable[[str], None],
    ctx: Dict[str, Any],
    call_id: str,
    started: float,
) -> streaming.StreamStats:
    """Replay a mock completion through the real streaming assembler.

    The scripted provider answers in whole chunks, so this splits the text
    into the same word-ish pieces a live provider would emit and feeds
    them through :class:`runtime.streaming.StreamAssembler`. That keeps
    one coalescing implementation for the live and offline lanes instead of
    a second, test-only shortcut.
    """
    window = int(ctx.get("stream_window_ms") or streaming.MIN_WINDOW_MS)
    assembler = streaming.StreamAssembler(on_delta=on_delta, window_ms=window)
    assembler.begin()
    parts = content.split(" ") or [""]
    for index, word in enumerate(parts):
        piece = word if index == len(parts) - 1 else word + " "
        assembler.feed({"choices": [{"delta": {"content": piece}}]})
    assembler.flush()
    return assembler.stats


#: Task.config keys that switch ``call_model`` over to the Ceiling-14
#: resilient pipeline (``runtime.provider_gateway``). The router delegates
#: ONLY when one of these is present, so an unconfigured caller keeps the
#: exact pre-existing code path — byte-identical request kwargs, ledger rows,
#: and exception objects. "Resilience is on" is a decision the operator makes,
#: not a default that changes every ablation arm's semantics.
_RESILIENCE_CONFIG_KEYS = (
    "offline",
    "privacy_policy",
    "privacy_data_classes",
    "privacy_providers",
    "privacy_models",
    "privacy_require_zdr",
    "privacy_redact",
    "local_model_profile",
    "local_model",
    "local_first",
    "local_first_roles",
    "local_first_decisive",
    "provider_fallbacks",
    "provider_fallback_max",
    "provider_fallback_across_tiers",
    "circuit_breaker_registry",
)


def _resilience_requested(ctx: Dict[str, Any]) -> bool:
    """True when the caller configured any Ceiling-14 resilience behavior.

    Deliberately a KEY test, not a VALUE test: a config carrying
    ``local_first: False`` still opts in, because the operator who wrote the
    key wants the pipeline (and its receipts) and explicitly turned the
    feature off inside it. A config with none of these keys never enters the
    pipeline at all.
    """
    return any(key in ctx for key in _RESILIENCE_CONFIG_KEYS)


def _resilient_call(
    messages: list,
    *,
    ctx: Dict[str, Any],
    target: Dict[str, Any],
    normalized_hint: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    api_key: Optional[str],
    tools: Optional[list],
    tool_choice: Optional[str],
    stream: bool,
    effort: Any = None,
) -> Any:
    """Delegate one call to ``runtime.provider_gateway``.

    A STREAMED call runs only the GATE half of the pipeline (redaction,
    offline, privacy) and then continues into the router's own streaming dial.
    Failover is deliberately unavailable for a stream: a partially consumed
    response must never be replayed against another provider as a second
    charge. The gate still applies, so streaming is not a way to bypass
    redaction or the offline/privacy refusals.

    ``effort`` is forwarded because the pipeline resolves it PER CANDIDATE:
    a fallback target may support a different knob than the primary, and a
    plan computed once against the primary would be a lie for the model that
    actually answered.
    """
    from . import provider_gateway

    if stream:
        messages = provider_gateway.enforce_stream_gate(
            messages,
            context=ctx,
            target=target,
            hint=normalized_hint,
            api_key=api_key,
        )
        return None
    return provider_gateway.resilient_call_model(
        messages,
        normalized_hint,
        provider,
        model,
        api_key,
        tools,
        tool_choice,
        idempotent=_idempotent_from(ctx),
        primary_target=target,
        effort=effort,
    )


def _effort_plan(
    ctx: Dict[str, Any], target: Dict[str, Any], effort: Any = None
) -> model_capabilities.EffortPlan:
    """Resolve the effort plan for ONE target, honestly and never fatally.

    A harness-served provider (the offline mock / scripted lane) is reported
    as ``synthetic`` rather than ``sent``: the response is canned and request
    parameters are not read, so claiming a parameter was sent would be the
    exact lie this plan exists to prevent. Everything else is ``map_effort``,
    which cannot raise.
    """
    model_name = str(target.get("model") or "")
    provider_name = str(target.get("provider") or "")
    if model_capabilities.effort_family_for(model_name, provider_name) == "" and (
        provider_name.lower() in model_capabilities.SYNTHETIC_PROVIDERS
        or bool(ctx.get("use_mock_provider"))
    ):
        return model_capabilities.synthetic_effort_plan(
            effort if effort is not None else ctx.get("effort"),
            model=model_name,
            provider=provider_name,
        )
    return model_capabilities.map_effort(
        effort if effort is not None else ctx.get("effort"),
        model_name,
        provider=provider_name,
        parameter=ctx.get("effort_parameter"),
    )


def _idempotent_from(ctx: Dict[str, Any]) -> bool:
    """Whether this task's model calls may be replayed.

    Default True (a chat completion is safe to replay). A task that creates
    provider-side state sets ``model_calls_idempotent: False`` and is never
    retried or failed over.
    """
    value = ctx.get("model_calls_idempotent", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def call_model(
    messages: list,
    difficulty_hint: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    tools: Optional[list] = None,
    tool_choice: Optional[str] = None,
    stream: bool = False,
    on_delta: Optional[Callable[[str], None]] = None,
    effort: Any = None,
) -> Any:
    """Call an LLM through the configured provider and return its content.

    Assumes OpenAI-style chat messages. Explicit provider/model settings
    take precedence over adaptive routing. Every provider attempt produces
    a context-local usage record and, when configured, a recoverable JSONL
    ledger row. Provider failures are recorded before they propagate.

    ``tools`` is an additive keyword: when a caller supplies provider-neutral
    tool schemas they are forwarded to the provider verbatim, and a provider
    that answers with native tool calls returns the NORMALIZED structure
    ``{"content", "tool_calls", "finish_reason"}`` instead of a bare string.
    A response with no native tool call still returns the historical string,
    so every existing Boundary-2 consumer is unchanged.

    ``stream`` / ``on_delta`` (VEX-CEILING-10) pass ``stream=True`` to the
    provider and deliver coalesced text to ``on_delta`` as it arrives. The
    assembled result is IDENTICAL in shape to the non-streamed path, so a
    streamed call is a drop-in for every consumer above this line. The
    ledger row records ``streamed``, the measured chunk/delivery counts, and
    whether usage came from the provider or an estimate — a call that fell
    back to a non-streamed request is recorded as ``streamed: false``, never
    claimed as streamed. A boundary that does not accept these keywords
    (an older stub, a test double) keeps working: the caller detects that
    and the request degrades to the historical single call.

    ``effort`` (AGT-08) is an additive keyword on the same terms: a level is
    mapped onto the RESOLVED TARGET's real provider parameter, and the plan
    is recorded on the ledger row, in ``get_last_usage()`` and in the
    ``model_routed`` trace event whether or not anything could be sent. It
    never changes whether a call is made and has no path to a completion
    status.
    """
    ctx = dict(_CONTEXT.get() or {})
    ledger = _LEDGER_PATH.get()
    # P1/W1: opened BEFORE any routing work so the receipt covers the whole
    # call. `import_wait_s` is filled in below from the preload receipt, which
    # is how "the first call paid for the import" and "a later call paid
    # nothing" become two different, visible numbers.
    call_latency = latency.CallLatency.begin()
    _import_before = _preload_snapshot()
    normalized_hint = _norm_hint(difficulty_hint)
    prediction: Dict[str, Any] = {
        "source": "explicit" if normalized_hint else "default",
        "estimator": None,
        "score": None,
        "hint": normalized_hint,
    }

    if normalized_hint is None and not (model or ctx.get("model")):
        predicted = _maybe_predict_difficulty(messages, ctx)
        if predicted:
            difficulty_hint = predicted["hint"]
            normalized_hint = _norm_hint(difficulty_hint)
            info = predicted.get("info") or {}
            features = info.get("features") or {}
            prediction = {
                "source": "predicted",
                "estimator": info.get("estimator"),
                "score": features.get("score"),
                "hint": normalized_hint,
                # R2-13: WHICH feature family produced the hint. Without it a
                # ledger cannot tell a lexical prediction from a structural one,
                # and the ablation cannot attribute a cost delta to either.
                "feature_family": info.get("estimator"),
                "features_resolved": features.get("resolved_feature_count"),
                "features_total": features.get("feature_count"),
                "fallback_reason": info.get("fallback_reason"),
            }

    target = _resolve_target(difficulty_hint, provider, model, ctx)
    # R2-13 capability gate. Strictly opt-in by KEY PRESENCE: with no
    # capability key in the context this returns a receipt that says the gate
    # was considered and is off, and every line below is unchanged. A refusal
    # raises here, BEFORE the provider is touched, so an unpriced or
    # tool-incapable model never becomes a billable call.
    tool_driven = bool(tools) or bool(ctx.get("capability_tool_driven"))
    capability_receipt = _capability_screen(
        target, ctx, hint=normalized_hint, tool_driven=tool_driven, explicit_model=model
    )
    if capability_receipt.get("enabled"):
        selected = capability_receipt.get("selected_model")
        if selected and selected != target.get("model"):
            # The screen picked a more capable candidate: re-derive the target
            # around it so the endpoint/key fields travel with the selection.
            for candidate in _tier_candidates(target, normalized_hint, ctx):
                if candidate.get("model") == selected:
                    target = _resolve_target(
                        candidate.get("routed_via_hint"),
                        candidate.get("provider"),
                        candidate.get("model"),
                        ctx,
                    )
                    break
    # AGT-08: the effort plan is resolved against the FINAL target, after any
    # capability screen may have re-pointed it, and before the request is
    # built. It is a value, never an exception.
    effort_plan = _effort_plan(ctx, target, effort)
    if _resilience_requested(ctx):
        # VEX-CEILING-14: hand the call to the resilient pipeline. This is the
        # ONE place the router delegates, and it is strictly opt-in — without a
        # resilience key in the context, every line below is unchanged.
        messages = _resilient_call(
            messages,
            ctx=ctx,
            target=target,
            normalized_hint=normalized_hint,
            provider=provider,
            model=model,
            api_key=api_key,
            tools=tools,
            tool_choice=tool_choice,
            stream=stream,
            effort=effort,
        )
        if not stream:
            return messages  # the pipeline already produced the response
        # A streamed call: the gate ran and returned the sanitized messages;
        # continue into the router's own streaming dial below.
    effective_key = api_key or target.get("api_key") or ctx.get("api_key")
    effective_base = target.get("api_base") or ctx.get("api_base")
    call_id = uuid.uuid4().hex
    started = time.time()
    # P1/W1: the latency clock. `call_latency` is opened at the very top of
    # call_model and the receipt accounts for the WHOLE call — including the
    # capability-screen rung above this line, which can import litellm
    # (measured 17.6-22.2 s on this host). Folding that into a call's reported
    # provider time is how a harness-side import gets misattributed to a
    # provider endpoint, so overhead and provider time are separate fields.
    call_latency.mark_dialed()
    configured_tiers = ctx.get("model_tiers")
    if not isinstance(configured_tiers, dict):
        configured_tiers = {}
    # The frozen-prefix plan is computed ONCE per call, before the provider is
    # touched, so a request that fails still records what it would have cached.
    cache_enabled = ctx.get("prompt_cache", True)
    if not isinstance(cache_enabled, bool):
        cache_enabled = str(cache_enabled).strip().lower() not in {
            "0",
            "false",
            "off",
            "no",
        }
    cache_ledger = prompt_cache.current_cache_ledger()
    cache_plan = prompt_cache.plan_cache(
        messages,
        tools,
        provider=target.get("provider"),
        model=target["model"],
        enabled=cache_enabled,
        min_prefix_tokens=int(
            ctx.get("prompt_cache_min_prefix_tokens")
            or prompt_cache.DEFAULT_MIN_PREFIX_TOKENS
        ),
        breakpoint_index=ctx.get("prompt_cache_breakpoint"),
    )
    request_messages, request_tools = prompt_cache.apply_cache_parameters(
        messages, tools, cache_plan
    )
    capabilities = _cache_capability({**ctx, "_resolved_model": target["model"]})
    secrets = tuple(
        str(value)
        for value in (
            effective_key,
            *(
                tier.get("api_key")
                for tier in configured_tiers.values()
                if isinstance(tier, dict)
            ),
        )
        if value
    )
    recorded_attempt = 0
    tool_call_count = 0
    stream_stats: Optional[streaming.StreamStats] = None
    streamed = False

    def record_attempt(
        outcome: str,
        attempt: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float = 0.0,
        cost_source: str = "unknown",
        elapsed_s: float = 0.0,
        error: Optional[str] = None,
        stop_reason: Optional[str] = None,
        cache_usage: Any = None,
        price_state: str = model_capabilities.PRICE_UNPRICED,
        cost_priced: bool = False,
        reasoning: Optional[Dict[str, Any]] = None,
        usage_reported: bool = False,
    ) -> None:
        receipt = prompt_cache.receipt_from_usage(cache_usage, cache_plan)
        cache_ledger.record(
            cache_plan,
            receipt,
            model=str(target["model"]),
            api_base_sha256=_endpoint_fingerprint(effective_base),
        )
        record = {
            "ts": now_iso(),
            "call_id": call_id,
            "attempt": attempt,
            "outcome": outcome,
            "model": target["model"],
            "provider": target.get("provider") or "",
            "tier": target.get("routed_via_hint"),
            "api_base_sha256": _endpoint_fingerprint(effective_base),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tokens": prompt_tokens + completion_tokens,
            "cost_usd": round(float(cost), 8),
            "cost_source": cost_source,
            # R2-13: "free" and "we never priced this model" are DIFFERENT
            # answers. A bare cost_usd of 0.0 cannot tell them apart, so the
            # state and the priced flag ride every row.
            "price_state": price_state,
            "cost_priced": bool(cost_priced),
            "elapsed_s": round(float(elapsed_s), 3),
            "routed_via_hint": target.get("routed_via_hint"),
            "difficulty_hint": normalized_hint,
            "difficulty_prediction": prediction,
            "context_window": int(capabilities["context_window"]),
            "context_window_source": capabilities["context_window_source"],
        }
        # The capability receipt always rides the row so "was the gate
        # considered?" is answerable after the fact, including when it was off.
        record["capability_gate"] = capability_receipt
        record["capability_refusal_count"] = int(
            capability_receipt.get("refusal_count") or 0
        )
        # AGT-08: the effort plan rides EVERY row, including a failure and
        # including a level that could not be sent. A cost claim is only
        # explainable if the ledger says what was asked for AND what reached
        # the provider.
        record.update(effort_plan.to_dict())
        if reasoning:
            record["reasoning_content_present"] = bool(reasoning.get("present"))
            record["reasoning_chars"] = int(reasoning.get("chars") or 0)
            record["reasoning_tokens"] = int(reasoning.get("tokens") or 0)
        # The cache receipt rides the ledger row verbatim, so /cost, --json and
        # any offline ledger analysis read the provider's own numbers.
        record.update(receipt.to_dict())
        record["cache_requested"] = bool(cache_plan.requested)
        record["cache_skip_reason"] = cache_plan.skip_reason
        record["cache_prefix_tokens_estimate"] = int(cache_plan.prefix_tokens_estimate)
        record["cache_provider_family"] = cache_plan.provider_family
        if stop_reason:
            # Recorded instead of silently ending: an incomplete or refused
            # stop is evidence, not noise, and replay needs it to explain why
            # a turn produced no usable content.
            record["stop_reason"] = str(stop_reason)
            record["tool_calls"] = int(tool_call_count or 0)
        record["streamed"] = bool(streamed)
        if stream_stats is not None:
            record["stream"] = stream_stats.to_dict()
        if error:
            record["error"] = error
        # P1/W1 (T3): the latency receipt. Every field is (value, state,
        # reason); a field that could not be measured carries
        # `unavailable` + the reason and NEVER 0. The receipt also folds into
        # the process window, which is what makes a p50/p95 over a real
        # sample possible at all — the ledger alone has no population.
        record.update(
            _latency_receipt(
                call_latency,
                _import_before,
                prompt_tokens=prompt_tokens if usage_reported else None,
                completion_tokens=completion_tokens if usage_reported else None,
                streamed=streamed,
                stream_stats=stream_stats,
                tokens_estimated=not usage_reported,
            )
        )
        _record_usage(record, ctx, ledger)

    if ctx.get("use_mock_provider"):
        # P1/W1: the offline lane has NO provider dial and NO gateway
        # overhead. Declaring that up front is what keeps a scripted run's
        # ledger from being read as provider-latency evidence.
        call_latency.dial_kind = "mock"
        call_latency.mark_dialed()
        try:
            content = _mock_call(messages, target)
            call_latency.mark_finished()
            prompt_tokens = (
                10 + sum(len(str(m.get("content", ""))) for m in messages) // 4
            )
            completion_tokens = len(content) // 4
            estimate = _cost_record(target, prompt_tokens, completion_tokens)
            cost, cost_source = estimate.cost_usd, estimate.source
        except Exception as exc:
            record_attempt(
                "error",
                1,
                elapsed_s=time.time() - started,
                error=_safe_error(exc, secrets),
            )
            raise
        if on_delta is not None and content:
            # The offline lane must exercise the SAME delta path as a live
            # provider, otherwise every streaming regression would be
            # invisible to the eval matrix and the scripted tests.
            stream_stats = _deliver_mock_stream(
                content, on_delta, ctx, call_id, started
            )
            streamed = True
        record_attempt(
            "success",
            1,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            cost_source=cost_source,
            elapsed_s=time.time() - started,
            price_state=estimate.price_state,
            cost_priced=estimate.priced,
            # The mock lane ESTIMATES its token counts; the provider reported
            # nothing. Saying so is what stops a scripted run's ledger from
            # being read as provider throughput evidence.
            usage_reported=False,
        )
        return content

    kwargs: Dict[str, Any] = {
        "model": target["model"],
        "messages": request_messages,
    }
    selected_provider = target.get("provider")
    if selected_provider:
        prefix = f"{selected_provider}/"
        kwargs["model"] = (
            target["model"]
            if str(target["model"]).startswith(prefix)
            else f"{prefix}{target['model']}"
        )
    if effective_key:
        kwargs["api_key"] = effective_key
    if effective_base:
        kwargs["api_base"] = effective_base
    max_completion_tokens = ctx.get("max_completion_tokens")
    if max_completion_tokens:
        kwargs["max_tokens"] = int(max_completion_tokens)
    # Native tool calling: the catalog's provider-neutral schemas reach the
    # provider verbatim. Absent tools, the historical kwargs are byte-equal
    # so every existing routing test and caller is unaffected. When a cache
    # breakpoint was requested the LAST schema also carries it, which is the
    # tool half of the stable agent prefix.
    if request_tools:
        kwargs["tools"] = list(request_tools)
        if tool_choice:
            kwargs["tool_choice"] = str(tool_choice)
    # AGT-08: the ONLY place an effort parameter reaches the wire. It is
    # ``kwargs.setdefault``-free on purpose - a level that could not be mapped
    # contributes nothing, so an unsupported provider can never receive a
    # parameter it did not ask for.
    if effort_plan.sent:
        kwargs.update(effort_plan.parameters)

    provider_attempts = 0
    reasoning: Dict[str, Any] = {
        "present": False,
        "chars": 0,
        "tokens": 0,
        "source": "none",
    }

    def record_provider_failure(
        attempt: int, elapsed_s: float, exc: BaseException
    ) -> None:
        nonlocal provider_attempts, recorded_attempt
        provider_attempts = attempt
        recorded_attempt = attempt
        record_attempt(
            "error",
            attempt,
            elapsed_s=elapsed_s,
            error=_safe_error(exc, secrets),
        )

    wants_stream = bool(stream) and on_delta is not None
    if wants_stream:
        kwargs["stream"] = True
    # P1/W1: whether the provider actually reported usage. An ESTIMATED token
    # count is a real number we computed, not a number the provider gave us,
    # and a tokens/sec figure divided by it would be presented as a
    # measurement of the provider's throughput when it is a measurement of
    # our estimator. The receipt says `tokens_estimated: true` and the rates
    # are reported with that flag visible.
    usage_reported = False
    # P1/W1: the provider window opens HERE. Everything above this line is
    # harness/gateway overhead — routing, the capability/context-window rungs
    # (one of which can import litellm), redaction, the cache plan — and
    # separating it from the dial is the whole reason this mark exists.
    call_latency.mark_dialed()
    try:
        if wants_stream:
            # Streamed path (VEX-CEILING-10). The assembled result is fed
            # through the SAME extraction and pricing code as the
            # non-streamed path, so a streamed call is indistinguishable
            # to every consumer above the router apart from the receipt.
            content, tool_calls, stop, stream_stats, stream_usage = (
                streaming.stream_call(
                    _litellm_completion(),
                    kwargs,
                    on_delta=on_delta,
                    window_ms=int(
                        ctx.get("stream_window_ms") or streaming.MIN_WINDOW_MS
                    ),
                )
            )
            streamed = bool(stream_stats.chunks_seen)
            tool_call_count = len(tool_calls)
            finish_reason = stop or ("stream_complete" if streamed else "")
            cache_usage = None
            cached_tokens = 0
            if stream_usage:
                prompt_tokens = int(stream_usage.get("prompt_tokens") or 0)
                completion_tokens = int(
                    stream_usage.get("completion_tokens")
                    or stream_usage.get("output_tokens")
                    or 0
                )
                cached_tokens, _c, _w, _r = prompt_cache.cache_tokens_from_usage(
                    stream_usage
                )
                reported_cost = stream_usage.get("cost")
                if reported_cost is None and "cache_read_input_tokens" in stream_usage:
                    reported_cost = 0.0  # priced by the cache-read rate below
                reasoning_tokens = int(stream_usage.get("reasoning_tokens") or 0)
                usage_reported = True
                if reasoning_tokens:
                    reasoning = {
                        "present": True,
                        "chars": 0,
                        "tokens": reasoning_tokens,
                        "source": "usage",
                    }
            else:
                # No usage frame: estimate, and say so.
                prompt_tokens = streaming.estimate_prompt_tokens(request_messages)
                completion_tokens = len(content) // 4
                reported_cost = None
        else:
            response = _completion_with_retry(
                kwargs,
                max_retries=int(ctx.get("rate_limit_retries", 4)),
                base_backoff_s=float(ctx.get("rate_limit_backoff_s", 15.0)),
                on_failure=record_provider_failure,
            )
            prompt_tokens, completion_tokens, reported_cost = _extract_usage(response)
            # `_extract_usage` returns (0, 0, None) for a shape it does not
            # recognise. That is a refusal, not a zero-token response, so the
            # receipt reports `unavailable` rather than an instant provider.
            usage_reported = bool(prompt_tokens or completion_tokens)
            cache_usage = getattr(response, "usage", None)
            cached_tokens, _creation, _write, _reported = (
                prompt_cache.cache_tokens_from_usage(cache_usage)
            )
            choice = response.choices[0]
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None)
            finish_reason = getattr(choice, "finish_reason", None)
            tool_calls = _extract_tool_calls(response)
            tool_call_count = len(tool_calls)
            reasoning = _extract_reasoning(response)
            if not isinstance(content, str):
                content = "" if content is None else str(content)
        # P1/W1: the provider window closes here — after the response is
        # parsed, because parsing is our cost, not the provider's, and a window
        # that stopped at the socket would hide it.
        call_latency.mark_finished()
        if not content and not tool_calls:
            # R2-14 protocol. An answer can be empty for two materially
            # different reasons, and telling them apart is the whole
            # diagnosis:
            #   * the model spent its completion budget on reasoning and the
            #     VISIBLE answer was truncated (finish_reason="length") -- a
            #     budget problem, fixed with a bigger max_completion_tokens;
            #   * the model returned only reasoning, or nothing at all -- not
            #     budget-limited, and reporting it as one sends the operator
            #     after the wrong knob entirely.
            # Reasoning is NEVER substituted for the answer: a hidden trace is
            # not a reply, and treating it as one would launder a truncated
            # turn into a successful-looking one.
            truncated = str(finish_reason or "") == "length"
            if reasoning.get("present") and truncated:
                outcome = "truncated_reasoning"
                failure = (
                    "provider returned no visible content: the completion budget "
                    "was consumed by reasoning_content ("
                    f"{reasoning.get('tokens') or reasoning.get('chars')}"
                    " reasoning units) and the answer was truncated at "
                    "finish_reason=length"
                )
            elif reasoning.get("present"):
                outcome = "empty_response_reasoning_only"
                failure = (
                    "provider returned reasoning_content but no assistant content "
                    "and no tool call; reasoning is not an answer"
                )
            else:
                outcome = "empty_response"
                failure = "provider returned empty assistant content"
            record_attempt(
                "error",
                max(provider_attempts, 1),
                elapsed_s=time.time() - started,
                error=failure,
                stop_reason=str(finish_reason or outcome),
                cache_usage=cache_usage,
                reasoning=reasoning,
            )
            # The receipt above is the COMPLETE one (it names the reasoning,
            # the stop reason and the outcome). Mark it recorded so the
            # generic handler below does not append a second, thinner row for
            # the same provider attempt — two rows for one failure makes a
            # ledger reader count the failure twice.
            recorded_attempt = max(provider_attempts, 1)
            raise RuntimeError(failure)
        estimate = _cost_record(
            target,
            prompt_tokens,
            completion_tokens,
            cached_tokens,
            provider_cost_usd=reported_cost,
        )
        cost, cost_source = estimate.cost_usd, estimate.source
    except Exception as exc:
        if recorded_attempt < max(provider_attempts, 1):
            record_attempt(
                "error",
                max(provider_attempts, 1),
                elapsed_s=time.time() - started,
                error=_safe_error(exc, secrets),
            )
        raise

    record_attempt(
        "success",
        provider_attempts + 1,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        cost_source=cost_source,
        elapsed_s=time.time() - started,
        stop_reason=finish_reason,
        cache_usage=cache_usage,
        price_state=estimate.price_state,
        cost_priced=estimate.priced,
        reasoning=reasoning,
        usage_reported=usage_reported,
    )
    if tool_calls:
        # Native structured turn: the caller validates the schema, and the
        # text-protocol fallback is still available because ``content``
        # travels with the calls.
        return {
            "content": content,
            "text": content,
            "tool_calls": tool_calls,
            "finish_reason": str(finish_reason or ""),
        }
    return content
