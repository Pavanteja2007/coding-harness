"""Model capability resolution: context windows, tool support, and PRICING.

Three rules this module exists to enforce.

**1. An unknown context window is never zero.** A zero window is not
"unknown", it is a lie that a caller will act on — a context budgeter will
drop every prompt, a truncation helper will return the empty string, and the
failure is silent. So the degradation ladder is

    injected probe  ->  litellm model info  ->  local table  ->  documented floor

and the last rung is a positive, deliberately conservative number. The resolved
window always carries its ``source`` so a consumer can tell a measured value
from a floor.

Caching: the answer is cached per ``(api_base, model)``. Two endpoints serving
the same model name are NOT the same capability (a local Ollama on 8k and a
hosted frontier model on 1M share a model string in practice), and the base URL
is the only honest discriminator. An unknown endpoint is keyed by the empty
string rather than collapsed into every other unknown.

Nothing here opens a network connection by default. A live probe is available
but must be supplied by the caller (``probe=``) or opted into through
``runtime.model_router`` config, because a capability probe is a real billable
call and this module must never make one on its own.

**2. A model with no price row is ``unpriced``, never ``$0``** (R2-13). The
distinction is not cosmetic: the price ladder's cheapest tier is chosen by
comparing numbers, so an absent row compared as ``0.0`` makes an *unpriced*
model the *cheapest* one and the cost report then under-reports in exactly the
direction that spends money. :data:`PRICE_STATES` is the closed vocabulary
(``priced`` | ``free`` | ``unpriced``) and ``free`` is reachable ONLY by a
DECLARED zero-price row — never by absence of evidence. :func:`price_of`
returns the state alongside the numbers so a caller cannot accidentally report
an unknown price as a free call.

**3. An unknown capability is ``None``, never ``False``.** ``supports_tools``
and friends are tri-state: a registry that reported "does not support tools"
for a model nobody has described would refuse to route the entire world. A
declared ``False`` is a fact and is enforced; ``None`` is an unknown and is
reported as one.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# P1/W1 (T3): the ONE litellm choke point. Imported eagerly because it is
# stdlib-only and costs ~0 ms, and because the context-window rung below is
# the FIRST thing on a real call that needs litellm — so it is the rung a
# background preload has to be able to satisfy.
from . import latency

__all__ = [
    "CAPABILITY_SOURCES",
    "EFFORT_AUTO",
    "EFFORT_CHOICES",
    "EFFORT_DISABLED",
    "EFFORT_ENV_VAR",
    "EFFORT_INVALID",
    "EFFORT_LEVELS",
    "EFFORT_PICKER_FAMILIES",
    "EFFORT_PICKER_NONE",
    "EFFORT_PICKER_UNSUPPORTED",
    "EFFORT_SENT",
    "EFFORT_STATUSES",
    "EFFORT_SYNTHETIC",
    "EFFORT_UNSUPPORTED_LEVEL",
    "EFFORT_UNSUPPORTED_MODEL",
    "EFFORT_VARIANT_DROPPED",
    "EFFORT_VARIANT_EXACT",
    "EFFORT_VARIANT_MAPPED",
    "FALLBACK_CONTEXT_WINDOW",
    "KNOWN_CONTEXT_WINDOWS",
    "MODEL_PRICES",
    "PRICE_FREE",
    "PRICE_PRICED",
    "PRICE_STATES",
    "PRICE_UNPRICED",
    "REFUSAL_REASONS",
    "REFUSAL_REASON_NO_ALTERNATIVE",
    "REFUSAL_REASON_TOOLS",
    "REFUSAL_REASON_TOOLS_UNKNOWN",
    "REFUSAL_REASON_UNPRICED",
    "SYNTHETIC_PROVIDERS",
    "CapabilityError",
    "CapabilityRoutingRefused",
    "CostEstimate",
    "EffortKnob",
    "EffortPlan",
    "ModelCapability",
    "ScreenResult",
    "ToolUseRefusal",
    "cached_probe_count",
    "capability_of",
    "carry_picker_effort",
    "context_window",
    "context_window_info",
    "effort_family_for",
    "effort_from_env",
    "estimate_cost",
    "known_capabilities",
    "known_context_window",
    "known_effort_knobs",
    "lookup_capability",
    "map_effort",
    "map_picker_effort",
    "next_picker_level",
    "normalize_effort",
    "picker_variant",
    "picker_vocabulary",
    "price_of",
    "register_capability",
    "register_effort_knob",
    "reset_capability_registry",
    "reset_context_window_cache",
    "reset_effort_knobs",
    "resolve_context_window",
    "resolve_effort",
    "screen_candidates",
    "synthetic_effort_plan",
    "unknown_capability",
    "unregister_capability",
    "unregister_effort_knob",
    "variant_keybind",
]

#: Closed pricing vocabulary. ``unpriced`` is the answer for a model with no
#: price row and MUST NOT be collapsed into ``free``; ``free`` requires a
#: declared zero-price row.
PRICE_PRICED = "priced"
PRICE_FREE = "free"
PRICE_UNPRICED = "unpriced"
PRICE_STATES = (PRICE_PRICED, PRICE_FREE, PRICE_UNPRICED)

#: Where a capability row came from. ``builtin`` is this module's table,
#: ``registry`` a caller registration, ``config`` a task-config declaration.
CAPABILITY_SOURCES = ("builtin", "registry", "config")


class CapabilityError(ValueError):
    """A capability or price declaration that cannot be honoured.

    Raised by :func:`register_capability` for a malformed row. A capability
    declaration that cannot be understood is a REFUSAL, not a silent default:
    guessing a context window or a price is the failure mode this module
    exists to prevent.
    """

    def __init__(self, message: str, *, reason: str = "invalid_declaration") -> None:
        super().__init__(message)
        self.reason = reason


class CapabilityRoutingRefused(RuntimeError):
    """The router refused to select a target the capability registry rejects.

    This is a fail-closed refusal and it is deliberately loud: a task that
    cannot be routed must not silently receive the cheapest-looking tier.
    ``reason`` is one of the closed slugs in :data:`REFUSAL_REASONS` and
    ``alternatives`` names the candidates that were considered and rejected,
    so the trace explains the refusal instead of only asserting it.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        alternatives: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.model = model
        self.provider = provider
        self.alternatives = list(alternatives or [])


#: Closed refusal vocabulary. A refusal is machine-readable first: an operator
#: has to be able to count them and a log reader has to be able to filter them.
REFUSAL_REASON_UNPRICED = "unpriced_model"
REFUSAL_REASON_TOOLS = "tool_calling_unsupported"
REFUSAL_REASON_TOOLS_UNKNOWN = "tool_calling_unverified"
REFUSAL_REASON_NO_ALTERNATIVE = "no_capable_alternative"
REFUSAL_REASONS = (
    REFUSAL_REASON_UNPRICED,
    REFUSAL_REASON_TOOLS,
    REFUSAL_REASON_TOOLS_UNKNOWN,
    REFUSAL_REASON_NO_ALTERNATIVE,
)


@dataclass(frozen=True)
class ToolUseRefusal:
    """One recorded capability exclusion: what was rejected and why.

    Assumes ``model``/``provider`` name the CANDIDATE (not the winner) and
    ``reason`` is a slug from :data:`REFUSAL_REASONS`. ``detail`` is a short
    human sentence; it is evidence, never a control path.
    """

    provider: str
    model: str
    reason: str
    detail: str
    price_state: str = PRICE_UNPRICED
    supports_tools: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe row for the ledger/trace/eval report."""
        return {
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "detail": self.detail,
            "price_state": self.price_state,
            "supports_tools": self.supports_tools,
        }


@dataclass(frozen=True)
class ModelCapability:
    """One declared (provider, model) capability row.

    Every field except ``provider``/``model``/``price_state``/``source`` is
    tri-state: ``None`` means UNKNOWN and must never be read as ``False``.
    ``price_state`` is one of :data:`PRICE_STATES` and is derived, not
    declared: a row with both rates is ``priced`` or ``free``, a row with
    neither is ``unpriced``.
    """

    provider: str
    model: str
    context_window: Optional[int] = None
    supports_tools: Optional[bool] = None
    supports_reasoning: Optional[bool] = None
    supports_streaming: Optional[bool] = None
    input_cost_per_million: Optional[float] = None
    output_cost_per_million: Optional[float] = None
    price_state: str = PRICE_UNPRICED
    source: str = "registry"
    known: bool = True

    @property
    def priced(self) -> bool:
        """True when a real price row exists (i.e. NOT an unknown)."""
        return self.price_state in (PRICE_PRICED, PRICE_FREE)

    @property
    def free(self) -> bool:
        """True only for a DECLARED zero-price model. Never for an unknown."""
        return self.price_state == PRICE_FREE

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection for ledgers, traces and reports."""
        return {
            "provider": self.provider,
            "model": self.model,
            "context_window": self.context_window,
            "supports_tools": self.supports_tools,
            "supports_reasoning": self.supports_reasoning,
            "supports_streaming": self.supports_streaming,
            "input_cost_per_million": self.input_cost_per_million,
            "output_cost_per_million": self.output_cost_per_million,
            "price_state": self.price_state,
            "priced": self.priced,
            "free": self.free,
            "capability_source": self.source,
            "known": self.known,
        }


@dataclass(frozen=True)
class CostEstimate:
    """A cost number plus the STATE that makes it meaningful.

    ``price_state`` travels with the number because ``0.0`` is ambiguous on its
    own: it is a free call, an unpriced model, or a provider that reported
    nothing. A consumer that reports ``cost_usd`` without it will eventually
    report "free" for a call that was never priced.
    """

    cost_usd: float
    source: str
    price_state: str
    model: str
    priced: bool

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe row for the ledger/trace/eval report."""
        return {
            "cost_usd": round(float(self.cost_usd), 10),
            "cost_source": self.source,
            "price_state": self.price_state,
            "model": self.model,
            "cost_priced": self.priced,
        }


#: The documented floor used when nothing else knows the model's window. It is
#: small on purpose: a budget built from this value still fits, and a model
#: that really is smaller produces a provider error the router already records,
#: rather than a harness that silently sent an empty prompt.
FALLBACK_CONTEXT_WINDOW = 8_192

#: Providers the harness implements itself (see ``runtime.mock_provider``).
#: litellm's model metadata can never describe one of these, so asking it is
#: guaranteed to learn nothing while importing a provider SDK that costs
#: seconds. Every mocked worker, every eval arm, and every scripted stress
#: task would pay that import on its first model call — which is most of the
#: test suite. These models resolve through the table/fallback rungs instead,
#: and the reported ``source`` says so, exactly as before.
SYNTHETIC_PROVIDERS = frozenset({"mock", "scripted", "fake", "harness-mock"})

#: Substring -> window, matched case-insensitively against the model name. The
#: most specific (longest) matching key wins, so ``gpt-4.1-mini`` is not
#: shadowed by ``gpt-4``. Values are the commonly published windows; a stale
#: entry degrades budgeting slightly, never correctness, because the caller
#: always sees the ``source``.
KNOWN_CONTEXT_WINDOWS: Dict[str, int] = {
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1": 1_047_576,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5": 16_385,
    "o1-mini": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
    "o3": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    "qwen": 32_768,
    "glm": 128_000,
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    "gemini-2": 1_048_576,
    "stepfun": 32_768,
    "llama-3.1": 131_072,
    "llama-3.2": 131_072,
    "llama-3.3": 131_072,
    "mistral": 32_768,
    "mixtral": 32_768,
    "command-r": 128_000,
}

#: The ONE price table (USD per 1M tokens, ``(input, output)``). This was
#: historically ``runtime.model_router._PRICES``; the router now re-exports THIS
#: object under that name so every existing reader keeps working against one
#: authority rather than two tables that can drift.
#:
#: A key's PRESENCE is what makes a model priced. Absence is ``unpriced`` and
#: must never be read as ``0.0`` — see the module docstring. A ``(0.0, 0.0)``
#: row is a DELIBERATE free-tier declaration and is the only way to reach the
#: ``free`` state.
MODEL_PRICES: Dict[str, Tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 2.00),
    "gpt-4.1": (2.00, 8.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-sonnet-4-20250514": (3.00, 25.00),
    "deepseek-chat": (0.14, 0.28),
    # --- ablation proxy prices (free-tier routers; tokens are REAL, these
    # are published rates for comparable model classes -- see runtime/ablation.py):
    "qwen3.8-27b": (0.20, 0.60),  # mid-size open model class
    "longcat-2.0-free": (0.20, 0.60),  # Round-6 probe (rejected: pseudo-XML tool calls)
    "stepfun-3.7-flash": (
        0.20,
        0.60,
    ),  # Round-6 ablation cheap tier (small open model class)
    "z-ai/glm-5.3-free": (0.60, 2.20),  # frontier-class API rates proxy
}

#: Declared capabilities for models this harness actually routes to. A model
#: that is not here is UNKNOWN, not incapable: :func:`capability_of` synthesizes
#: a tri-state row rather than inventing a ``False``.
#:
#: ``supports_reasoning`` is a property of the RESPONSE SHAPE (a provider that
#: returns a separate ``reasoning_content`` field), not of whether the model
#: thinks. It drives the R2-14 ``truncated_reasoning`` outcome.
_BUILTIN_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "gpt-4o-mini": {"tools": True, "reasoning": False, "streaming": True},
    "gpt-4o": {"tools": True, "reasoning": False, "streaming": True},
    "gpt-4.1-mini": {"tools": True, "reasoning": False, "streaming": True},
    "gpt-4.1": {"tools": True, "reasoning": False, "streaming": True},
    "o1": {"tools": True, "reasoning": True, "streaming": True},
    "o1-mini": {"tools": True, "reasoning": True, "streaming": True},
    "o3": {"tools": True, "reasoning": True, "streaming": True},
    "o3-mini": {"tools": True, "reasoning": True, "streaming": True},
    "claude-3-5-haiku": {"tools": True, "reasoning": False, "streaming": True},
    "claude-3-5-sonnet": {"tools": True, "reasoning": False, "streaming": True},
    "claude-3-7-sonnet": {"tools": True, "reasoning": False, "streaming": True},
    "claude-sonnet-4": {"tools": True, "reasoning": False, "streaming": True},
    "claude-opus-4": {"tools": True, "reasoning": False, "streaming": True},
    "deepseek-chat": {"tools": True, "reasoning": False, "streaming": True},
    "deepseek-reasoner": {"tools": True, "reasoning": True, "streaming": True},
    "qwen3.8-27b": {"tools": True, "reasoning": False, "streaming": True},
    "stepfun-3.7-flash": {"tools": True, "reasoning": False, "streaming": True},
    "z-ai/glm-5.3-free": {"tools": True, "reasoning": False, "streaming": True},
}

_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_CACHE_LOCK = threading.RLock()
_PROBE_CALLS: Dict[Tuple[str, str], int] = {}
_REGISTRY: Dict[Tuple[str, str], ModelCapability] = {}


def _price_state(input_cost: Optional[float], output_cost: Optional[float]) -> str:
    """Derive the closed price state from a (possibly absent) rate pair.

    Only a pair that is BOTH present is priced; a partial pair is a malformed
    declaration and is reported as ``unpriced`` rather than half-priced, so a
    cost report never bills half a call as if it were whole.
    """
    if input_cost is None or output_cost is None:
        return PRICE_UNPRICED
    if float(input_cost) <= 0.0 and float(output_cost) <= 0.0:
        return PRICE_FREE
    return PRICE_PRICED


def _optional_rate(value: Any, field: str) -> Optional[float]:
    """Coerce a declared rate, treating absent/None as unpriced.

    Raises :class:`CapabilityError` for a present-but-unusable value (a
    string, a negative number) — a rate that cannot be read is a broken
    declaration, and silently coercing it to ``None`` would turn a typo into
    an "unknown price" that a cost report then prints as free.
    """
    if value is None or value == "":
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise CapabilityError(
            f"{field} must be a number or absent, got {value!r:.60}",
            reason="invalid_price",
        ) from None
    if rate < 0:
        raise CapabilityError(
            f"{field} must not be negative, got {rate!r}", reason="invalid_price"
        )
    return rate


def _optional_bool(value: Any, field: str) -> Optional[bool]:
    """Coerce a tri-state capability flag, preserving UNKNOWN as ``None``.

    Raises :class:`CapabilityError` for a present-but-unusable value, because a
    typo must not silently become "unknown" (which is eligible) or "false"
    (which is an enforcement).
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise CapabilityError(
        f"{field} must be true/false/absent, got {value!r:.40}", reason="invalid_flag"
    )


def price_of(model: Optional[str]) -> Dict[str, Any]:
    """Return the price row for ``model`` with its explicit STATE.

    Assumes ``model`` is a bare model name (a ``provider/`` prefix is
    stripped and matched, because a tier's litellm string is ``openai/gpt-4o``
    while the table key is ``gpt-4o``). Never invents a number: a model with no
    row returns ``input_cost_per_million=None`` and
    ``price_state="unpriced"``, which is NOT the same answer as ``0.0``.
    """
    name = str(model or "").strip()
    if name and "/" in name:
        name = name.rsplit("/", 1)[-1]
    prices = MODEL_PRICES.get(name)
    if prices is None:
        return {
            "model": name or None,
            "input_cost_per_million": None,
            "output_cost_per_million": None,
            "price_state": PRICE_UNPRICED,
            "priced": False,
            "free": False,
        }
    cost_in, cost_out = float(prices[0]), float(prices[1])
    state = _price_state(cost_in, cost_out)
    return {
        "model": name,
        "input_cost_per_million": cost_in,
        "output_cost_per_million": cost_out,
        "price_state": state,
        "priced": state != PRICE_UNPRICED,
        "free": state == PRICE_FREE,
    }


def register_capability(
    entry: Mapping[str, Any] | ModelCapability,
    *,
    source: str = "registry",
) -> ModelCapability:
    """Register (or replace) one (provider, model) capability row.

    Assumes ``entry`` is a mapping using the :class:`ModelCapability` field
    names, or an already-built instance. ``provider`` may be ``""`` for a
    provider-agnostic row; lookup then falls back to a model-name match
    (see :func:`lookup_capability`). A malformed declaration RAISES
    :class:`CapabilityError` naming the offending field — a broken capability
    row is a refusal, never a default. An absent price pair is accepted and
    recorded as ``unpriced``; that is the honest state, not an error.

    ``source`` must be one of :data:`CAPABILITY_SOURCES`; the value rides
    every receipt so a reader can tell a built-in row from a caller's claim.
    """
    if isinstance(entry, ModelCapability):
        row = replace(entry, source=source)
        _validate_row(row)
        with _CACHE_LOCK:
            _REGISTRY[(row.provider.lower(), row.model.lower())] = row
        return row
    if not isinstance(entry, Mapping):
        raise CapabilityError(
            f"capability entry must be a mapping, got {type(entry).__name__}",
            reason="invalid_declaration",
        )
    if source not in CAPABILITY_SOURCES:
        raise CapabilityError(
            f"capability source must be one of {CAPABILITY_SOURCES}, got {source!r}",
            reason="invalid_declaration",
        )
    model = str(entry.get("model") or "").strip()
    if not model:
        raise CapabilityError(
            "capability entry requires a model name", reason="invalid_declaration"
        )
    provider = str(entry.get("provider") or "").strip().lower()
    cost_in = _optional_rate(
        entry.get("input_cost_per_million"), "input_cost_per_million"
    )
    cost_out = _optional_rate(
        entry.get("output_cost_per_million"), "output_cost_per_million"
    )
    window = entry.get("context_window")
    if window is not None:
        try:
            window = int(window)
        except (TypeError, ValueError):
            raise CapabilityError(
                f"context_window must be an integer or absent, got {window!r:.40}",
                reason="invalid_window",
            ) from None
        if window <= 0:
            raise CapabilityError(
                f"context_window must be positive, got {window!r}",
                reason="invalid_window",
            )
    row = ModelCapability(
        provider=provider,
        model=model,
        context_window=window,
        supports_tools=_optional_bool(
            entry.get("supports_tools", entry.get("tools")), "supports_tools"
        ),
        supports_reasoning=_optional_bool(
            entry.get("supports_reasoning", entry.get("reasoning")),
            "supports_reasoning",
        ),
        supports_streaming=_optional_bool(
            entry.get("supports_streaming", entry.get("streaming")),
            "supports_streaming",
        ),
        input_cost_per_million=cost_in,
        output_cost_per_million=cost_out,
        price_state=_price_state(cost_in, cost_out),
        source=source,
        known=True,
    )
    _validate_row(row)
    with _CACHE_LOCK:
        _REGISTRY[(provider, model.lower())] = row
    return row


def _validate_row(row: ModelCapability) -> None:
    """Reject a row whose price state was hand-declared inconsistently."""
    if row.price_state not in PRICE_STATES:
        raise CapabilityError(
            f"price_state must be one of {PRICE_STATES}, got {row.price_state!r}",
            reason="invalid_declaration",
        )
    if row.source not in CAPABILITY_SOURCES:
        raise CapabilityError(
            f"capability source must be one of {CAPABILITY_SOURCES}, got {row.source!r}",
            reason="invalid_declaration",
        )
    derived = _price_state(row.input_cost_per_million, row.output_cost_per_million)
    if row.price_state != derived:
        raise CapabilityError(
            f"price_state {row.price_state!r} contradicts the declared rates "
            f"(derived {derived!r})",
            reason="invalid_declaration",
        )


def unregister_capability(provider: Optional[str], model: str) -> bool:
    """Remove one registered row. Returns whether a row was actually removed.

    Built-in rows are NOT removable — a caller can overlay a provider-specific
    row but cannot delete the documented default, because the router's
    fallback-to-defaults behaviour depends on it existing.

    Both spellings of the model name are tried, because
    ``register_capability`` keys on the string exactly as written and
    ``lookup_capability`` accepts either. A removal that only understood one
    spelling would leave a prefixed registration (``z-ai/glm-5.3-free``) in the
    registry forever with no way to take it back out — a declaration an
    operator cannot withdraw.
    """
    prov = str(provider or "").strip().lower()
    raw = str(model or "").strip().lower()
    bare = raw.rsplit("/", 1)[-1] if "/" in raw else raw
    keys = [(prov, raw)]
    if bare != raw:
        keys.append((prov, bare))
    with _CACHE_LOCK:
        for key in keys:
            if _REGISTRY.pop(key, None) is not None:
                return True
    return False


def reset_capability_registry() -> None:
    """Drop every caller-registered row (tests and fresh processes).

    Context-window caching is a SEPARATE cache and is untouched; it is keyed by
    endpoint+model and is still exercised by the router.
    """
    with _CACHE_LOCK:
        _REGISTRY.clear()


def known_capabilities() -> List[ModelCapability]:
    """Return every effective row (built-in overlaid by registrations).

    Deterministic order: by ``(provider, model)`` so an ablation report and a
    test can diff two runs without sorting first.
    """
    merged: Dict[Tuple[str, str], ModelCapability] = {}
    for name, flags in _BUILTIN_CAPABILITIES.items():
        prices = MODEL_PRICES.get(name)
        cost_in = float(prices[0]) if prices else None
        cost_out = float(prices[1]) if prices else None
        merged[("", name)] = ModelCapability(
            provider="",
            model=name,
            context_window=known_context_window(name)[0] or None,
            supports_tools=flags["tools"],
            supports_reasoning=flags["reasoning"],
            supports_streaming=flags["streaming"],
            input_cost_per_million=cost_in,
            output_cost_per_million=cost_out,
            price_state=_price_state(cost_in, cost_out),
            source="builtin",
            known=True,
        )
    with _CACHE_LOCK:
        merged.update(_REGISTRY)
    return [merged[key] for key in sorted(merged)]


def unknown_capability(
    provider: Optional[str] = None, model: Optional[str] = None
) -> ModelCapability:
    """Return the honest UNKNOWN row: every flag is ``None``, price unpriced.

    This is what a model nobody has described resolves to. It is deliberately
    NOT a capability denial — an unknown model is routed (subject to the
    caller's unpriced policy) and its price is reported as unknown, never free.
    """
    return ModelCapability(
        provider=str(provider or "").strip().lower(),
        model=str(model or "").strip(),
        context_window=None,
        supports_tools=None,
        supports_reasoning=None,
        supports_streaming=None,
        input_cost_per_million=None,
        output_cost_per_million=None,
        price_state=PRICE_UNPRICED,
        source="registry",
        known=False,
    )


def _registered_by_model(name: str) -> Optional[ModelCapability]:
    """Return the deterministic FIRST registered row for ``name``, any provider.

    Assumes ``name`` is already bare and lower-cased. This is the
    provider-AGNOSTIC fallback for the two places that genuinely do not know
    the provider: the cost report (the ledger row is priced from a model name)
    and a caller that named a model without one. Without it, a declared row
    would be invisible to exactly the consumer it exists for, and the cost
    report would report ``unpriced`` for a model the operator priced. When
    several providers registered the same name, the lexicographically first
    wins so two runs cannot disagree.

    A registered row is also matched by its BARE name: ``register_capability``
    keys on the model string exactly as the caller gave it, and for a
    bring-your-own router that string is usually prefixed
    (``z-ai/glm-5.3-free``, ``vendor/model-x``) -- litellm's own routing form.
    Matching the bare name here is what makes a prefixed registration visible
    to the provider-agnostic consumer; without it the operator's declared rate
    was invisible and the report said ``unpriced``.
    """
    matches = [
        row
        for (provider, key), row in list(_REGISTRY.items())
        if key == name or key.rsplit("/", 1)[-1] == name
    ]
    if not matches:
        return None
    matches.sort(key=lambda row: (row.provider, row.model))
    return matches[0]


def lookup_capability(
    provider: Optional[str], model: Optional[str]
) -> Optional[ModelCapability]:
    """Return the declared row for ``(provider, model)``, or ``None``.

    Resolution order, most specific first: an exact ``(provider, model)``
    registration **as written**, then the same with a ``provider/``-prefixed
    model string reduced to its bare name, then a provider-agnostic
    ``("", model)`` registration, then ANY registered row for that model name
    (the deterministic provider-agnostic fallback -- the cost report and a
    provider-less caller both need it), then the built-in provider-agnostic
    row. Returns ``None`` -- never a fabricated row -- for a model nobody has
    described; use :func:`capability_of` for a total function.

    **The two rungs that reduce the prefix are both load-bearing, and their
    absence was a real pricing defect.** ``register_capability`` keys on the
    model string exactly as the caller wrote it, and for a bring-your-own
    router that string is normally prefixed -- ``z-ai/glm-5.3-free``,
    ``vendor/model-x`` -- because that is litellm's own routing form. A lookup
    that reduced the prefix before consulting the registry, against a registry
    that had stored it un-reduced, could never match: an operator who declared
    a rate for their router model got ``price_state='unpriced'`` and
    ``cost_usd=0.0`` back. That is the direction the honesty rules name --
    a report that under-counts cost in the direction that spends money -- and
    it also silently discarded the operator's own explicit instruction to pay.
    So the raw name is tried FIRST (rung 1), the reduced name second (rung 2),
    and the provider-agnostic fallback matches either spelling.
    """
    raw = str(model or "").strip()
    if not raw:
        return None
    name = raw.rsplit("/", 1)[-1] if "/" in raw else raw
    prov = str(provider or "").strip().lower()
    raw_key = raw.lower()
    name_key = name.lower()
    with _CACHE_LOCK:
        exact = _REGISTRY.get((prov, raw_key))
        if exact is not None:
            return exact
        if name_key != raw_key:
            exact = _REGISTRY.get((prov, name_key))
            if exact is not None:
                return exact
        wildcard = _REGISTRY.get(("", raw_key))
        if wildcard is not None:
            return wildcard
        wildcard = _REGISTRY.get(("", name_key))
    if wildcard is not None:
        return wildcard
    by_name = _registered_by_model(name.lower())
    if by_name is not None:
        return by_name
    flags = _BUILTIN_CAPABILITIES.get(name)
    prices = MODEL_PRICES.get(name)
    if flags is None and prices is None:
        return None
    cost_in = float(prices[0]) if prices else None
    cost_out = float(prices[1]) if prices else None
    window, _source = known_context_window(name)
    return ModelCapability(
        provider=prov,
        model=name,
        context_window=window or None,
        supports_tools=flags["tools"] if flags else None,
        supports_reasoning=flags["reasoning"] if flags else None,
        supports_streaming=flags["streaming"] if flags else None,
        input_cost_per_million=cost_in,
        output_cost_per_million=cost_out,
        price_state=_price_state(cost_in, cost_out),
        source="builtin",
        known=True,
    )


def capability_of(
    provider: Optional[str] = None, model: Optional[str] = None
) -> ModelCapability:
    """Return the declared row, or an honest UNKNOWN row. Never raises.

    This is the total function the router uses: it must always have something
    to report, and the honest answer for an undescribed model is "unknown",
    which is distinguishable from "does not support tools" and from "free".
    """
    row = lookup_capability(provider, model)
    if row is not None:
        return row
    return unknown_capability(provider, model)


def declared_rates(model: Optional[str]) -> Dict[str, Any]:
    """Return the effective rate pair for ``model`` and where it came from.

    Precedence: the shared :data:`MODEL_PRICES` table first (it is the
    documented default for every model the harness routes to), then a
    REGISTERED capability row's declared rates. The registry has to be
    consulted here, not only by the router, because a price that only the
    routing screen can see is a price the cost report would silently drop —
    and a cost report that drops a declared rate is exactly the "unknown
    priced as free" failure this module exists to prevent.
    """
    table = price_of(model)
    if table["priced"]:
        return {**table, "price_origin": "price_table"}
    row = lookup_capability(None, model)
    if row is not None and row.priced:
        return {
            "model": row.model or None,
            "input_cost_per_million": row.input_cost_per_million,
            "output_cost_per_million": row.output_cost_per_million,
            "price_state": row.price_state,
            "priced": True,
            "free": row.free,
            "price_origin": f"registry:{row.source}",
        }
    return {**table, "price_origin": "none"}


def estimate_cost(
    model: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cached_input_tokens: int = 0,
    cache_read_discount: float = 1.0,
    input_cost_per_million: Optional[float] = None,
    output_cost_per_million: Optional[float] = None,
    provider_cost_usd: Optional[float] = None,
) -> CostEstimate:
    """Estimate a call's cost AND the price state that makes it meaningful.

    Precedence, highest first: an explicit configured rate pair, a
    caller-supplied provider-reported cost, then the effective rate from
    :func:`declared_rates` (the shared table, else a registered capability
    row). The final case is the one that used to be a lie: it returns
    ``cost_usd=0.0`` **with** ``price_state="unpriced"`` and ``priced=False``,
    so a report can say "we do not know what this cost" instead of "$0".

    ``cache_read_discount`` prices the cached portion of the prompt at a
    fraction of the normal input rate (Anthropic publishes 0.1x). It only
    applies when the model is actually priced; on an unpriced model a discount
    of zero is still zero and is still UNKNOWN.
    """
    name = str(model or "").strip()
    if name and "/" in name:
        name = name.rsplit("/", 1)[-1]
    if provider_cost_usd is not None:
        return CostEstimate(
            cost_usd=float(provider_cost_usd),
            source="provider",
            price_state=PRICE_PRICED,
            model=name or None,
            priced=True,
        )
    if input_cost_per_million is not None and output_cost_per_million is not None:
        p_in = float(input_cost_per_million)
        p_out = float(output_cost_per_million)
        source = "configured"
    else:
        rates = declared_rates(name)
        p_in = rates["input_cost_per_million"]
        p_out = rates["output_cost_per_million"]
        source = "price_table" if rates["priced"] else PRICE_UNPRICED
    if p_in is None or p_out is None:
        return CostEstimate(
            cost_usd=0.0,
            source=PRICE_UNPRICED,
            price_state=PRICE_UNPRICED,
            model=name or None,
            priced=False,
        )
    state = _price_state(p_in, p_out)
    cached = max(0, min(int(cached_input_tokens or 0), int(prompt_tokens or 0)))
    billable_input = max(0, int(prompt_tokens or 0) - cached)
    cost = (billable_input / 1e6) * p_in + (int(completion_tokens or 0) / 1e6) * p_out
    if cached and p_in and float(cache_read_discount) != 1.0:
        cost += (cached / 1e6) * p_in * float(cache_read_discount)
        source = f"{source}+cache_discount"
    return CostEstimate(
        cost_usd=cost,
        source=source,
        price_state=state,
        model=name or None,
        priced=True,
    )


@dataclass(frozen=True)
class ScreenResult:
    """The outcome of screening routing candidates against the registry.

    ``selected`` is the winning candidate mapping (a router target dict), or
    ``None`` when EVERY candidate was refused — in which case
    :func:`screen_candidates` has already raised, so a caller that receives
    this object always has a selection. ``refusals`` is the full record of what
    was rejected and why, in preference order, and is what a ledger row / trace
    event / eval report publishes: a capability exclusion is a CONSTRAINT with
    an audit trail, not a silent preference.
    """

    selected: Dict[str, Any]
    refusals: List[ToolUseRefusal] = ()
    considered: List[Dict[str, Any]] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe receipt for the ledger/trace/eval report."""
        return {
            "selected_model": self.selected.get("model"),
            "selected_provider": self.selected.get("provider"),
            "refusals": [refusal.to_dict() for refusal in self.refusals],
            "considered": list(self.considered),
            "refusal_count": len(self.refusals),
        }


def _candidate_name(candidate: Mapping[str, Any]) -> str:
    """Return the candidate's model name with any provider prefix reduced."""
    name = str(candidate.get("model") or "").strip()
    return name.rsplit("/", 1)[-1] if "/" in name else name


def screen_candidates(
    candidates: List[Mapping[str, Any]],
    *,
    tool_driven: bool = False,
    allow_unpriced: bool = False,
    strict_tools: bool = False,
) -> ScreenResult:
    """Select the first capability-eligible candidate, or REFUSE.

    ``candidates`` is a preference-ordered list of router target mappings
    (``provider``/``model`` plus the endpoint keys the router needs). The order
    is the caller's economics; this function only removes candidates the
    capability registry says cannot work, so the price ladder still decides
    among what is ELIGIBLE.

    A candidate is refused when:

    * it has no price row and ``allow_unpriced`` is false — an unknown price is
      not a cheap price, and routing on it is what makes a cost report wrong in
      the direction that spends money (:data:`REFUSAL_REASON_UNPRICED`);
    * the call is tool-driven (``tool_driven``) and the model is DECLARED
      unable to call tools (:data:`REFUSAL_REASON_TOOLS`). This is a capability
      constraint, not a preference: price cannot buy it back;
    * the call is tool-driven, ``strict_tools`` is on, and tool support is
      merely UNKNOWN (:data:`REFUSAL_REASON_TOOLS_UNKNOWN`). Off by default —
      "not described" is not "cannot", and a registry that had to be complete
      before anything could route would refuse the whole world.

    Raises :class:`CapabilityRoutingRefused` when nothing is eligible, naming
    the reason and every candidate considered. Failing loudly is the point: a
    task that cannot be routed must not silently get the cheapest-looking tier.
    """
    ordered = [dict(candidate) for candidate in (candidates or []) if candidate]
    considered: List[Dict[str, Any]] = []
    refusals: List[ToolUseRefusal] = []
    reasons: List[str] = []
    for candidate in ordered:
        provider = str(candidate.get("provider") or "").strip().lower()
        model = _candidate_name(candidate)
        capability = capability_of(provider or None, model)
        considered.append(
            {
                "provider": provider,
                "model": model,
                "price_state": capability.price_state,
                "supports_tools": capability.supports_tools,
                "capability_known": capability.known,
            }
        )
        if not capability.priced and not allow_unpriced:
            refusals.append(
                ToolUseRefusal(
                    provider=provider,
                    model=model,
                    reason=REFUSAL_REASON_UNPRICED,
                    detail=(
                        "no price row for this model; an unpriced model is not a "
                        "cheap model and the router will not select one without "
                        "an explicit allowance"
                    ),
                    price_state=capability.price_state,
                    supports_tools=capability.supports_tools,
                )
            )
            reasons.append(REFUSAL_REASON_UNPRICED)
            continue
        if tool_driven and capability.supports_tools is False:
            refusals.append(
                ToolUseRefusal(
                    provider=provider,
                    model=model,
                    reason=REFUSAL_REASON_TOOLS,
                    detail=(
                        "model is declared unable to emit tool calls and this is a "
                        "tool-driven loop; price cannot buy tool support"
                    ),
                    price_state=capability.price_state,
                    supports_tools=False,
                )
            )
            reasons.append(REFUSAL_REASON_TOOLS)
            continue
        if tool_driven and strict_tools and capability.supports_tools is not True:
            refusals.append(
                ToolUseRefusal(
                    provider=provider,
                    model=model,
                    reason=REFUSAL_REASON_TOOLS_UNKNOWN,
                    detail=(
                        "tool-calling support is unverified for this model and the "
                        "run requires verified tool support"
                    ),
                    price_state=capability.price_state,
                    supports_tools=capability.supports_tools,
                )
            )
            reasons.append(REFUSAL_REASON_TOOLS_UNKNOWN)
            continue
        return ScreenResult(
            selected=candidate, refusals=tuple(refusals), considered=tuple(considered)
        )
    # When every candidate was refused for the SAME reason, that reason IS the
    # diagnosis and naming it beats the generic "nothing was eligible" — an
    # operator who sees "all three tiers are tool-incapable" can act; one who
    # sees "no_capable_alternative" has to go read the refusal list. Mixed
    # reasons really are "no single explanation", so they keep the generic slug.
    unique_reasons = {refusal.reason for refusal in refusals}
    reason = (
        reasons[0]
        if len(unique_reasons) == 1 and reasons
        else REFUSAL_REASON_NO_ALTERNATIVE
    )
    raise CapabilityRoutingRefused(
        "no capability-eligible model target: "
        + (
            "; ".join(f"{r.model} ({r.reason})" for r in refusals)
            or "no candidates were supplied"
        ),
        reason=reason,
        model=refusals[0].model if refusals else None,
        provider=refusals[0].provider if refusals else None,
        alternatives=[refusal.to_dict() for refusal in refusals],
    )


def _endpoint_key(api_base: Optional[str]) -> str:
    """Return a stable, non-secret cache key for a provider base URL.

    The URL can embed credentials, so the cache stores a SHA-256 fingerprint
    instead of the URL itself. An absent base URL is its own key rather than an
    alias for every other absent URL.
    """
    if not api_base:
        return ""
    return hashlib.sha256(str(api_base).encode("utf-8", "replace")).hexdigest()[:16]


def known_context_window(model: Optional[str]) -> Tuple[int, str]:
    """Return ``(window, source)`` from the local table, or ``(0, "")``.

    Longest matching key wins so a specific entry is never shadowed by a
    shorter prefix of itself.
    """
    name = str(model or "").strip().lower()
    if not name:
        return 0, ""
    best_key = ""
    for key in KNOWN_CONTEXT_WINDOWS:
        if key in name and len(key) > len(best_key):
            best_key = key
    if not best_key:
        return 0, ""
    return int(KNOWN_CONTEXT_WINDOWS[best_key]), f"table:{best_key}"


def _from_litellm(
    model: Optional[str], provider: Optional[str] = None
) -> Tuple[int, str]:
    """Ask litellm's model metadata, returning ``(0, "")`` when it cannot.

    litellm prints a provider-list banner for an unknown model. That banner
    would corrupt a ``--json`` document (this harness guarantees stdout is
    exactly one JSON object there), so the call is made with stdout captured
    and the captured text is discarded.

    A harness-implemented provider (see :data:`SYNTHETIC_PROVIDERS`) is
    refused before the import: the answer is known to be "no metadata", and
    the import is the single most expensive thing this ladder does.

    P1/W1: the import is routed through ``runtime.latency.ensure_litellm``,
    the ONE choke point every ``runtime/`` import of litellm uses. That is
    what lets a background preload overlap this rung without this call
    observing a half-initialised module, and it is why a preload failure
    degrades to this exact lazy path rather than to a broken one.
    """
    name = str(model or "").strip()
    if not name:
        return 0, ""
    if str(provider or "").strip().lower() in SYNTHETIC_PROVIDERS:
        return 0, ""
    try:
        litellm = latency.ensure_litellm()
    except Exception:
        return 0, ""
    getter = getattr(litellm, "get_model_info", None)
    if not callable(getter):
        return 0, ""
    try:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            info = getter(name) or {}
    except Exception:
        return 0, ""
    if not isinstance(info, Mapping):
        return 0, ""
    for key in ("max_input_tokens", "max_tokens", "context_window"):
        try:
            value = int(info.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value, f"litellm:{key}"
    return 0, ""


def _probe_arity(probe: Callable[..., Any]) -> int:
    """Return how many positional arguments a probe accepts (0..3, capped).

    ``TypeError`` raised *inside* a probe must not be mistaken for an arity
    mismatch, so the signature is read once instead of relying on trial calls.
    """
    try:
        import inspect

        parameters = list(inspect.signature(probe).parameters.values())
    except (TypeError, ValueError, ImportError):
        return 0
    positional = 0
    for parameter in parameters:
        if parameter.kind in (
            parameter.POSITIONAL_ONLY,
            parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
        elif parameter.kind is parameter.VAR_POSITIONAL:
            return 3
    return min(positional, 3)


def _from_probe(
    probe: Optional[Callable[..., Any]],
    model: Optional[str],
    provider: Optional[str],
    api_base: Optional[str],
) -> Tuple[int, str]:
    """Run a caller-supplied probe, accepting any positive integer it returns.

    A probe is allowed to raise or return nonsense: both degrade to "no
    answer" here. A probe that returns a non-positive value is treated as a
    REFUSAL, never as a zero window.
    """
    if probe is None:
        return 0, ""
    arguments = {
        0: (),
        1: (model,),
        2: (model, provider),
        3: (model, provider, api_base),
    }.get(_probe_arity(probe), (model, provider, api_base))
    try:
        answer = probe(*arguments)
    except Exception:
        return 0, ""
    if isinstance(answer, Mapping):
        for key in ("context_window", "max_input_tokens", "max_tokens"):
            try:
                value = int(answer.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value, "probe"
        return 0, ""
    try:
        value = int(answer)
    except (TypeError, ValueError):
        return 0, ""
    return (value, "probe") if value > 0 else (0, "")


def resolve_context_window(
    model: Optional[str],
    *,
    provider: Optional[str] = None,
    api_base: Optional[str] = None,
    probe: Optional[Callable[..., Any]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Resolve a model's context window, caching per (endpoint, model).

    ``probe`` is an optional callable the caller supplies (a live capability
    request, a test double). It is invoked AT MOST ONCE per (endpoint, model)
    even across callers, which is the whole point of the cache: capability
    probing is a network call and a billable one.

    The returned dict always has a strictly positive ``context_window``.
    """
    endpoint = _endpoint_key(api_base)
    model_name = str(model or "").strip()
    key = (endpoint, model_name)
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is not None:
            with _CACHE_LOCK:
                hits = _PROBE_CALLS.get(key, 0)
            return {**cached, "cache": "hit", "probe_calls": hits}
    probed = probe is not None
    if probed:
        with _CACHE_LOCK:
            _PROBE_CALLS[key] = _PROBE_CALLS.get(key, 0) + 1
    window, source = _from_probe(probe, model_name, provider, api_base)
    if window <= 0:
        window, source = _from_litellm(model_name, provider)
    if window <= 0:
        window, source = known_context_window(model_name)
    if window <= 0:
        window, source = FALLBACK_CONTEXT_WINDOW, "fallback"
    record: Dict[str, Any] = {
        "context_window": int(window),
        "context_window_source": source,
        "model": model_name,
        "api_base_sha256": endpoint or None,
        "cache": "miss",
    }
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = record
    with _CACHE_LOCK:
        calls = _PROBE_CALLS.get(key, 0)
    return {**record, "probe_calls": calls}


def context_window_info(
    model: Optional[str],
    *,
    provider: Optional[str] = None,
    api_base: Optional[str] = None,
    probe: Optional[Callable[..., Any]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Alias of :func:`resolve_context_window` (the documented public name)."""
    return resolve_context_window(
        model,
        provider=provider,
        api_base=api_base,
        probe=probe,
        use_cache=use_cache,
    )


def context_window(
    model: Optional[str],
    *,
    provider: Optional[str] = None,
    api_base: Optional[str] = None,
    probe: Optional[Callable[..., Any]] = None,
    use_cache: bool = True,
) -> int:
    """Return only the resolved window. Always strictly positive."""
    return int(
        resolve_context_window(
            model,
            provider=provider,
            api_base=api_base,
            probe=probe,
            use_cache=use_cache,
        )["context_window"]
    )


def reset_context_window_cache() -> None:
    """Forget every cached window and probe count (tests and fresh runs)."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _PROBE_CALLS.clear()


def cached_probe_count() -> Dict[str, int]:
    """Return the per-endpoint probe call count (evidence the cache works)."""
    with _CACHE_LOCK:
        return {
            f"{endpoint}|{model}": count
            for (endpoint, model), count in _PROBE_CALLS.items()
        }


# ---------------------------------------------------------------------------
# Effort ladder (AGT-08) - the ONE authority for "how hard did the model think"
# ---------------------------------------------------------------------------
#
# The rule this section exists to enforce: **a level that is not sent is
# reported as not sent.** "Set to high" that silently does nothing is worse
# than not offering the setting, so every answer is an ``EffortPlan`` carrying
# a CLOSED status:
#
#   ``sent``              a real provider parameter was placed on the request
#   ``auto``              no level requested; nothing was sent (the default)
#   ``unsupported_model`` the model family has no declared effort knob
#   ``unsupported_level`` the family has a knob but not for this level
#   ``disabled``          the caller pinned ``effort_parameter`` off
#   ``synthetic``         a harness-served provider that ignores parameters
#   ``invalid``           the requested value is not a level; named
#
# ``EffortPlan.parameters`` is the ONLY thing a caller may merge into a
# request. It is empty for every status except ``sent``, which is what makes
# "unsupported" structurally unable to become "silently ignored".

#: The closed ladder. ``auto`` means "send nothing and let the provider
#: decide", which is the shipped default because it is byte-identical to the
#: pre-AGT-08 request.
EFFORT_AUTO = "auto"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_CHOICES = (EFFORT_AUTO, *EFFORT_LEVELS)

#: Where a level may come from when nobody named it explicitly. Precedence is
#: call-level override > ``Task.config`` > this variable > ``auto``.
EFFORT_ENV_VAR = "NEO_EFFORT"

#: The closed status vocabulary. A caller may add a KNOB, never a status.
EFFORT_SENT = "sent"
EFFORT_UNSUPPORTED_MODEL = "unsupported_model"
EFFORT_UNSUPPORTED_LEVEL = "unsupported_level"
EFFORT_DISABLED = "disabled"
EFFORT_SYNTHETIC = "synthetic"
EFFORT_INVALID = "invalid"
EFFORT_STATUSES = (
    EFFORT_AUTO,
    EFFORT_SENT,
    EFFORT_UNSUPPORTED_MODEL,
    EFFORT_UNSUPPORTED_LEVEL,
    EFFORT_DISABLED,
    EFFORT_SYNTHETIC,
    EFFORT_INVALID,
)

#: Accepts a few spellings a person actually types. Anything else is reported
#: as ``invalid`` with the value echoed, never rounded to a level.
_EFFORT_ALIASES = {
    "": EFFORT_AUTO,
    "auto": EFFORT_AUTO,
    "default": EFFORT_AUTO,
    "off": EFFORT_AUTO,
    "lo": "low",
    "med": "medium",
    "mid": "medium",
    "hi": "high",
    "extra-high": "xhigh",
    "x-high": "xhigh",
    "ultra": "max",
    "highest": "max",
}


@dataclass(frozen=True)
class EffortKnob:
    """One provider family's REAL effort parameter.

    ``parameter`` is the wire name the provider documents; ``values`` maps
    each accepted level to the value for that parameter. A level absent from
    ``values`` is ``unsupported_level``, never clamped to the nearest one -
    silently sending ``high`` for a request that asked for ``max`` is the
    exact failure this type prevents.
    """

    family: str
    parameter: str
    values: Mapping[str, Any]
    note: str = ""

    def __post_init__(self) -> None:
        name = str(self.family or "").strip().lower()
        if not name:
            raise CapabilityError("effort knob needs a provider family")
        if not str(self.parameter or "").strip():
            raise CapabilityError(
                f"effort knob for {name} needs a real provider parameter name"
            )
        unknown = sorted(set(self.values) - set(EFFORT_LEVELS))
        if unknown:
            raise CapabilityError(
                f"effort knob for {name} declares levels outside the ladder: {unknown}"
            )
        object.__setattr__(self, "family", name)
        object.__setattr__(self, "parameter", str(self.parameter).strip())
        object.__setattr__(self, "values", dict(self.values))

    @property
    def levels(self) -> Tuple[str, ...]:
        """The levels this knob accepts, in ladder order."""
        return tuple(level for level in EFFORT_LEVELS if level in self.values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family": self.family,
            "parameter": self.parameter,
            "levels": list(self.levels),
            "note": self.note,
        }


#: The built-in knobs. Every entry is a parameter a provider actually
#: documents; nothing here is a Neo invention that would be a 400 upstream.
_BUILTIN_EFFORT_KNOBS: Dict[str, EffortKnob] = {
    knob.family: knob
    for knob in (
        # OpenAI's documented reasoning-control parameter. Three levels, so
        # ``xhigh``/``max`` are an explicit unsupported_level.
        EffortKnob(
            family="openai",
            parameter="reasoning_effort",
            values={"low": "low", "medium": "medium", "high": "high"},
            note="OpenAI reasoning_effort; this family has three levels",
        ),
        # Anthropic's documented extended-thinking budget. All five levels
        # are expressible; the provider's own maximum still governs and a
        # request above it is the provider's refusal, not a silent clamp.
        EffortKnob(
            family="anthropic",
            parameter="thinking",
            values={
                "low": {"type": "enabled", "budget_tokens": 1024},
                "medium": {"type": "enabled", "budget_tokens": 4096},
                "high": {"type": "enabled", "budget_tokens": 16384},
                "xhigh": {"type": "enabled", "budget_tokens": 24576},
                "max": {"type": "enabled", "budget_tokens": 32768},
            },
            note="Anthropic extended thinking; the provider caps the budget",
        ),
        # Google's documented thinking-budget parameter.
        EffortKnob(
            family="google",
            parameter="thinking_budget",
            values={
                "low": 1024,
                "medium": 4096,
                "high": 16384,
                "xhigh": 24576,
                "max": 32768,
            },
            note="Gemini thinking budget in tokens",
        ),
    )
}

_EFFORT_KNOBS: Dict[str, EffortKnob] = dict(_BUILTIN_EFFORT_KNOBS)
_EFFORT_KNOBS_LOCK = threading.RLock()

#: Model-name fragments that select a family when the provider name is absent
#: or is a generic OpenAI-compatible gateway. Deliberately NARROW: a family is
#: claimed only by a name that actually exposes the knob, because attaching
#: ``reasoning_effort`` to a model that rejects it turns a cost knob into a
#: 400. ``gpt-4o`` therefore claims nothing while ``gpt-5`` claims ``openai``.
_MODEL_FAMILY_HINTS: Tuple[Tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("gpt-5", "openai"),
    ("gpt5", "openai"),
    ("o1-", "openai"),
    ("o1", "openai"),
    ("o3-", "openai"),
    ("o3", "openai"),
    ("o4-", "openai"),
    ("o4", "openai"),
    ("gemini", "google"),
    ("thinking", "google"),
)

#: Generic provider names that mean "an OpenAI-compatible endpoint". The
#: effort knob follows the MODEL's family, because a gateway can front any
#: of them; a model with no hint is ``unsupported_model``.
_GENERIC_PROVIDERS = frozenset({"openai", "openai-compatible", "custom", ""})


@dataclass(frozen=True)
class EffortPlan:
    """The honest answer to "what was actually sent for this effort level"."""

    requested: str
    status: str
    model: str = ""
    provider: str = ""
    family: str = ""
    parameter: str = ""
    value: Any = None
    detail: str = ""

    @property
    def sent(self) -> bool:
        """Whether a real provider parameter is on the request."""
        return self.status == EFFORT_SENT

    @property
    def parameters(self) -> Dict[str, Any]:
        """The kwargs to merge. Empty for every status except ``sent``."""
        if not self.sent or not self.parameter:
            return {}
        return {self.parameter: self.value}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "effort": self.requested,
            "effort_status": self.status,
            "effort_sent": self.sent,
            "effort_model": self.model,
            "effort_provider": self.provider,
            "effort_family": self.family,
            "effort_parameter": self.parameter,
            "effort_value": self.value,
            "effort_detail": self.detail,
        }


def normalize_effort(value: Any) -> str:
    """Return the canonical ladder rung for ``value``, or ``""`` if invalid.

    ``""`` (rather than ``auto``) is returned for an unrecognised value so a
    caller can REPORT it instead of quietly downgrading a typo to the
    default. Use :func:`resolve_effort` when a total answer is wanted.
    """
    if value is None:
        return EFFORT_AUTO
    text = str(value).strip().lower().replace("_", "-")
    if text in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[text]
    if text in EFFORT_LEVELS:
        return text
    return ""


def effort_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """Return the level named by :data:`EFFORT_ENV_VAR`, or ``""``.

    The RAW value is returned when it is set but unrecognised, so a caller can
    report the typo. Returning ``""`` for both "unset" and "set to nonsense"
    would let a bad environment variable look like no opinion at all.
    """
    import os

    source = os.environ if env is None else env
    raw = source.get(EFFORT_ENV_VAR)
    if raw is None or not str(raw).strip():
        return ""
    return normalize_effort(raw) or str(raw).strip().lower()


def resolve_effort(
    config: Optional[Mapping[str, Any]] = None,
    *,
    override: Any = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[str, str]:
    """Resolve the effective effort as ``(level, source)``.

    Precedence is call-level ``override`` > ``config["effort"]`` >
    ``NEO_EFFORT`` > ``auto``. The returned ``source`` is one of
    ``override``/``config``/``env``/``default`` so a receipt can say where a
    level came from. An unrecognised value resolves to ``auto`` AND the
    source becomes ``invalid:<value>``, so the caller can report it rather than
    letting a typo read as a deliberate default.
    """
    for value, source in (
        (override, "override"),
        ((config or {}).get("effort"), "config"),
        (effort_from_env(env) or None, "env"),
    ):
        if value is None or value == "":
            continue
        level = normalize_effort(value)
        if level:
            return level, source
        return EFFORT_AUTO, f"invalid:{str(value)[:32]}"
    return EFFORT_AUTO, "default"


def register_effort_knob(knob: EffortKnob) -> EffortKnob:
    """Declare (or replace) a provider family's effort knob.

    A malformed declaration RAISES, the same rule as :func:`register_capability`:
    a knob that cannot be understood must not become a silent default.
    """
    if not isinstance(knob, EffortKnob):
        raise CapabilityError("register_effort_knob needs an EffortKnob")
    with _EFFORT_KNOBS_LOCK:
        _EFFORT_KNOBS[knob.family] = knob
    return knob


def unregister_effort_knob(family: str) -> bool:
    """Remove a declared knob; returns whether one was removed.

    A built-in can be removed too (a deployment may front an endpoint that
    rejects the parameter outright); :func:`reset_effort_knobs` puts the
    built-in table back.
    """
    name = str(family or "").strip().lower()
    with _EFFORT_KNOBS_LOCK:
        return _EFFORT_KNOBS.pop(name, None) is not None


def reset_effort_knobs() -> None:
    """Forget every registered knob and restore the built-in table."""
    with _EFFORT_KNOBS_LOCK:
        _EFFORT_KNOBS.clear()
        _EFFORT_KNOBS.update(_BUILTIN_EFFORT_KNOBS)


def known_effort_knobs() -> List[EffortKnob]:
    """Return every declared knob, built-in and registered, sorted by family."""
    with _EFFORT_KNOBS_LOCK:
        return [_EFFORT_KNOBS[name] for name in sorted(_EFFORT_KNOBS)]


def effort_family_for(model: Optional[str], provider: Optional[str] = None) -> str:
    """Resolve the provider FAMILY whose effort knob applies.

    The MODEL decides, not the provider name: an ``openai``-compatible
    gateway fronts Anthropic and Google models too, and sending
    ``reasoning_effort`` to a gateway serving a Claude model is a 400. An
    unrecognised model is ``""`` and therefore reported
    ``unsupported_model`` rather than guessed.
    """
    name = str(model or "").strip().lower()
    # A provider-qualified id ("anthropic/claude-...") is the strongest hint.
    if "/" in name:
        head = name.split("/", 1)[0]
        with _EFFORT_KNOBS_LOCK:
            if head in _EFFORT_KNOBS:
                return head
    for fragment, family in _MODEL_FAMILY_HINTS:
        if fragment in name:
            with _EFFORT_KNOBS_LOCK:
                if family in _EFFORT_KNOBS:
                    return family
    declared = str(provider or "").strip().lower()
    if declared in _GENERIC_PROVIDERS:
        return ""
    with _EFFORT_KNOBS_LOCK:
        return declared if declared in _EFFORT_KNOBS else ""


def map_effort(
    level: Any,
    model: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    parameter: Any = None,
) -> EffortPlan:
    """Map an effort level onto the provider's real parameter.

    ``parameter`` is the caller's override of the knob choice: a string names
    the parameter to send, and the falsy values ``None``/``""``/``"none"``/
    ``False`` mean "send nothing at all" (``disabled``). An unrecognised
    model with no override is ``unsupported_model``; an unrecognised
    ``parameter`` is ``disabled`` with the reason, because sending a
    parameter nobody asked for is the failure this whole section is about.

    Never raises: a bad level is a reported ``invalid`` plan, not an
    exception in a run's model path.
    """
    model_name = str(model or "")
    provider_name = str(provider or "")
    raw = "" if level is None else str(level).strip().lower()
    canonical = normalize_effort(level)

    if not canonical:
        return EffortPlan(
            requested=raw or "(none)",
            status=EFFORT_INVALID,
            model=model_name,
            provider=provider_name,
            detail=(
                f"{raw or 'value'!r} is not an effort level; expected one of "
                f"{', '.join(EFFORT_CHOICES)}"
            ),
        )

    if canonical == EFFORT_AUTO:
        return EffortPlan(
            requested=EFFORT_AUTO,
            status=EFFORT_AUTO,
            model=model_name,
            provider=provider_name,
            detail="no level requested; the provider's own default is used",
        )

    if parameter is not None and str(parameter).strip().lower() in {
        "none",
        "off",
        "false",
        "0",
        "no",
    }:
        return EffortPlan(
            requested=canonical,
            status=EFFORT_DISABLED,
            model=model_name,
            provider=provider_name,
            detail="effort_parameter pinned off: no effort parameter is sent",
        )

    family = effort_family_for(model_name, provider_name)
    knob: Optional[EffortKnob] = None
    with _EFFORT_KNOBS_LOCK:
        if parameter:
            wanted = str(parameter).strip()
            knob = next(
                (item for item in _EFFORT_KNOBS.values() if item.parameter == wanted),
                None,
            )
            if knob is None:
                return EffortPlan(
                    requested=canonical,
                    status=EFFORT_DISABLED,
                    model=model_name,
                    provider=provider_name,
                    parameter=wanted,
                    detail=(
                        f"no declared effort knob sends {wanted!r}; nothing is sent"
                    ),
                )
            family = knob.family
        else:
            knob = _EFFORT_KNOBS.get(family)

    if knob is None:
        return EffortPlan(
            requested=canonical,
            status=EFFORT_UNSUPPORTED_MODEL,
            model=model_name,
            provider=provider_name,
            detail=(
                f"{model_name or provider_name or 'this target'} has no declared "
                "effort knob; the request is sent unchanged"
            ),
        )

    if canonical not in knob.values:
        return EffortPlan(
            requested=canonical,
            status=EFFORT_UNSUPPORTED_LEVEL,
            model=model_name,
            provider=provider_name,
            family=knob.family,
            parameter=knob.parameter,
            detail=(
                f"{knob.family} sends {knob.parameter} for "
                f"{'/'.join(knob.levels)} only; {canonical!r} is not one of them, "
                "so nothing is sent rather than clamped"
            ),
        )

    return EffortPlan(
        requested=canonical,
        status=EFFORT_SENT,
        model=model_name,
        provider=provider_name,
        family=knob.family,
        parameter=knob.parameter,
        value=knob.values[canonical],
        detail=knob.note,
    )


def synthetic_effort_plan(
    level: Any, model: Optional[str] = None, provider: Optional[str] = None
) -> EffortPlan:
    """Return the honest plan for a harness-served (mock/scripted) provider.

    A scripted provider serves a canned response and ignores request
    parameters, so ``sent`` would be a lie and ``unsupported_model`` would
    overstate the gap. The status says exactly what happened.
    """
    canonical = normalize_effort(level)
    return EffortPlan(
        requested=canonical or str(level or "").strip().lower(),
        status=EFFORT_AUTO if canonical == EFFORT_AUTO else EFFORT_SYNTHETIC,
        model=str(model or ""),
        provider=str(provider or ""),
        detail=(
            "harness-served provider: the response is scripted and request "
            "parameters are not read"
        ),
    )


# ---------------------------------------------------------------------------
# The effort VARIANT (product round, terminal 03) - surfacing the ladder
# ---------------------------------------------------------------------------
#
# The ladder above is the WIRE vocabulary: what this harness can put on a
# request. A picker is not that. A picker is the PRODUCT vocabulary: the set
# of rungs a person may CHOOSE for a given provider, which is a different
# question and is answered here rather than by widening the knobs.
#
# The two are kept apart on purpose, because merging them is how a receipt
# starts implying a parameter was sent when it was not:
#
#   * ``openai`` offers ``none|minimal|low|medium|high|xhigh`` as CHOICES,
#     while its declared ``reasoning_effort`` knob carries ``low|medium|high``.
#     Picking ``xhigh`` is a real user action whose honest answer is
#     ``unsupported_level`` -- not a clamp to ``high``, and not a silent
#     no-op.
#   * ``anthropic`` offers ``high|max``; its ``thinking`` knob can express all
#     five wire rungs, so a carried ``low`` is reported as ``mapped`` with the
#     budget it really sends rather than being discarded.
#   * ``google`` offers ``low|high``.
#
# Nothing here re-implements the ladder: every plan is produced by
# :func:`map_effort`, and the only thing this section adds is the CHOICE
# vocabulary, the one rung that means "send nothing", and the receipts.

#: The product-facing CHOICE vocabulary per provider family, in pick order.
#: ``variant.cycle`` walks exactly this tuple and wraps at the end.
EFFORT_PICKER_FAMILIES: Dict[str, Tuple[str, ...]] = {
    "anthropic": ("high", "max"),
    "openai": ("none", "minimal", "low", "medium", "high", "xhigh"),
    "google": ("low", "high"),
}

#: The rung that means "no effort parameter at all". It is a CHOICE, not a
#: wire level, which is why it is not in :data:`EFFORT_LEVELS`.
EFFORT_PICKER_NONE = "none"

#: A model whose family claims no knob offers exactly one choice. Offering the
#: whole ladder there would be a list of rungs that every one of which reports
#: ``unsupported_model``: clutter that teaches the user nothing.
EFFORT_PICKER_UNSUPPORTED: Tuple[str, ...] = (EFFORT_AUTO,)

#: Closed vocabulary for how a carried level survived a model change.
EFFORT_VARIANT_EXACT = "exact"  # already in the new model's choice vocabulary
EFFORT_VARIANT_MAPPED = "mapped"  # not a choice here, but a real parameter
EFFORT_VARIANT_DROPPED = "dropped"  # cannot be honoured; fell back, and said so

#: The keybind a surface binds to cycle the variant. Declared HERE, beside the
#: vocabulary it cycles, so a surface cannot bind a key that walks a different
#: list than the one the receipt reports.
VARIANT_KEYBIND = "variant.cycle"


def variant_keybind() -> str:
    """Return the keybind a surface binds to cycle the effort variant.

    A function rather than a bare constant so every surface reads the SAME
    value; ``cli.models`` re-exports it and the picker test pins the equality.
    """
    return VARIANT_KEYBIND


def _knob_for(family: str, parameter: Any = None) -> Optional[EffortKnob]:
    """Return the knob a plan would use, or ``None`` when there is none."""
    with _EFFORT_KNOBS_LOCK:
        if parameter:
            wanted = str(parameter).strip()
            return next(
                (item for item in _EFFORT_KNOBS.values() if item.parameter == wanted),
                None,
            )
        return _EFFORT_KNOBS.get(family)


def picker_vocabulary(
    model: Optional[str] = None, provider: Optional[str] = None
) -> Tuple[str, ...]:
    """Return the levels a person may CHOOSE for this target, in pick order.

    The MODEL decides the family (see :func:`effort_family_for`), for the same
    reason the knob does: an ``openai``-compatible gateway fronts Claude and
    Gemini too, and offering a Claude user the OpenAI rungs would be offering
    levels that cannot be sent.

    A target whose family declares no knob returns
    :data:`EFFORT_PICKER_UNSUPPORTED` - one honest choice instead of five
    rungs that all report ``unsupported_model``. Never raises, never returns
    an empty tuple, so a caller can always index ``[0]``.
    """
    family = effort_family_for(model, provider)
    if not family:
        return EFFORT_PICKER_UNSUPPORTED
    with _EFFORT_KNOBS_LOCK:
        known = family in _EFFORT_KNOBS
    if not known:
        return EFFORT_PICKER_UNSUPPORTED
    return tuple(EFFORT_PICKER_FAMILIES.get(family, (EFFORT_AUTO,)))


def normalize_picker_level(value: Any) -> str:
    """Return the canonical CHOICE rung for ``value``, or ``""`` when unknown.

    Accepts the wire aliases too (``hi`` -> ``high``) so one typed value cannot
    resolve in one place and be ignored in another. ``none`` is accepted as
    itself; ``auto``/``default`` resolve to :data:`EFFORT_PICKER_NONE`, because
    "the provider's own default" and "send no effort parameter" are the same
    user intent under two names.
    """
    if value is None:
        return ""
    text = str(value).strip().lower().replace("_", "-")
    if text in (EFFORT_AUTO, "default", "off"):
        return EFFORT_PICKER_NONE
    if text == EFFORT_PICKER_NONE:
        return EFFORT_PICKER_NONE
    wire = normalize_effort(text)
    if wire:
        return wire
    # A rung that is a real CHOICE for some family but not a wire level at all
    # (``minimal`` today). It is accepted here so the refusal happens against
    # the family's declared parameter - see :func:`map_picker_effort` - rather
    # than as ``invalid``, which would blame the user for picking from a list
    # the product itself published.
    for vocabulary in EFFORT_PICKER_FAMILIES.values():
        if text in vocabulary:
            return text
    return ""


def map_picker_effort(
    level: Any,
    model: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    parameter: Any = None,
) -> EffortPlan:
    """Map a CHOICE rung onto the provider's real parameter, or say why not.

    Thin over :func:`map_effort`; it exists for exactly two rungs the wire
    ladder does not have:

    * ``none`` -> ``auto``, whose plan is the honest "nothing was requested and
      nothing was sent" row rather than an invented parameter;
    * a rung that is a real choice for a family but not a wire level at all
      (``minimal`` for OpenAI today). That is ``unsupported_level`` with a
      sentence naming the parameter and the levels that family does accept -
      NOT ``invalid``, which would blame the user for picking from a list the
      product itself published, and NOT a clamp.

    Never raises.
    """
    rung = normalize_picker_level(level)
    if rung == EFFORT_PICKER_NONE:
        return map_effort(EFFORT_AUTO, model, provider=provider, parameter=parameter)
    if not rung:
        # Not a rung anybody published. Delegating gives the honest `invalid`
        # row with the value echoed, which is the whole point of not rounding a
        # typo to a level.
        return map_effort(level, model, provider=provider, parameter=parameter)
    if rung in EFFORT_LEVELS:
        return map_effort(rung, model, provider=provider, parameter=parameter)
    # A choice the wire ladder does not carry. Report it against the family's
    # own declared parameter so the reason is actionable.
    family = effort_family_for(model, provider)
    knob = _knob_for(family, parameter)
    model_name = str(model or "")
    provider_name = str(provider or "")
    if knob is None:
        return EffortPlan(
            requested=rung,
            status=EFFORT_UNSUPPORTED_MODEL,
            model=model_name,
            provider=provider_name,
            family=family,
            detail=(
                f"{model_name or provider_name or 'this target'} has no declared "
                f"effort knob; {rung!r} is not sent and the request is unchanged"
            ),
        )
    return EffortPlan(
        requested=rung,
        status=EFFORT_UNSUPPORTED_LEVEL,
        model=model_name,
        provider=provider_name,
        family=knob.family,
        parameter=knob.parameter,
        detail=(
            f"{knob.family} sends {knob.parameter} for "
            f"{'/'.join(knob.levels)} only; {rung!r} is not one of them, "
            "so nothing is sent rather than clamped"
        ),
    )


def next_picker_level(
    model: Optional[str] = None,
    provider: Optional[str] = None,
    current: Any = None,
    *,
    parameter: Any = None,
) -> str:
    """Return the rung ``variant.cycle`` moves to from ``current``.

    Walks :func:`picker_vocabulary` and WRAPS. A current level that is not in
    the vocabulary (a rung carried from another family, or an unknown value)
    starts the walk at the first choice rather than being rounded to something
    - the cycle is a control, and a control that silently rewrites the setting
    it is showing is the bug this whole section exists to prevent. A vocabulary
    of one rung returns that rung, so a model with no knob is stable rather
    than surprising. Never raises and never returns ``""``.
    """
    vocabulary = picker_vocabulary(model, provider)
    if len(vocabulary) <= 1:
        return vocabulary[0] if vocabulary else EFFORT_AUTO
    rung = normalize_picker_level(current)
    try:
        index = vocabulary.index(rung)
    except ValueError:
        return vocabulary[0]
    return vocabulary[(index + 1) % len(vocabulary)]


def _in_vocabulary(rung: str, vocabulary: Tuple[str, ...]) -> bool:
    """Whether a rung is one of the choices, with ``none`` and ``auto`` equal.

    ``none`` and ``auto`` are the same user intent under two names - "the
    provider's own default" - and they are spelled differently in different
    families' vocabularies. Collapsing them here is what stops a knob-LESS
    model from reporting a carried ``auto`` as a level that was ``dropped``.
    """
    if rung in vocabulary:
        return True
    if rung in (EFFORT_PICKER_NONE, EFFORT_AUTO):
        return EFFORT_AUTO in vocabulary or EFFORT_PICKER_NONE in vocabulary
    return False


def carry_picker_effort(
    previous: Any,
    model: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    parameter: Any = None,
) -> Dict[str, Any]:
    """Carry an effort level across a MODEL change, and report what happened.

    This is the "chained immediately after model selection" rule, and it has
    exactly three honest answers (:data:`EFFORT_VARIANT_EXACT`,
    :data:`EFFORT_VARIANT_MAPPED`, :data:`EFFORT_VARIANT_DROPPED`):

    * ``exact`` - the level is in the new model's choice vocabulary.
    * ``mapped`` - it is not a choice here, but the family WILL send a real
      parameter for it, so it is kept and the receipt names the parameter and
      the value. Dropping a level the provider would have honoured would be
      the same silent-downgrade the ladder forbids.
    * ``dropped`` - the family cannot honour it at all. The first rung of the
      new vocabulary takes over and the receipt NAMES the level that was
      dropped, so the change is stated rather than performed.

    The returned dict always carries ``plan`` (the honest
    :class:`EffortPlan` for the level now in force) so a caller never has to
    re-derive what would be sent. Never raises.
    """
    vocabulary = picker_vocabulary(model, provider)
    rung = normalize_picker_level(previous)
    detail = ""
    if not rung:
        level = vocabulary[0] if vocabulary else EFFORT_AUTO
        carried = EFFORT_AUTO
    elif _in_vocabulary(rung, vocabulary):
        level = EFFORT_AUTO if vocabulary == EFFORT_PICKER_UNSUPPORTED else rung
        carried = EFFORT_VARIANT_EXACT
    else:
        plan = map_picker_effort(rung, model, provider=provider, parameter=parameter)
        if plan.sent:
            level = rung
            carried = EFFORT_VARIANT_MAPPED
            detail = (
                f"{rung!r} is not one of this model's choices "
                f"({'/'.join(vocabulary)}), but {plan.family} will send "
                f"{plan.parameter}={plan.value!r} for it"
            )
        else:
            level = vocabulary[0] if vocabulary else EFFORT_AUTO
            carried = EFFORT_VARIANT_DROPPED
            detail = plan.detail
    plan = map_picker_effort(level, model, provider=provider, parameter=parameter)
    return {
        "previous": rung,
        "level": level,
        "effective_effort": plan.requested if plan.sent else EFFORT_AUTO,
        "carried": carried,
        "changed": level != rung,
        "vocabulary": list(vocabulary),
        "sent": plan.sent,
        "parameter": plan.parameter,
        "value": plan.value,
        "status": plan.status,
        "family": plan.family,
        "detail": detail or plan.detail,
        "plan": plan.to_dict(),
    }


def picker_variant(
    level: Any,
    model: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    parameter: Any = None,
) -> Dict[str, Any]:
    """The receipt a surface renders for one effort choice.

    ``honoured`` is the field a UI must be careful with: it is True ONLY when
    a real provider parameter is on the request. ``level`` is what the user
    picked. A surface that renders ``level`` where it means ``honoured`` is
    exactly the "set to high that silently does nothing" bug, so the two are
    separate keys with separate meanings and both are always present.

    Never raises; an unusable ``level`` is reported as an ``invalid`` plan
    rather than raising inside a surface's render path.
    """
    vocabulary = picker_vocabulary(model, provider)
    rung = normalize_picker_level(level)
    # The PLAN is resolved from what was actually asked for, so a model with no
    # declared knob still reports WHY (unsupported_model) rather than being
    # quietly rewritten to a request that would have been fine.
    plan = map_picker_effort(
        rung or level, model, provider=provider, parameter=parameter
    )
    if vocabulary == EFFORT_PICKER_UNSUPPORTED:
        # A knob-less model has exactly one choice and its own vocabulary spells
        # it `auto`; reporting it as `none` would put the level outside the list
        # the receipt publishes, and `none` is a DIFFERENT family's word for a
        # family that does have a knob. Only the LABEL moves; the status above
        # is the honest answer for the request that was made.
        rung = EFFORT_AUTO
    return {
        "level": rung or str(level or "").strip().lower(),
        "effective_effort": plan.requested if plan.sent else EFFORT_AUTO,
        "in_vocabulary": _in_vocabulary(rung, vocabulary),
        "vocabulary": list(vocabulary),
        "honoured": plan.sent,
        "parameter": plan.parameter,
        "value": plan.value,
        "status": plan.status,
        "family": plan.family,
        "detail": plan.detail,
        "keybind": VARIANT_KEYBIND,
        "plan": plan.to_dict(),
    }
