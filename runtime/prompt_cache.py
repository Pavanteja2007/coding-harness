"""Prefix-stable prompt caching: digests, breakpoints, and cache receipts.

This module is the single owner of *where a cache breakpoint sits* and *what
counts as a cache hit*. It is pure: it computes digests, decides the provider
parameters, and folds a provider usage payload into a receipt. It never calls a
provider and never writes to disk.

Why it exists
-------------
Provider prompt caching only pays when the request begins with a BYTE-IDENTICAL
prefix. Three things used to break that in this codebase:

1. the step system prompt interpolated the issue, plan, completed steps, and
   retrieved files into the *system* message, so the first message changed on
   every turn (``harness.prompts`` now splits it at a stable marker);
2. tool schemas were not part of any digest, so a catalog change was invisible
   to the cache accounting and to the receipts;
3. nothing recorded what the provider actually did, so a "cache hit" claim
   could not be distinguished from a provider that ignores caching.

Vocabulary (kept exact, because the receipts and the CLI render it):

``frozen prefix``
    The leading run of ``system`` messages (or the first message when a caller
    has none) plus the normalized tool schemas. Everything after it is
    volatile: issue text, retrieved context, prior-attempt feedback.

``breakpoint``
    The message index at which the frozen prefix ends. Provider cache
    parameters are attached at that index.

``cache status``
    ``hit`` | ``partial`` | ``creation`` | ``miss`` | ``unsupported`` |
    ``unreported``. ``unreported`` is the honest answer when the provider
    returns no cache fields at all: it is never rounded to ``miss``.

``invalidations``
    A call whose prefix digest differs from the previous call's digest for the
    same (model, endpoint). A tool-schema change therefore produces EXACTLY ONE
    invalidation: the next call re-creates the cache entry and every later call
    on the new prefix is a hit again.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CACHE_CREATION",
    "CACHE_HIT",
    "CACHE_MISS",
    "CACHE_PARTIAL",
    "CACHE_STATUSES",
    "CACHE_UNREPORTED",
    "CACHE_UNSUPPORTED",
    "CachePlan",
    "CacheReceipt",
    "PromptCacheLedger",
    "apply_cache_parameters",
    "cache_control_kwargs",
    "current_cache_ledger",
    "estimate_tokens",
    "frozen_prefix",
    "normalize_tool_schemas",
    "plan_cache",
    "prefix_digest",
    "receipt_from_usage",
    "reset_cache_context",
    "set_cache_ledger",
    "split_at_breakpoint",
    "tool_schema_digest",
]

CACHE_HIT = "hit"
CACHE_PARTIAL = "partial"
CACHE_CREATION = "creation"
CACHE_MISS = "miss"
CACHE_UNSUPPORTED = "unsupported"
CACHE_UNREPORTED = "unreported"

#: Statuses that mean "the provider answered a cache question". ``unreported``
#: is deliberately excluded: it proves nothing either way, so counting it as a
#: miss would overstate savings and counting it as a hit would overstate
#: efficiency.
CACHE_STATUSES = (
    CACHE_HIT,
    CACHE_PARTIAL,
    CACHE_CREATION,
    CACHE_MISS,
    CACHE_UNSUPPORTED,
    CACHE_UNREPORTED,
)
_DECIDED_STATUSES = frozenset({CACHE_HIT, CACHE_PARTIAL, CACHE_CREATION, CACHE_MISS})
_HIT_STATUSES = frozenset({CACHE_HIT, CACHE_PARTIAL})

#: Providers that accept an explicit ``cache_control`` breakpoint. Everything
#: else caches the stable prefix implicitly and must NOT be sent the parameter
#: (an unknown key is a 400 on most OpenAI-compatible gateways).
ANTHROPIC_PROVIDERS = frozenset({"anthropic", "claude", "bedrock", "vertex_ai"})

#: Rough characters-per-token ratio used only to decide whether a prefix is
#: big enough to be worth a cache write. The real accounting always comes from
#: the provider's reported token counts.
CHARS_PER_TOKEN = 4

#: Default minimum frozen-prefix size before a cache breakpoint is requested.
#: Providers ignore breakpoints below their own floor; requesting one on a tiny
#: prefix only adds a parameter.
DEFAULT_MIN_PREFIX_TOKENS = 1024

#: Reported cache-read price as a fraction of the reported cache-write price.
#: Anthropic's published ratio is 0.1x. This is only used when a provider
#: reports cached tokens but no cost, and the resulting ledger row says so
#: (``cost_source`` carries ``cache_discount``).
DEFAULT_CACHE_READ_DISCOUNT = 0.1


def estimate_tokens(text: Any) -> int:
    """Return a rough token estimate for text.

    Only used for pre-flight decisions ("is this prefix big enough to cache?")
    and for labelling a partial hit. Never reported as a measured count.
    """
    if text is None:
        return 0
    body = (
        text if isinstance(text, str) else json.dumps(text, sort_keys=True, default=str)
    )
    return max(1, (len(body) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN) if body else 0


def _message_text(message: Any) -> str:
    """Return the plain text of a chat message, whatever content shape it has."""
    if not isinstance(message, Mapping):
        return "" if message is None else str(message)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: List[str] = []
        for block in content:
            if isinstance(block, Mapping):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def normalize_tool_schemas(tools: Optional[Iterable[Any]]) -> List[Any]:
    """Return tool schemas in a canonical, order-stable form.

    Provider payloads carry key order and extra provider fields that do not
    change the cache prefix semantics but would change a naive digest. Only the
    fields a provider actually keys the prefix on are kept: the schema's
    identity (name/function name), description, parameter names, required
    names, and type. Sorting makes the digest independent of the catalog's
    declaration order so a reordered (but equivalent) catalog is not reported
    as an invalidation.
    """
    normalized: List[Any] = []
    for tool in tools or []:
        if not isinstance(tool, Mapping):
            normalized.append({"name": str(tool)})
            continue
        function = tool.get("function")
        function = function if isinstance(function, Mapping) else tool
        name = str(function.get("name") or tool.get("name") or tool.get("type", ""))
        description = str(function.get("description") or tool.get("description") or "")
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping):
            parameters = tool.get("parameters")
        properties: Dict[str, Any] = {}
        required: List[str] = []
        if isinstance(parameters, Mapping):
            raw_properties = parameters.get("properties")
            if isinstance(raw_properties, Mapping):
                for key, value in raw_properties.items():
                    if isinstance(value, Mapping):
                        properties[str(key)] = {
                            "type": str(value.get("type", "")),
                            "description": str(value.get("description") or ""),
                            "enum": sorted(
                                str(item) for item in value.get("enum", []) or []
                            )
                            if isinstance(value.get("enum"), (list, tuple, set))
                            else [],
                        }
                    else:
                        properties[str(key)] = {"type": str(value)}
            raw_required = parameters.get("required")
            if isinstance(raw_required, (list, tuple, set)):
                required = sorted(str(item) for item in raw_required)
        normalized.append(
            {
                "name": name,
                "description": description,
                "parameters": properties,
                "required": required,
            }
        )
    normalized.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return normalized


def tool_schema_digest(tools: Optional[Iterable[Any]]) -> str:
    """Return a stable digest of the effective tool schemas ("" when absent)."""
    normalized = normalize_tool_schemas(tools)
    if not normalized:
        return ""
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def frozen_prefix(
    messages: Optional[Sequence[Any]],
    breakpoint_index: Optional[int] = None,
) -> List[Any]:
    """Return the leading messages that form the cacheable prefix.

    The default rule is **the first message when it is a system message**,
    falling back to the first message of any kind. It deliberately stops at
    ONE system message rather than consuming the whole leading system run:
    over-including a volatile system message silently destroys every future
    cache hit (and looks perfectly healthy in the receipts until the provider
    stops hitting), while under-including costs only the few tokens of a
    second static system block. A caller that knows its prefix is longer can
    say so with ``breakpoint_index``.

    ``harness.prompts.render_step_messages`` produces exactly the shape this
    rule is built for: ``[system(frozen), system(per-turn), user]``.
    """
    items = [item for item in (messages or [])]
    if not items:
        return []
    if breakpoint_index is not None:
        try:
            index = int(breakpoint_index)
        except (TypeError, ValueError):
            index = -1
        if index >= 0:
            return items[: index + 1]
    first = items[0]
    role = first.get("role") if isinstance(first, Mapping) else None
    if role in ("system", "developer"):
        return items[:1]
    for message in items:
        inner = message.get("role") if isinstance(message, Mapping) else None
        if inner in ("system", "developer"):
            return items[:1]
    return items[:1]


def split_at_breakpoint(
    messages: Optional[Sequence[Any]],
    tools: Optional[Iterable[Any]] = None,
    breakpoint_index: Optional[int] = None,
) -> Tuple[List[Any], List[Any], int]:
    """Return ``(prefix, suffix, breakpoint_index)`` for a request.

    The prefix is the cacheable leading run; the suffix carries every volatile
    message (issue text, retrieved context, feedback). The index is the position
    of the LAST prefix message, so a provider parameter attached there covers
    the whole prefix.
    """
    items = [item for item in (messages or [])]
    prefix = frozen_prefix(items, breakpoint_index)
    index = len(prefix) - 1
    return prefix, items[len(prefix) :], index


def prefix_digest(
    messages: Optional[Sequence[Any]],
    tools: Optional[Iterable[Any]] = None,
    breakpoint_index: Optional[int] = None,
) -> str:
    """Return the digest of the cacheable prefix INCLUDING the tool schemas.

    Tool schemas are part of the digest on purpose: a catalog change is a real
    prefix change, and the receipts must show it as exactly one invalidation
    rather than silently reusing a stale entry.
    """
    prefix, _suffix, _index = split_at_breakpoint(messages, tools, breakpoint_index)
    payload = {
        "messages": [
            {
                "role": item.get("role", "system")
                if isinstance(item, Mapping)
                else "system",
                "content": _message_text(item),
            }
            for item in prefix
        ],
        "tools": normalize_tool_schemas(tools),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()


def _provider_family(provider: Optional[str], model: Optional[str]) -> str:
    """Return a coarse provider family used to pick cache parameters."""
    text = f"{provider or ''} {model or ''}".lower()
    if any(family in text for family in ANTHROPIC_PROVIDERS):
        return "anthropic"
    if "gemini" in text or "vertex" in text:
        return "google"
    if "bedrock" in text:
        return "aws"
    return "openai"


@dataclass(frozen=True)
class CachePlan:
    """One request's cache intent, computed before the provider is called."""

    #: SHA-256 over the frozen prefix plus the normalized tool schemas.
    prefix_sha256: str
    #: Digest of the tool schemas alone ("" when no tools were sent).
    tool_schema_sha256: str
    #: Index of the last frozen-prefix message (-1 when there is no prefix).
    breakpoint_index: int
    #: Estimated frozen-prefix tokens (a pre-flight estimate, not a measurement).
    prefix_tokens_estimate: int
    #: Coarse provider family: ``anthropic`` | ``google`` | ``aws`` | ``openai``.
    provider_family: str
    #: True when a cache parameter will actually be sent.
    requested: bool
    #: Why no parameter was sent (empty string when one was).
    skip_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe description of this plan."""
        return {
            "prefix_sha256": self.prefix_sha256,
            "tool_schema_sha256": self.tool_schema_sha256,
            "cache_breakpoint_index": self.breakpoint_index,
            "prefix_tokens_estimate": self.prefix_tokens_estimate,
            "cache_provider_family": self.provider_family,
            "cache_requested": self.requested,
            "cache_skip_reason": self.skip_reason,
        }


@dataclass(frozen=True)
class CacheReceipt:
    """What the provider actually reported about caching for one call."""

    status: str = CACHE_UNREPORTED
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_write_tokens: int = 0
    prefix_sha256: str = ""
    tool_schema_sha256: str = ""
    breakpoint_index: int = -1
    prefix_tokens_estimate: int = 0
    reported: bool = False

    @property
    def hit(self) -> bool:
        """Return whether the provider served the prefix from cache."""
        return self.status in _HIT_STATUSES

    @property
    def decided(self) -> bool:
        """Return whether this receipt carries a usable cache answer."""
        return self.status in _DECIDED_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe receipt for the ledger and the trace."""
        return {
            "cache_status": self.status,
            "cache_hit": self.hit,
            "cached_input_tokens": int(self.cached_input_tokens),
            "cache_creation_input_tokens": int(self.cache_creation_tokens),
            "cache_write_tokens": int(self.cache_write_tokens),
            "cache_prefix_sha256": self.prefix_sha256,
            "cache_tool_schema_sha256": self.tool_schema_sha256,
            "cache_breakpoint_index": int(self.breakpoint_index),
            "cache_prefix_tokens_estimate": int(self.prefix_tokens_estimate),
            "cache_reported": bool(self.reported),
        }


def plan_cache(
    messages: Optional[Sequence[Any]],
    tools: Optional[Iterable[Any]] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    enabled: bool = True,
    min_prefix_tokens: int = DEFAULT_MIN_PREFIX_TOKENS,
    breakpoint_index: Optional[int] = None,
) -> CachePlan:
    """Decide whether to request a cache breakpoint for this request.

    ``enabled`` False (config ``prompt_cache``) is the OFF arm: the plan is
    still computed so the ledger can show what the prefix WOULD have been, but
    no provider parameter is sent. A prefix smaller than ``min_prefix_tokens``
    is not worth caching and is reported with an explicit ``skip_reason``
    instead of being silently requested.
    """
    prefix, _suffix, index = split_at_breakpoint(messages, tools, breakpoint_index)
    family = _provider_family(provider, model)
    digest = prefix_digest(messages, tools, breakpoint_index)
    schemas = tool_schema_digest(tools)
    estimate = sum(estimate_tokens(_message_text(item)) for item in prefix)
    if schemas:
        estimate += estimate_tokens(normalize_tool_schemas(tools))
    try:
        floor = max(0, int(min_prefix_tokens))
    except (TypeError, ValueError):
        floor = DEFAULT_MIN_PREFIX_TOKENS
    skip = ""
    if not enabled:
        skip = "disabled"
    elif not prefix:
        skip = "empty_prefix"
    elif family != "anthropic":
        # OpenAI-compatible and Google endpoints cache a stable prefix
        # implicitly; there is no parameter to send, and sending an unknown one
        # is a hard 400 on most gateways.
        skip = "implicit_prefix"
    elif estimate < floor:
        skip = "prefix_below_floor"
    return CachePlan(
        prefix_sha256=digest,
        tool_schema_sha256=schemas,
        breakpoint_index=index,
        prefix_tokens_estimate=estimate,
        provider_family=family,
        requested=skip == "",
        skip_reason=skip,
    )


def cache_control_kwargs(
    messages: Sequence[Any],
    plan: CachePlan,
) -> Dict[str, Any]:
    """Return a shallow copy of ``messages`` with the cache parameter attached.

    Only the LAST frozen-prefix message is annotated, so one parameter covers
    the whole prefix. Message mappings are copied rather than mutated: the
    caller's list (and any conversation the harness still holds) is untouched.
    """
    if not plan.requested or plan.breakpoint_index < 0:
        return list(messages)
    annotated: List[Any] = []
    for position, message in enumerate(messages):
        if position != plan.breakpoint_index or not isinstance(message, Mapping):
            annotated.append(message)
            continue
        updated = dict(message)
        updated["cache_control"] = {"type": "ephemeral"}
        annotated.append(updated)
    return annotated


def apply_cache_parameters(
    messages: Sequence[Any],
    tools: Optional[Iterable[Any]],
    plan: CachePlan,
) -> Tuple[List[Any], Optional[List[Any]]]:
    """Return ``(messages, tools)`` with provider cache parameters applied.

    The tool catalog is annotated as a second, independent cache breakpoint
    where the provider accepts one, because the tool schemas are the other half
    of a stable agent prefix. Both annotations are copies: no caller-owned
    object is modified.
    """
    out_messages = cache_control_kwargs(messages, plan)
    if not plan.requested or not tools:
        return out_messages, (list(tools) if tools else None)
    annotated_tools: List[Any] = []
    for position, tool in enumerate(tools):
        if position == len(tools) - 1 and isinstance(tool, Mapping):
            updated = dict(tool)
            updated["cache_control"] = {"type": "ephemeral"}
            annotated_tools.append(updated)
        else:
            annotated_tools.append(tool)
    return out_messages, annotated_tools


def _coerce_int(value: Any) -> int:
    """Return a non-negative int for provider-reported token counts."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _has_field(container: Any, key: str) -> bool:
    """Return whether a usage container CARRIES a key (even a zero value)."""
    if isinstance(container, Mapping):
        return key in container
    return hasattr(container, key)


def cache_tokens_from_usage(usage: Any) -> Tuple[int, int, int, bool]:
    """Return ``(cached_input, creation, write, reported)`` from a usage payload.

    Providers disagree on the field names, and this must not silently read zero
    from a shape it did not understand, so ``reported`` says whether ANY
    recognized cache field was present:

    * OpenAI: ``prompt_tokens_details.cached_tokens`` and
      ``prompt_tokens_details.cache_creation_input_tokens``;
    * Anthropic: ``cache_read_input_tokens`` and
      ``cache_creation_input_tokens``;
    * proxies/aggregators: flat ``cached_tokens`` / ``cache_read_tokens`` /
      ``cache_hit_tokens`` aliases.
    """
    if usage is None:
        return 0, 0, 0, False
    if not isinstance(usage, Mapping):
        usage = {
            key: getattr(usage, key, None)
            for key in (
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "cached_tokens",
                "cache_write_tokens",
                "prompt_tokens_details",
                "input_tokens_details",
            )
        }
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, Mapping):
        details = usage.get("input_tokens_details")

    def _from(container: Any, key: str) -> int:
        if isinstance(container, Mapping):
            return _coerce_int(container.get(key))
        return _coerce_int(getattr(container, key, 0))

    cached = 0
    creation = 0
    write = 0
    reported = False
    read_keys = (
        "cached_tokens",
        "cache_read_input_tokens",
        "cache_read_tokens",
        "cache_hit_tokens",
    )
    creation_keys = ("cache_creation_input_tokens", "cache_creation_tokens")
    write_keys = ("cache_write_tokens", "cache_creation_tokens")
    for container in (details, usage):
        if container is None:
            continue
        value = 0
        for key in read_keys:
            if not _has_field(container, key):
                continue
            # The field is PRESENT: the provider spoke the cache protocol, so
            # a zero here is a real "not cached", not silence. That distinction
            # is what keeps `miss` separate from `unreported`.
            reported = True
            value = max(value, _from(container, key))
        cached = max(cached, value)
        for key in creation_keys:
            if _has_field(container, key):
                reported = True
                creation = max(creation, _from(container, key))
        for key in write_keys:
            if _has_field(container, key):
                reported = True
                write = max(write, _from(container, key))
    return cached, creation, write, reported


def receipt_from_usage(usage: Any, plan: CachePlan) -> CacheReceipt:
    """Fold a provider usage payload into a :class:`CacheReceipt`.

    Status rules, in order:

    1. cached tokens > 0 and covering at least the estimated prefix -> ``hit``;
    2. cached tokens > 0 but short of the estimate -> ``partial``;
    3. creation tokens > 0 (or an explicit write) with no read -> ``creation``;
    4. the provider reported a cache vocabulary and reported zero reads on a
       request we asked to cache -> ``miss``;
    5. the provider reported no cache field at all -> ``unreported``;
    6. we never asked (no prefix, disabled, or an implicitly-caching family)
       -> ``unsupported``.

    ``miss`` is only claimed when the provider demonstrably speaks the cache
    protocol; otherwise the honest answer is ``unreported``.
    """
    cached, creation, write, reported = cache_tokens_from_usage(usage)
    if cached > 0:
        status = (
            CACHE_HIT
            if plan.prefix_tokens_estimate <= 0 or cached >= plan.prefix_tokens_estimate
            else CACHE_PARTIAL
        )
    elif creation > 0 or write > 0:
        status = CACHE_CREATION
    elif reported:
        status = CACHE_MISS
    elif plan.requested:
        status = CACHE_UNREPORTED
    else:
        status = CACHE_UNSUPPORTED
    return CacheReceipt(
        status=status,
        cached_input_tokens=cached,
        cache_creation_tokens=creation,
        cache_write_tokens=write,
        prefix_sha256=plan.prefix_sha256,
        tool_schema_sha256=plan.tool_schema_sha256,
        breakpoint_index=plan.breakpoint_index,
        prefix_tokens_estimate=plan.prefix_tokens_estimate,
        reported=reported,
    )


class PromptCacheLedger:
    """Per-execution-context accounting of prompt-cache behaviour.

    One ledger per router context (``set_cache_ledger``), so concurrent tasks
    never observe one another's counts. It answers three questions the CLI and
    the handoff need:

    * ``cache_hit_rate`` over calls the provider actually decided;
    * how many input tokens were served from cache, and how many were spent
      creating entries;
    * how many times the prefix changed, which is how a tool-schema edit is
      shown to invalidate the cache exactly once.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.calls = 0
        self.status_counts: Dict[str, int] = {}
        self.cached_input_tokens = 0
        self.cache_creation_tokens = 0
        self.invalidations = 0
        self.prefixes_seen: set[str] = set()
        self._last_prefix: Optional[str] = None
        self.recent: List[Dict[str, Any]] = []
        self.max_recent = 50

    def record(
        self,
        plan: CachePlan,
        receipt: CacheReceipt,
        *,
        model: str = "",
        api_base_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record one call and return the ledger delta for that call.

        An invalidation is exactly one prefix-digest CHANGE between
        consecutive calls, so a tool-schema edit invalidates the cache once and
        the very next call on the new prefix re-creates the entry.
        """
        entry = receipt.to_dict()
        entry["model"] = str(model or "")
        entry["api_base_sha256"] = str(api_base_sha256 or "")
        with self._lock:
            self.calls += 1
            self.status_counts[receipt.status] = (
                self.status_counts.get(receipt.status, 0) + 1
            )
            self.cached_input_tokens += receipt.cached_input_tokens
            self.cache_creation_tokens += receipt.cache_creation_tokens
            changed = (
                self._last_prefix is not None
                and self._last_prefix != plan.prefix_sha256
            )
            if changed:
                self.invalidations += 1
            self.prefixes_seen.add(plan.prefix_sha256)
            self._last_prefix = plan.prefix_sha256
            entry["invalidated"] = changed
            self.recent.append(entry)
            if len(self.recent) > self.max_recent:
                del self.recent[: len(self.recent) - self.max_recent]
        return entry

    def summary(self) -> Dict[str, Any]:
        """Return the cache hit rate and token totals for this ledger."""
        with self._lock:
            decided = sum(
                self.status_counts.get(status, 0) for status in _DECIDED_STATUSES
            )
            hits = sum(self.status_counts.get(status, 0) for status in _HIT_STATUSES)
            hit_rate = (hits / decided) if decided else 0.0
            return {
                "cache_calls": self.calls,
                "cache_hit_rate": round(hit_rate, 4),
                "cache_hits": hits,
                "cache_decided_calls": decided,
                "cache_invalidation_count": self.invalidations,
                "cache_distinct_prefixes": len(self.prefixes_seen),
                "cached_input_tokens": self.cached_input_tokens,
                "cache_creation_input_tokens": self.cache_creation_tokens,
                "cache_status_counts": dict(self.status_counts),
            }

    def reset(self) -> None:
        """Forget every recorded call (used by tests and fresh runs)."""
        with self._lock:
            self.calls = 0
            self.status_counts = {}
            self.cached_input_tokens = 0
            self.cache_creation_tokens = 0
            self.invalidations = 0
            self.prefixes_seen = set()
            self._last_prefix = None
            self.recent = []


_LEDGER: contextvars.ContextVar[Optional[PromptCacheLedger]] = contextvars.ContextVar(
    "neo_prompt_cache_ledger", default=None
)


def set_cache_ledger(ledger: Optional[PromptCacheLedger]) -> PromptCacheLedger:
    """Install a ledger for the current thread/task and return it.

    Passing ``None`` creates a fresh ledger, which is what a new execution
    context wants: a resumed task starts its own accounting rather than
    inheriting another thread's.
    """
    resolved = ledger if ledger is not None else PromptCacheLedger()
    _LEDGER.set(resolved)
    return resolved


def current_cache_ledger() -> PromptCacheLedger:
    """Return this context's ledger, creating one on first use."""
    ledger = _LEDGER.get()
    if ledger is None:
        ledger = set_cache_ledger(None)
    return ledger


def reset_cache_context() -> None:
    """Drop the current context's ledger (the next use starts a fresh one)."""
    _LEDGER.set(None)
