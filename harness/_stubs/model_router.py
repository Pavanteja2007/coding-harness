"""STUB for INTERFACES.md Boundary 2 — Terminal 3 (runtime) owns the real one.

Single hardcoded provider via litellm (project tech decision), with the
exact Boundary 2 signature so harness/deps.py can swap it out. litellm is
imported lazily because the installed copy on some dev machines (Python
3.10) fails at import — the harness and its tests must import cleanly
without it; the error surfaces only if you actually call the model.

Cost/token usage: litellm's usage metadata is used to fill a usage record
the harness reads back (via get_last_usage) for budget tracking and the
TaskResult.model_calls list.
"""
import time
from typing import Any, Dict, List, Optional

_last_usage: Dict[str, Any] = {}

# Rough per-1M-token price table (USD) for the default fallback models —
# only used to estimate cost when the provider doesn't report it.
_EST_PRICES: Dict[str, tuple] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 2.00),
    "gpt-4.1": (2.00, 8.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "deepseek-chat": (0.14, 0.28),
}


def get_last_usage() -> Dict[str, Any]:
    """Usage from the most recent call: {model, provider, prompt_tokens,
    completion_tokens, cost_usd}. Empty dict if no call has been made.
    Assumes one call_model thread at a time; harness usage-tracking wraps
    it per call (see harness/model_client.py which snapshots immediately).
    """
    return dict(_last_usage)


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost from the price table; unknown models cost 0."""
    p_in, p_out = _EST_PRICES.get(model, (0.0, 0.0))
    return (prompt_tokens / 1e6) * p_in + (completion_tokens / 1e6) * p_out


def call_model(
    messages: list,
    difficulty_hint: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """STUB: call one hardcoded provider directly via litellm.

    Assumes `messages` is an OpenAI-style chat message list. difficulty_hint
    is accepted (Boundary 2 contract) but ignored — the stub has no routing
    logic; Terminal 3's real router will use it. Returns the assistant
    message content string. Raises RuntimeError if litellm is unavailable
    (e.g. broken install) so the harness can surface a clean error instead
    of an import traceback.
    """
    global _last_usage
    model = model or "gpt-4o-mini"
    provider = provider or "openai"
    try:
        import litellm  # lazy: may be missing/broken on the dev machine
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "model stub: litellm is not importable on this machine "
            f"({exc}). Install litellm or inject a model via "
            "harness.deps.set_call_model()."
        ) from exc

    kwargs: Dict[str, Any] = {"model": model}
    if api_key:
        kwargs["api_key"] = api_key

    started = time.time()
    response = litellm.completion(messages=messages, **kwargs)
    elapsed = time.time() - started

    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    cost = _estimate_cost(model, prompt_tokens, completion_tokens)

    _last_usage = {
        "model": model,
        "provider": provider,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens": prompt_tokens + completion_tokens,
        "cost_usd": round(cost, 6),
        "elapsed_s": round(elapsed, 2),
    }
    content = response.choices[0].message.content or ""
    return content
