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

Config wiring: the harness passes provider/model/api_key in task.config;
the router reads task.config-derived entries via a module-level context
(see set_call_context / runtime/worker.py). Difficulty prediction for
unrated steps (hint=None but routing on) happens HERE via
runtime.difficulty, so the harness never needs routing code.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import DEFAULT_MODEL_TIERS
from .fsutil import append_jsonl, now_iso

HINTS = ("easy", "medium", "hard")

# Rough per-1M-token price table (USD) — fallback when litellm reports no
# cost. Same table shape as harness/_stubs/model_router.py so behavior is
# comparable across the swap. Keys are litellm model strings.
_PRICES: Dict[str, tuple] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 2.00),
    "gpt-4.1": (2.00, 8.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-sonnet-4-20250514": (3.00, 25.00),
    "deepseek-chat": (0.14, 0.28),
    # --- ablation proxy prices (free-tier routers; tokens are REAL, these
    # are published rates for comparable model classes — see runtime/ablation.py):
    "qwen3.8-27b": (0.20, 0.60),        # mid-size open model class
    "z-ai/glm-5.3-free": (0.60, 2.20),  # frontier-class API rates proxy
}

_LOCK = threading.RLock()
_last_usage: Dict[str, Any] = {}
# Thread-local guard: when the "llm" difficulty estimator itself calls
# call_model, that nested call must not re-trigger difficulty prediction
# (infinite recursion). The estimator always passes explicit model cfg,
# and this guard is the belt to those braces.
_ESTIMATOR_GUARD = threading.local()
# Module-level call context (set per-task by runtime/worker.py so each task
# process's router knows its config without changing call_model's frozen
# signature). Keys: adaptive_routing, model_tiers, difficulty_estimator,
# difficulty_llm, provider/model/api_key defaults, use_mock_provider, ledger_dir.
_context: Dict[str, Any] = {}
_ledger_path: Optional[Path] = None


def set_call_context(config: Optional[Dict[str, Any]], ledger_dir: Optional[str] = None) -> None:
    """Install this task's router config as the module-level context.

    Assumes a process-per-task scheduler (one context per process at a
    time); worker.py calls this once before run_task. Thread-safe; pass
    None to clear. ledger_dir enables per-call JSONL ledger writing.
    """
    global _ledger_path
    with _LOCK:
        _context.clear()
        if config:
            _context.update(config)
        _ledger_path = Path(ledger_dir) if ledger_dir else None
        _last_usage.clear()


def get_last_usage() -> Dict[str, Any]:
    """Usage of the most recent call_model call: {model, provider,
    prompt_tokens, completion_tokens, tokens, cost_usd, elapsed_s,
    routed_via_hint}. Empty dict if no call has been made. This is the
    convention Terminal 1's ModelClient reads via _read_usage().
    """
    with _LOCK:
        return dict(_last_usage)


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
) -> Dict[str, Any]:
    """Decide the (provider, model, api_key, api_base, routed) target.

    Precedence: explicit provider/model > tier table for hint > context
    defaults > built-in medium tier. A model_tiers entry may carry its own
    api_key/api_base (e.g. routing across different providers' endpoints —
    each tier hits its own gateway); context-level api_key/api_base apply
    to non-routed calls. Returns dict for logging; never raises on
    unknown hints.
    """
    with _LOCK:
        ctx = dict(_context)

    tiers = ctx.get("model_tiers") or DEFAULT_MODEL_TIERS
    if not isinstance(tiers, dict):
        tiers = DEFAULT_MODEL_TIERS

    explicit_model = model or ctx.get("model")
    explicit_provider = provider or ctx.get("provider")

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
                    "api_key": tier.get("api_key"),
                    "api_base": tier.get("api_base"),
                }
    return {
        "provider": explicit_provider,
        "model": explicit_model or DEFAULT_MODEL_TIERS["medium"]["model"],
        "routed_via_hint": None if explicit_model else "medium-default",
        "api_key": None,
        "api_base": ctx.get("api_base"),
    }


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
    if getattr(_ESTIMATOR_GUARD, "active", False):
        return {}
    estimator = ctx.get("difficulty_estimator") or "heuristic"
    if estimator == "off":
        return {}
    if not any(isinstance(m, dict) and str(m.get("content", "")).strip()
               for m in messages):
        return {}
    from .difficulty import predict_difficulty

    _ESTIMATOR_GUARD.active = True
    try:
        hint, info = predict_difficulty(
            "\n".join(str(m.get("content", "")) for m in messages
                      if isinstance(m, dict)),
            estimator=estimator, llm_cfg=ctx.get("difficulty_llm"),
            messages=messages,
        )
        return {"hint": hint, "info": info}
    finally:
        _ESTIMATOR_GUARD.active = False


def _cost_fallback(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost when litellm reports none (unknown-model price
    table; see _PRICES). Non-table models cost $0 — never block a call."""
    p_in, p_out = _PRICES.get(model, (0.0, 0.0))
    return (prompt_tokens / 1e6) * p_in + (completion_tokens / 1e6) * p_out


def _record_usage(record: Dict[str, Any]) -> None:
    with _LOCK:
        _last_usage.clear()
        _last_usage.update(record)
    ledger = _ledger_path
    if ledger is not None:
        try:
            append_jsonl(ledger, record)
        except OSError:
            pass  # ledger is observability, not correctness


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
        if ("ratelimit" in text or "rate limit" in text
                or "429" in text or "too many requests" in text
                or "request limit" in text):
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
        reason = re.split(r"[:\-\u2013]", text, 1)[-1].strip() if text else ""
        if "internalservererror" in lname or "apiconnectionerror" in lname:
            return True
        if "badrequesterror" in chain.lower() and not reason:
            return True  # empty body = gateway flake, not a real 400
        if lname.endswith("error") and any(
                f" {code} " in f" {text} " or text.startswith(code)
                for code in ("500", "502", "503", "504")):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _completion_with_retry(kwargs: Dict[str, Any], max_retries: int,
                           base_backoff_s: float) -> Any:
    """litellm.completion with bounded retry for transient congestion.

    Rate limits (429s) back off long (base 15s — provider windows are
    minute-scale); other transient errors (5xx/gateway flakes) retry a
    small budget with short backoff. Non-transient errors propagate
    immediately (they're task failures, not congestion)."""
    import litellm

    rl_attempt = 0
    tr_attempt = 0
    transient_budget = 2
    while True:
        try:
            return litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001 — classified below
            if _is_rate_limit_error(exc):
                if rl_attempt >= max_retries:
                    raise
                wait = base_backoff_s * (2 ** rl_attempt)
                time.sleep(wait)
                rl_attempt += 1
                continue
            if _is_transient_error(exc) and tr_attempt < transient_budget:
                time.sleep(5.0)
                tr_attempt += 1
                continue
            raise


def call_model(
    messages: list,
    difficulty_hint: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Boundary 2 (INTERFACES.md): call an LLM and return assistant content.

    Assumes `messages` is an OpenAI-style chat message list. Routing:
    explicit provider/model wins; else with adaptive_routing ON, the
    difficulty_hint selects a model tier (easy->cheap, hard->expensive);
    else the context/default model. Transient provider rate limits are
    retried with exponential backoff (config: rate_limit_retries,
    rate_limit_backoff_s) — a congested endpoint must not kill a whole
    task. Usage (model, provider, tokens, cost, routed_via_hint) is
    recorded in get_last_usage() and the per-call JSONL ledger. Raises
    RuntimeError if litellm is not importable.
    """
    with _LOCK:
        ctx = dict(_context)

    if _norm_hint(difficulty_hint) is None and not (model or ctx.get("model")):
        # No caller hint and no explicit model: adaptive routing (if on)
        # predicts difficulty from the message content itself.
        predicted = _maybe_predict_difficulty(messages, ctx)
        if predicted:
            difficulty_hint = predicted["hint"]

    target = _resolve_target(difficulty_hint, provider, model)
    effective_key = api_key or target.get("api_key") or ctx.get("api_key")

    started = time.time()
    if ctx.get("use_mock_provider"):
        content = _mock_call(messages, target)
        prompt_tokens = 10 + sum(len(str(m.get("content", ""))) for m in messages) // 4
        completion_tokens = len(content) // 4
        cost = _cost_fallback(target["model"], prompt_tokens, completion_tokens)
    else:
        kwargs: Dict[str, Any] = {"model": target["model"], "messages": messages}
        if target.get("provider"):
            kwargs["model"] = f"{target['provider']}/{target['model']}"
        if effective_key:
            kwargs["api_key"] = effective_key
        effective_base = target.get("api_base") or ctx.get("api_base")
        if effective_base:
            kwargs["api_base"] = effective_base
        response = _completion_with_retry(
            kwargs,
            max_retries=int(ctx.get("rate_limit_retries", 4)),
            base_backoff_s=float(ctx.get("rate_limit_backoff_s", 15.0)),
        )
        prompt_tokens, completion_tokens, cost = _extract_usage(response)
        if cost is None:
            cost = _cost_fallback(target["model"], prompt_tokens, completion_tokens)
        content = response.choices[0].message.content or ""

    record = {
        "ts": now_iso(),
        "model": target["model"],
        "provider": target["provider"] or "",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens": prompt_tokens + completion_tokens,
        "cost_usd": round(float(cost), 6),
        "elapsed_s": round(time.time() - started, 2),
        "routed_via_hint": target.get("routed_via_hint"),
        "difficulty_hint": _norm_hint(difficulty_hint),
    }
    _record_usage(record)
    return content
