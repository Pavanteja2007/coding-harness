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

import collections.abc as _abc
import contextvars
import datetime as _datetime
import decimal as _decimal
import enum as _enum
import hashlib
import json
import os
import re
import threading
import unicodedata
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CACHE_BREAKPOINT_LIMIT",
    "CACHE_CREATION",
    "CACHE_DIALECTS",
    "CACHE_DIALECT_REFUSALS",
    "CACHE_HIT",
    "CACHE_MISS",
    "CACHE_PARTIAL",
    "CACHE_STATUSES",
    "CACHE_UNREPORTED",
    "CACHE_UNSUPPORTED",
    "EXTRA_CACHED_TOKEN_KEYS",
    "INVALIDATION_CAUSES",
    "STAMP_REGION_MARKERS",
    "VOLATILE_REGION_MARKERS",
    "CacheDialect",
    "CacheDialectRefused",
    "CachePlan",
    "CacheReceipt",
    "PromptCacheLedger",
    "apply_cache_parameters",
    "cache_control_kwargs",
    "cache_dialect_for",
    "cache_dialect_table",
    "cache_parameters_for",
    "cache_session_key",
    "canonical_json",
    "canonical_prompt_text",
    "canonical_tool_schemas",
    "current_cache_ledger",
    "date_only",
    "enforce_cache_dialect_gate",
    "estimate_tokens",
    "frozen_prefix",
    "has_full_timestamp",
    "normalize_tool_schemas",
    "plan_cache",
    "prefix_digest",
    "prefix_instability",
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

#: Maximum number of ``cache_control`` breakpoints an Anthropic-family
#: request may carry. Anthropic documents 4. **Exceeding the cap is a hard
#: error**, not a degradation: a request with a fifth breakpoint is rejected
#: by the API, and the agent's whole turn dies on an optimisation. A deferred
#: tool search can plausibly push a session over, which is why the plan
#: carries the count and refuses rather than sending and finding out.
CACHE_BREAKPOINT_LIMIT = 4

#: Provider-side cached-read field names that this module did not previously
#: know about. DeepSeek reports ``prompt_cache_hit_tokens`` /
#: ``prompt_cache_miss_tokens``; Gemini reports
#: ``cachedContentTokenCount``. Without these a working DeepSeek or Gemini
#: cache reads as ``unreported`` forever, and an operator concludes their
#: caching is broken when it is not.
EXTRA_CACHED_TOKEN_KEYS: Tuple[str, ...] = (
    "prompt_cache_hit_tokens",
    "cachedContentTokenCount",
    "cached_content_token_count",
    "cache_read_input_tokens_details",
)

#: Full-timestamp shapes that must never reach a frozen prefix. A second of
#: clock movement inside the prefix invalidates the provider's cache entry on
#: the very next turn, and the receipt then shows an invalidation an operator
#: cannot explain (nothing about their edit changed). ``date_only`` reduces one
#: to its date; :func:`has_full_timestamp` is the assertion that none is there.
_FULL_TIMESTAMP_RES = (
    # 2026-10-04T20:51:00(.123456)?(Z|+00:00)
    re.compile(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    ),
    # 20:51:00.123456 on its own, with a date nearby (a log line, a stamp block)
    re.compile(r"(?<!\d)\d{2}:\d{2}:\d{2}(?:\.\d+)?(?!\d)"),
)

#: Regions of a rendered prompt this module OWNS and may therefore reduce to a
#: date. A timestamp in user-supplied issue text is the user's data and is
#: never rewritten -- rewriting it would change what the model reads to make
#: a cache number look better, which is the wrong direction for a
#: correctness-adjacent field. Markers are read from the harness renderers.
STAMP_REGION_MARKERS: Tuple[str, ...] = (
    "## Session stamp",
    "## Run started",
)

#: Markers that name a VOLATILE region which must never be inside a frozen
#: prefix. A tool-search result in the cached region is the sharpest version
#: of this bug: the next turn would read a stale result as if it were the
#: catalog. Read from the renderers where possible; the first two are
#: ``harness.prompts``' own headers.
VOLATILE_REGION_MARKERS: Tuple[str, ...] = (
    "## Overall issue",
    "## Retrieved context",
    "## Tool search results",
    "## Applicable skills",
    "## Relevant past decisions",
    "## Coordinated-change fan-out",
    "## Active request",
    "## Structured continuity",
)


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


#: Tag key for a set/frozenset, which has no JSON spelling. Namespaced with a
#: prefix no tool schema contains. The honest limit: a caller's own dict with
#: this literal key would canonicalise to the same bytes as a set -- contrived,
#: but stated rather than left for someone to discover.
SET_TAG = "__neo_canonical_set__"

#: Tag key for a value this module could not canonicalise BY VALUE. It carries
#: the TYPE NAME and nothing else -- see :func:`_canonical_value` for why the
#: value is not in there.
OPAQUE_TAG = "__neo_canonical_opaque__"

#: The text sentinel for the same refusal where a plain string is required
#: (``_message_text`` on a message that is not a mapping). Angle-bracketed and
#: prefixed so it cannot be confused with prompt text, and carrying no address.
OPAQUE_TEXT = "<neo-opaque:"

#: How deep :func:`_canonical_value` recurses before declaring the remainder
#: opaque. A self-referential payload from a provider used to raise
#: ``RecursionError`` -- on the live request path, inside ``plan_cache`` -- which
#: the docstring's "never raises on a provider payload" promised against.
CANONICAL_MAX_DEPTH = 64

#: Why each canonicalisation finding matters, as one sentence each. A source name
#: on its own tells a reader they have a problem; it does not tell them whether
#: the digest they are looking at is still trustworthy.
_CANONICALISATION_DETAIL: Dict[str, str] = {
    "non_canonical_prefix_value": (
        "the digest can only name this value by its TYPE, so two different "
        "values of that type collide: the digest is a lower bound on identity, "
        "not an identity"
    ),
    "set_in_prefix_value": (
        "a set has no JSON spelling; the digest is stable across processes but "
        "the bytes on the wire carry a tagged marker object the provider's "
        "schema does not declare"
    ),
    "cycle_in_prefix_value": (
        "a self-referential value in the prefix; this used to raise "
        "RecursionError on the live request path"
    ),
    "max_depth_in_prefix_value": (
        f"the prefix nests deeper than CANONICAL_MAX_DEPTH ({CANONICAL_MAX_DEPTH}); "
        "everything below that depth is summarised as one opaque marker"
    ),
}

#: The types this module canonicalises BY VALUE rather than by type name. Each
#: entry is (predicate, renderer). These are all realistic provider-payload or
#: config-payload values, and each has a spelling that is a pure function of the
#: value -- which is the only thing a digest may hash.
_BY_VALUE_TYPES: Tuple[Tuple[Any, Any], ...] = (
    (os.PathLike, os.fspath),
    (_decimal.Decimal, str),
    (_uuid.UUID, str),
    (_datetime.datetime, lambda v: v.isoformat()),
    (_datetime.date, lambda v: v.isoformat()),
    (_datetime.time, lambda v: v.isoformat()),
    (_datetime.timedelta, lambda v: v.total_seconds()),
)


def _canonical_text(value: Any) -> str:
    """Return the ONE string coercion this module uses. It never returns an address.

    ``str(obj)`` on an arbitrary object yields ``"<Marker object at
    0x00000202D772FFD0>"``. That is not a canonicalisation -- it is a memory
    address wearing a digest's clothes: it differs between two logically
    identical objects *in one process*, and between every process. Measured,
    before this function existed:

        canonical_json({"note": Marker("x")}) == canonical_json({"note": Marker("x")})
        -> False

    So every branch that used to reach bare ``str()`` on an unrecognised value
    comes through here instead, and the refusal is a stable, readable token
    naming the TYPE. :func:`prefix_instability` reports the same values, so a
    refusal is visible rather than silent.
    """
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    for kind, render in _BY_VALUE_TYPES:
        if isinstance(value, kind):
            try:
                return unicodedata.normalize("NFC", str(render(value)))
            except Exception:  # pragma: no cover - a renderer that refuses
                break
    if isinstance(value, _enum.Enum):
        # The VALUE, not ``"Color.RED"``: renaming the enum class then does not
        # move the digest, and two enums with the same value correctly collide
        # because they ARE the same thing on the wire.
        return _canonical_text(value.value)
    return f"{OPAQUE_TEXT}{type(value).__qualname__}>"


def _message_text(message: Any) -> str:
    """Return the plain text of a chat message, whatever content shape it has."""
    if not isinstance(message, Mapping):
        return "" if message is None else _canonical_text(message)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: List[str] = []
        for block in content:
            if isinstance(block, Mapping):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(_canonical_text(block))
        return "\n".join(part for part in parts if part)
    return "" if content is None else _canonical_text(content)


def date_only(value: Any) -> str:
    """Return a timestamp reduced to its DATE.

    The corpus's finding is that a second-resolution clock in the prompt
    destroys the prefix on every turn. A date changes once per day, so a
    prompt carrying one is byte-stable for a whole session.

    Only ISO-8601-shaped values are reduced. Anything unrecognised is returned
    UNCHANGED rather than guessed at: a mangled timestamp that read as a date
    would be a different lie from one that stays a timestamp.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if not text:
        return text
    match = _FULL_TIMESTAMP_RES[0].search(text)
    if match is not None:
        return text[: match.start()] + match.group(0)[:10] + text[match.end() :]
    return text


def has_full_timestamp(text: Any) -> bool:
    """Whether a full (time-of-day bearing) timestamp appears in ``text``.

    This is the assertion, not the fix. A prefix that answers ``True`` here is
    a prefix whose cache entry is invalidated by the clock, and the
    invalidation receipt will name an edit the operator never made.
    """
    body = text if isinstance(text, str) else ("" if text is None else str(text))
    if not body:
        return False
    return any(pattern.search(body) for pattern in _FULL_TIMESTAMP_RES)


def canonical_prompt_text(text: Any) -> str:
    """Return the byte-stable form of prompt text this module sends.

    Two normalisations, both aimed at the same property -- two logically
    identical prompts must produce identical BYTES:

    1. **NFC Unicode normalisation.** ``"caf\\u00e9"`` and ``"cafe\\u0301"`` are
       the same string to a reader and different bytes to a cache key. Without
       this, a tool description or a memory note that reached the prefix
       through two different composition paths silently splits the cache.
    2. **Date-only inside a stamp region this module owns.** A clock in a
       region we generated is our own instability and we reduce it. A clock
       in user-supplied issue text is left alone on purpose.

    Everything else -- spacing, newlines, wording -- is returned verbatim.
    Rewriting a prompt to make a cache number look better is the wrong
    direction for a field a human reads.
    """
    body = text if isinstance(text, str) else ("" if text is None else str(text))
    if not body:
        return body
    body = unicodedata.normalize("NFC", body)
    for marker in STAMP_REGION_MARKERS:
        if marker in body:
            head, _, tail = body.partition(marker)
            body = head + marker + date_only(tail)
    return body


def _canonical_value(
    value: Any,
    *,
    _findings: Optional[List[Dict[str, Any]]] = None,
    _path: str = "$",
    _depth: int = 0,
    _stack: Optional[List[int]] = None,
) -> Any:
    """Recursively rebuild ``value`` into a canonical, JSON-safe shape.

    Canonical means: mapping keys sorted and stringified by
    :func:`_canonical_key`, strings NFC-normalised, sets emitted as a sorted
    tagged list, non-finite floats refused rather than serialised (JSON has no
    spelling for NaN and every reader spells it differently), and **nothing
    unrecognised ever reaching ``str()``**.

    That last clause is the whole point of this rewrite, and it was measured
    three ways on the previous version of this function:

    1. **A ``set`` was hash-ordered.** ``set``/``frozenset`` fell through to the
       final ``str(value)``, and CPython's string hashing is seed-randomised, so
       the same catalog produced a different digest in every process::

           seed 1 -> {"tools":"{'read', 'write', 'grep', 'glob'}"}
           seed 2 -> {"tools":"{'grep', 'read', 'write', 'glob'}"}
           seed 3 -> {"tools":"{'read', 'glob', 'write', 'grep'}"}

       A catalog is the one payload in this system that is legitimately a set of
       strings, and a worker process re-hashing its own catalog per turn is a
       receipt that reports "one stable prefix" while the provider re-reads the
       entry in full. This is the Wave-1 digest-vs-bytes defect arriving through
       a different door.
    2. **An arbitrary object produced its memory address** (``"<Marker object at
       0x...>"``), so two logically identical objects in ONE process disagreed.
    3. **A self-referential payload raised ``RecursionError``** -- inside
       ``plan_cache``, on the live request path -- contradicting this very
       docstring's promise.

    **Why an opaque value is tagged rather than refused.** Raising is not an
    option (above), and hashing the type name alone would make two *different*
    values of the same type collide -- which is the projection-vs-bytes defect
    again, one level down. So the tag names the type and :func:`prefix_instability`
    reports every occurrence: the digest stays reproducible, and the receipt
    says the identity is a lower bound rather than pretending otherwise.

    ``_findings`` collects ``{source, path, type}`` rows for every opaque value,
    a set, and a depth stoppage. It is optional, so the pure callers are
    byte-identical to before.
    """
    if _stack is None:
        _stack = []

    # A container already on the stack is a CYCLE, not a shared subobject. A
    # shared subobject appearing twice in a tree is legal and must still hash,
    # so this is a stack and deliberately not a visited-set.
    if isinstance(value, (_abc.Mapping, _abc.Set, list, tuple)) and id(value) in _stack:
        _note(_findings, "cycle_in_prefix_value", _path, type(value).__name__)
        return {OPAQUE_TAG: "cycle"}
    if _depth >= CANONICAL_MAX_DEPTH:
        _note(_findings, "max_depth_in_prefix_value", _path, type(value).__name__)
        return {OPAQUE_TAG: "max_depth_exceeded"}

    if isinstance(value, _abc.Mapping):
        _stack.append(id(value))
        try:
            return {
                _canonical_key(key): _canonical_value(
                    value[key],
                    _findings=_findings,
                    _path=f"{_path}.{_canonical_key(key)}",
                    _depth=_depth + 1,
                    _stack=_stack,
                )
                for key in sorted(value, key=str)
            }
        finally:
            _stack.pop()

    if isinstance(value, _abc.Set):
        # A set has no JSON spelling, so it is emitted as a SORTED tagged list.
        # Sorting is on each member's canonical JSON text, because a set may mix
        # types and `sorted()` on a mixed set raises -- a raise on a provider
        # payload is the failure mode this function exists to remove.
        _note(_findings, "set_in_prefix_value", _path, type(value).__name__)
        members = [
            _canonical_value(
                item,
                _findings=_findings,
                _path=f"{_path}[set]",
                _depth=_depth + 1,
                _stack=_stack,
            )
            for item in value
        ]
        members.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        return {SET_TAG: members}

    if isinstance(value, (list, tuple)):
        _stack.append(id(value))
        try:
            return [
                _canonical_value(
                    item,
                    _findings=_findings,
                    _path=f"{_path}[{index}]",
                    _depth=_depth + 1,
                    _stack=_stack,
                )
                for index, item in enumerate(value)
            ]
        finally:
            _stack.pop()

    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            # NaN/inf have no canonical JSON spelling. ``str`` gives a stable
            # one, which is what "bit-perfect" needs -- not a raise, because
            # this runs on every request.
            return str(value)
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, _enum.Enum):
        return _canonical_value(
            value.value,
            _findings=_findings,
            _path=_path,
            _depth=_depth + 1,
            _stack=_stack,
        )

    # A type this module canonicalises BY VALUE. Checked after the JSON-native
    # types above so nothing above changes meaning, and before the opaque
    # fallback because a `str()` of these loses the value: `str(Decimal(...))`
    # keeps precision that a float round-trip would drop, and `str(Path(...))`
    # is platform-shaped.
    for kind, render in _BY_VALUE_TYPES:
        if isinstance(value, kind):
            try:
                return _canonical_value(
                    render(value),
                    _findings=_findings,
                    _path=_path,
                    _depth=_depth + 1,
                    _stack=_stack,
                )
            except Exception:  # pragma: no cover - a renderer that refuses
                break

    _note(_findings, "non_canonical_prefix_value", _path, type(value).__qualname__)
    return {OPAQUE_TAG: type(value).__qualname__}


def _note(
    findings: Optional[List[Dict[str, Any]]],
    source: str,
    path: str,
    type_name: str,
) -> None:
    """Append one canonicalisation finding. No-op when nobody is listening."""
    if findings is None:
        return
    findings.append({"source": source, "path": path, "type": type_name})


def _canonical_key(key: Any) -> str:
    """Return the canonical string form of a mapping key.

    Keys were already ``str()``-coerced and sorted before this round, so no
    existing digest moves. What is added is the address rule: a key that is not
    a JSON scalar must not reach bare ``str()``, and a tuple key must not
    become its repr (``"(2, 3)"``), which is a different spelling per distinct
    tuple and therefore not an identity.
    """
    if isinstance(key, str):
        return unicodedata.normalize("NFC", key)
    if isinstance(key, bool) or key is None:
        return "true" if key is True else ("false" if key is False else "null")
    if isinstance(key, (int, float)):
        return repr(key) if isinstance(key, float) else str(key)
    if isinstance(key, (bytes, bytearray)):
        return key.decode("utf-8", "replace")
    if isinstance(key, (list, tuple)):
        return json.dumps(
            [_canonical_value(item) for item in key],
            sort_keys=True,
            separators=(",", ":"),
        )
    return _canonical_text(key)


def canonical_json(
    value: Any, *, findings: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Serialise ``value`` to its single canonical JSON string.

    Sorted keys, no incidental whitespace, NFC strings. Two logically
    identical values MUST produce byte-identical output; that equality is the
    whole reason this function exists, and ``runtime``'s prefix-stability pin
    asserts it on sha256 rather than on "the fields look the same".

    ``findings`` is an optional sink. It is appended to, never read, so the pure
    callers are byte-identical to before and a caller that wants to know whether
    the digest it just computed was over real values asks for the rows.
    """
    return json.dumps(
        _canonical_value(value, _findings=findings),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def canonical_tool_schemas(
    tools: Optional[Iterable[Any]],
    *,
    findings: Optional[List[Dict[str, Any]]] = None,
) -> List[Any]:
    """Return the tool catalog in the exact form this module puts on the wire.

    This is a pure **re-serialisation**, not a projection: every field the
    caller declared survives, because dropping a field a provider needs would
    break the call. What changes is only the order and the formatting, so that
    two logically identical catalogs produce identical BYTES.

    Why this exists at all -- it was measured, not reasoned. With the previous
    behaviour the digest normalised key order while the payload did not, so a
    caller that built the same schemas in a different order produced:

    * ``cache_invalidation_count = 0``
    * ``cache_distinct_prefixes = 1``
    * **two provider cache creations**

    A receipt saying "one prefix, never invalidated" beside an invoice that
    paid for two writes is worse than no receipt, so the digest and the bytes
    are now the same thing by construction: the digest hashes exactly the
    bytes ``apply_cache_parameters`` emits.

    ``findings`` exists because this is where a set or an opaque value in the
    catalog is FIRST seen. Phase 3b/W2 measured both (``str(set)`` is
    seed-dependent; ``str(obj)`` is a memory address), and a caller that only
    canonicalised the already-tagged output would find neither -- the tags would
    have swallowed the evidence.
    """
    canonical: List[Any] = []
    for tool in tools or []:
        canonical.append(_canonical_value(tool, _findings=findings))
    # The ARRAY order is part of the provider's key too, so it is sorted --
    # but on the canonical form, so the sort is a function of the content.
    canonical.sort(key=canonical_json)
    return canonical


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
    """Return a stable digest of the tool catalog AS SENT ("" when absent).

    Phase 3b/W1 correction: this used to hash a *projected* form (name,
    description, parameter names, types). The projection is order-insensitive,
    which was the right instinct and the wrong target -- it made the digest
    insensitive to byte differences the provider DOES key on, so the receipt
    could report a stable prefix while the provider re-read it in full.

    It now hashes :func:`canonical_tool_schemas`, i.e. the exact bytes
    ``apply_cache_parameters`` emits. An incidental provider field the
    projection used to discard (``"strict": true``, say) now moves the digest,
    and that is correct: it moves the bytes too.
    """
    canonical = canonical_tool_schemas(tools)
    if not canonical:
        return ""
    return hashlib.sha256(canonical_json(canonical).encode("utf-8")).hexdigest()


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

    Phase 3b/W1: both halves are now the **bytes**, not a projection of them.
    Message text goes through :func:`canonical_prompt_text` (NFC, date-only in
    a stamp region) and the catalog through :func:`canonical_tool_schemas`,
    which is the same value :func:`apply_cache_parameters` sends. Digest
    equality therefore implies byte equality, which is the only form of this
    claim a provider can agree with.
    """
    prefix, _suffix, _index = split_at_breakpoint(messages, tools, breakpoint_index)
    payload = {
        "messages": [
            {
                "role": item.get("role", "system")
                if isinstance(item, Mapping)
                else "system",
                "content": canonical_prompt_text(_message_text(item)),
            }
            for item in prefix
        ],
        "tools": canonical_tool_schemas(tools),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def prefix_instability(
    messages: Optional[Sequence[Any]],
    tools: Optional[Iterable[Any]] = None,
    breakpoint_index: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Report every named reason the frozen prefix is not really stable.

    A prefix digest can only say *that* it changed, never *why*. An operator
    looking at a cache report that shows one invalidation per turn cannot act
    on that, so each volatile source is located and named here:

    * ``full_timestamp`` -- a clock inside the prefix. The provider re-reads
      the entry on the next turn and the receipt blames an edit nobody made.
    * ``volatile_region`` -- a header that names per-turn content (retrieved
      context, skills, tool-search results, decisions). If one is inside the
      prefix, the next turn reads it as if it were still current.
    * ``non_text_content`` -- a content block the digest reduces to its text
      alone, so an image or a structured part can change without the digest
      noticing.

    Phase 3b/W2 adds the **canonicalisation** sources, named because they were
    all measured on the previous implementation of :func:`_canonical_value`:

    * ``non_canonical_prefix_value`` -- a value the digest could only name by
      its TYPE. The digest is then a *lower bound* on identity: two different
      values of that type collide. Reproducible, not silent, not sound.
    * ``set_in_prefix_value`` -- a set in the payload. The digest is stable
      across processes now, but the wire bytes carry the tagged marker object
      rather than a set, which is a schema the provider does not declare.
    * ``cycle_in_prefix_value`` / ``max_depth_in_prefix_value`` -- a
      self-referential or pathologically nested payload. These used to raise
      ``RecursionError`` inside this function, on the live request path.

    Every row carries the marker and the character offset, because "your
    prompt is unstable" is not actionable and ``## Retrieved context at 4120``
    is.
    """
    prefix, _suffix, _index = split_at_breakpoint(messages, tools, breakpoint_index)
    found: List[Dict[str, Any]] = []
    for position, message in enumerate(prefix):
        text = canonical_prompt_text(_message_text(message))
        if not text:
            continue
        # Which PATTERN matched matters: a bare HH:MM:SS (a log line quoted into
        # a system prompt) is a clock just as much as a full ISO stamp, and the
        # first version of this function indexed pattern 0 unconditionally and
        # raised AttributeError on exactly that input -- a crash on the live
        # call path, found by this round's own pin.
        clock = None
        for pattern in _FULL_TIMESTAMP_RES:
            clock = pattern.search(text)
            if clock is not None:
                break
        if clock is not None:
            found.append(
                {
                    "source": "full_timestamp",
                    "message_index": position,
                    "example": clock.group(0),
                    "char_offset": clock.start(),
                    "detail": "a clock inside the prefix invalidates every turn",
                }
            )
        for marker in VOLATILE_REGION_MARKERS:
            offset = text.find(marker)
            if offset >= 0:
                found.append(
                    {
                        "source": "volatile_region",
                        "message_index": position,
                        "marker": marker,
                        "char_offset": offset,
                        "detail": (
                            "per-turn content inside the cached prefix; the "
                            "next turn reads it as current"
                        ),
                    }
                )
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, (list, tuple)) and not all(
                isinstance(block, Mapping) and set(block) <= {"type", "text"}
                for block in content
            ):
                found.append(
                    {
                        "source": "non_text_content",
                        "message_index": position,
                        "detail": (
                            "a structured content block whose non-text parts "
                            "the prefix digest does not cover"
                        ),
                    }
                )

    # The canonicalisation pass runs over the payload ``prefix_digest`` hashes,
    # so these rows and that digest cannot disagree about what was in it. The
    # CATALOG is canonicalised HERE, raw, because this is the first and only pass
    # that sees a set or an opaque value in it: canonicalising it twice (once
    # here, once inside ``prefix_digest``) would find the tags this pass just
    # wrote and report nothing, which is how the first version of this wiring
    # reported a clean receipt for a payload it had just proven unstable.
    findings: List[Dict[str, Any]] = []
    canonical_json(
        {
            "messages": prefix,
            "tools": canonical_tool_schemas(tools, findings=findings),
        },
        findings=findings,
    )
    for row in findings:
        found.append(
            {
                "source": row["source"],
                "message_index": -1,
                "path": row["path"],
                "type": row["type"],
                "detail": _CANONICALISATION_DETAIL.get(
                    row["source"], "a prefix value this module could not canonicalise"
                ),
            }
        )
    return found


def _provider_family(provider: Optional[str], model: Optional[str]) -> str:
    """Return a coarse provider family used to pick cache parameters."""
    text = f"{provider or ''} {model or ''}".lower()
    if any(family in text for family in ANTHROPIC_PROVIDERS):
        return "anthropic"
    if "gemini" in text or "vertex" in text:
        return "google"
    if "bedrock" in text:
        return "aws"
    if "deepseek" in text:
        return "deepseek"
    return "openai"


# ---------------------------------------------------------------------------
# THE DIALECT TABLE. Data, so a new provider is a data edit.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheDialect:
    """How ONE provider family is asked to cache, and what it reports back.

    ``mechanism`` is the load-bearing field and it is one of exactly three
    words, because the three behave completely differently and conflating them
    is how a ``cache_control`` key ends up on an OpenAI-shaped gateway -- a
    hard 400 on most of them:

    ``breakpoints``
        We place explicit markers and the provider caches what precedes them.
    ``implicit``
        The provider caches a repeated prefix on its own; there is nothing to
        send, and sending anything is the bug.
    ``keyed``
        The provider caches automatically but routes on a caller-supplied
        stable key, so a key must be passed.
    ``bytes``
        There is no provider-side cache API at all and the endpoint's own KV
        cache keys on raw token bytes.

    Every row also carries the usage field names that family's response uses,
    because "the provider reports cache usage under a name we did not read" is
    indistinguishable from "caching is broken" in a cost report.
    """

    dialect: str
    families: Tuple[str, ...]
    markers: Tuple[str, ...]
    mechanism: str
    message_parameter: Optional[str] = None
    parameter_value: Optional[Mapping[str, Any]] = None
    tool_parameter: Optional[str] = None
    request_parameter: Optional[str] = None
    breakpoint_limit: int = CACHE_BREAKPOINT_LIMIT
    cached_usage_keys: Tuple[str, ...] = ()
    miss_usage_keys: Tuple[str, ...] = ()
    note: str = ""

    def wants_breakpoints(self) -> bool:
        """Whether this dialect requires us to place explicit markers."""
        return self.mechanism == "breakpoints"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe description of the row (for receipts and docs)."""
        return {
            "dialect": self.dialect,
            "mechanism": self.mechanism,
            "families": list(self.families),
            "message_parameter": self.message_parameter or "",
            "tool_parameter": self.tool_parameter or "",
            "request_parameter": self.request_parameter or "",
            "breakpoint_limit": self.breakpoint_limit,
            "cached_usage_keys": list(self.cached_usage_keys),
            "miss_usage_keys": list(self.miss_usage_keys),
            "note": self.note,
        }


#: The one table. A new provider is a row here, never a branch in code.
CACHE_DIALECTS: Tuple[CacheDialect, ...] = (
    CacheDialect(
        dialect="anthropic",
        families=("anthropic", "claude"),
        markers=("anthropic", "claude"),
        mechanism="breakpoints",
        message_parameter="cache_control",
        parameter_value={"type": "ephemeral"},
        tool_parameter="cache_control",
        breakpoint_limit=CACHE_BREAKPOINT_LIMIT,
        cached_usage_keys=("cache_read_input_tokens",),
        note=(
            "Explicit ephemeral breakpoints, up to 4. Exceeding the cap is a "
            "hard 400, so the plan counts them and refuses rather than sends."
        ),
    ),
    CacheDialect(
        dialect="bedrock",
        families=("bedrock", "aws"),
        markers=("bedrock", "converse"),
        mechanism="breakpoints",
        message_parameter="cachePoint",
        parameter_value={"type": "default"},
        breakpoint_limit=CACHE_BREAKPOINT_LIMIT,
        cached_usage_keys=("cacheReadInputTokens",),
        note=(
            "The Converse API spells the marker cachePoint, not cache_control, "
            "and Bedrock has no implicit prefix caching at all -- an Anthropic "
            "spelling fails."
        ),
    ),
    CacheDialect(
        dialect="vertex_ai",
        families=("vertex_ai", "vertex"),
        # "aiplatform" is measured, not guessed. Vertex hosts look like
        # `us-central1-aiplatform.googleapis.com`, which contains the substring
        # "google" -- and the `gemini` row below matches on "google". Because
        # the api_base pass runs in table order, the gemini row won on a real
        # Vertex endpoint and the vertex_ai row was reachable ONLY by a
        # provider string. Nothing behavioural was wrong (both rows are
        # `implicit`, send nothing, and read `cachedContentTokenCount`), but an
        # operator debugging a Vertex deployment was told `gemini`, which sends
        # them to the wrong file. `generativelanguage.googleapis.com` contains
        # no "aiplatform", so the gemini row keeps that one.
        markers=("vertex_ai", "vertex", "aiplatform"),
        mechanism="implicit",
        cached_usage_keys=("cachedContentTokenCount", "cached_content_token_count"),
        note=(
            "Vertex caches a repeated prefix on its own. An explicit "
            "cache_control is rejected on the generateContent path."
        ),
    ),
    CacheDialect(
        dialect="gemini",
        families=("google", "gemini"),
        markers=("gemini", "google", "generativelanguage"),
        mechanism="implicit",
        cached_usage_keys=("cachedContentTokenCount", "cached_content_token_count"),
        note=(
            "Implicit caching, reported in usageMetadata as "
            "cachedContentTokenCount. Explicit control is a separate "
            "cachedContent resource, not a per-request marker."
        ),
    ),
    CacheDialect(
        dialect="deepseek",
        families=("deepseek",),
        markers=("deepseek",),
        mechanism="implicit",
        cached_usage_keys=("prompt_cache_hit_tokens",),
        miss_usage_keys=("prompt_cache_miss_tokens",),
        note=(
            "Automatic, reported as prompt_cache_hit_tokens / "
            "prompt_cache_miss_tokens -- names this module did not read before, "
            "so a working DeepSeek cache read as unreported forever."
        ),
    ),
    CacheDialect(
        dialect="openai",
        families=("openai", "openrouter", "together", "groq", "fireworks"),
        markers=("openai", "openrouter", "together", "groq", "fireworks"),
        mechanism="keyed",
        request_parameter="prompt_cache_key",
        cached_usage_keys=("cached_tokens",),
        note=(
            "Caching is automatic. prompt_cache_key does not enable it -- it "
            "routes a request to the same cache host, so a STABLE per-session "
            "key is what makes the hits land."
        ),
    ),
    CacheDialect(
        dialect="local",
        families=("ollama", "llamacpp", "vllm", "lmstudio", "local"),
        markers=(
            "ollama",
            "llama.cpp",
            "llamacpp",
            "vllm",
            "lmstudio",
            "localhost",
            "127.0.0.1",
        ),
        mechanism="bytes",
        note=(
            "No provider-side cache API exists. llama.cpp and vLLM key their "
            "own KV/prefix caches on the raw token bytes, so bit-perfect "
            "prefix normalization is the ONLY lever here and it needs no "
            "provider cooperation at all."
        ),
    ),
)

#: Refusal slugs for :func:`enforce_cache_dialect_gate`. A closed set: an
#: unrecognised refusal must not be inventable at a call site.
CACHE_DIALECT_REFUSALS = (
    "foreign_marker_present",
    "required_marker_missing",
    "session_key_missing",
    "breakpoint_limit_exceeded",
)


class CacheDialectRefused(RuntimeError):
    """A request that would carry a dialect the endpoint rejects.

    Deliberately NOT a ``PermissionError``: ``provider_gateway`` absorbs those
    as its fail-closed path and reads them as "try the next candidate", which
    would turn a cache-dialect mistake into one more reason to spend money
    somewhere else. The same reasoning ``runtime.hardline`` documents for its
    own refusal type.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        dialect: str = "",
        expected: str = "",
        found: Tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.dialect = dialect
        self.expected = expected
        self.found = tuple(found)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe refusal for the ledger and the trace."""
        return {
            "cache_dialect_refused": True,
            "cache_dialect_refusal": self.reason,
            "cache_dialect_detail": self.detail,
            "cache_dialect": self.dialect,
            "cache_dialect_expected": self.expected,
            "cache_dialect_found": list(self.found),
        }


def cache_dialect_table() -> List[Dict[str, Any]]:
    """Return every dialect as data, for docs, ``/cost`` and the pins."""
    return [dialect.to_dict() for dialect in CACHE_DIALECTS]


def cache_dialect_for(
    provider: Optional[str],
    model: Optional[str] = None,
    api_base: Optional[str] = None,
) -> CacheDialect:
    """Return the ONE dialect row for this endpoint.

    Resolution order, and the order matters:

    1. **Explicit ``api_base`` markers.** A self-hosted llama.cpp behind an
       OpenAI-shaped URL must resolve to ``local``, and an operator's endpoint
       is a stronger fact about where the bytes land than a model name is
       about what is running.
    2. **A dialect named in the provider string** (``bedrock/``,
       ``vertex_ai/``, ``deepseek/``), which is litellm's own routing spelling.
    3. **Model-name markers**, because an ``openai``-named gateway routinely
       fronts Claude and Gemini.

    The Anthropic row is checked LAST and on the model name only: the word
    ``claude`` turns up in OpenAI-compatible base URLs often enough that
    letting it win on ``api_base`` would send ``cache_control`` to a gateway
    that 400s on it.

    Nothing here raises. An endpoint nobody recognises gets the ``openai`` row,
    which is the safest default in the table: it sends no marker and it passes
    the session key.
    """
    base = str(api_base or "").lower()
    provider_text = str(provider or "").lower()
    name_text = f"{provider or ''} {model or ''}".lower()
    # THREE separate passes, not one loop. A single pass in table order let the
    # `openai` row win on the provider string before `local` was ever
    # considered, so an OpenAI-shaped gateway in front of a self-hosted
    # llama.cpp resolved to `openai` -- and OpenAI's row carries
    # `prompt_cache_key`, which a llama.cpp server has never heard of.
    # Measured by this round's own pin.
    for dialect in CACHE_DIALECTS:
        if any(marker in base for marker in dialect.markers):
            return dialect
    for dialect in CACHE_DIALECTS:
        if dialect.dialect == "anthropic":
            continue
        if any(marker in provider_text for marker in dialect.markers):
            return dialect
    for dialect in CACHE_DIALECTS:
        if dialect.dialect == "anthropic":
            continue
        if any(marker in name_text for marker in dialect.markers):
            return dialect
    if "claude" in name_text or "anthropic" in name_text:
        return CACHE_DIALECTS[0]
    return next(row for row in CACHE_DIALECTS if row.dialect == "openai")


def cache_session_key(
    plan: "CachePlan",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the stable cache key to pass, and say where it came from.

    OpenAI's ``prompt_cache_key`` does not enable caching; it routes the
    request to the same cache host. Codex keys on ``thread_id`` and OpenCode on
    ``sessionID``, so a **stable per-session key is how a non-Anthropic
    provider gets its hits** -- and the corpus records OpenCode emitting six
    dialects simultaneously, which is precisely how such a key gets lost.

    The key is ``sha256(session_id | prefix_sha256)``:

    * stable for every turn of a session on an unchanged prefix, which is the
      property that makes the hits land;
    * **different when the prefix changes**, so a rewritten prefix does not go
      on asking for an entry that cannot exist;
    * different between sessions, so two runs never share a routing key.

    With no session id the key degrades to the prefix digest alone and the
    receipt says ``source="prefix_only"``. That is safe rather than merely
    convenient: two callers sharing a key is a cache-locality question only,
    because the provider still keys the entry on the actual prefix bytes, and
    sharing one requires the prefixes to be identical by definition.
    """
    session = str(session_id or "").strip()
    material = f"{session}|{plan.prefix_sha256}" if session else plan.prefix_sha256
    return {
        "cache_session_key": hashlib.sha256(material.encode("utf-8")).hexdigest()[:32],
        "cache_session_key_source": "session_and_prefix" if session else "prefix_only",
    }


def cache_parameters_for(
    dialect: CacheDialect,
    plan: "CachePlan",
    *,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the request-level parameters this dialect's endpoint accepts.

    Only ``mechanism == "keyed"`` gets anything here. Every other dialect
    returns an empty dict: sending a parameter a family does not document is
    the hard-400 failure this module exists to prevent, and the previous
    behaviour -- attaching Anthropic's ``cache_control`` to anything that was
    not literally Anthropic -- is why that split was pinned in the first
    place.
    """
    if dialect.mechanism != "keyed" or not dialect.request_parameter:
        return {}
    key = cache_session_key(plan, session_id)["cache_session_key"]
    return {dialect.request_parameter: key}


def enforce_cache_dialect_gate(
    messages: Sequence[Any],
    tools: Optional[Iterable[Any]],
    plan: "CachePlan",
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_base: Optional[str] = None,
    session_id: Optional[str] = None,
    request_kwargs: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Check the outgoing request against the dialect table before it is sent.

    The mirror of ``provider_gateway.enforce_stream_gate``, and deliberately the
    same shape: a streamed call cannot be failed over, so that gate exists to
    keep the safety checks running on the one path that skips the fallback
    pipeline. A cache dialect has the same character in reverse -- it is the
    one request parameter that turns a mistyped provider name into a **hard
    400**, and a hard 400 on turn 1 looks exactly like a broken agent.

    It refuses, loudly and with the reason, when:

    * a marker belonging to ANOTHER dialect is present
      (``foreign_marker_present``);
    * a ``breakpoints`` dialect was asked to cache but carries no marker
      (``required_marker_missing``);
    * a ``keyed`` dialect has no stable key to send (``session_key_missing``);
    * the request carries more breakpoints than the family's cap
      (``breakpoint_limit_exceeded``) -- the deferred-tool-search case, which
      is a refusal rather than an optimistic send.

    Returns the receipt it checked against, so a caller can record *why* the
    request was shaped the way it was.
    """
    dialect = cache_dialect_for(provider, model, api_base)
    extra = dict(request_kwargs or {})
    markers_present: List[str] = []
    for index, message in enumerate(messages or []):
        if not isinstance(message, Mapping):
            continue
        for row in CACHE_DIALECTS:
            if row.message_parameter and row.message_parameter in message:
                markers_present.append(row.message_parameter)
        if index == plan.breakpoint_index and dialect.wants_breakpoints():
            expected = dialect.message_parameter or ""
            # Only when we ACTUALLY asked to cache. A plan that declined --
            # `disabled`, `prefix_below_floor`, `empty_prefix` -- must not be
            # forced to carry a marker, and demanding one anyway turned every
            # short-prefix call into a refusal. The invariant worth pinning is
            # the narrow one: `plan.requested` IMPLIES a marker is present.
            if plan.requested and expected and expected not in message:
                raise CacheDialectRefused(
                    "required_marker_missing",
                    f"{dialect.dialect} caches only what precedes a "
                    f"{expected!r} marker, this plan requested a cache "
                    f"breakpoint, and none was placed",
                    dialect=dialect.dialect,
                    expected=expected,
                )
    for tool in tools or []:
        if not isinstance(tool, Mapping):
            continue
        for row in CACHE_DIALECTS:
            if row.tool_parameter and row.tool_parameter in tool:
                markers_present.append(row.tool_parameter)
    own = {dialect.message_parameter or "", dialect.tool_parameter or ""}
    foreign = sorted({name for name in markers_present if name} - own)
    if foreign:
        raise CacheDialectRefused(
            "foreign_marker_present",
            f"{dialect.dialect} does not accept {', '.join(foreign)}; an "
            f"unknown request key is a hard 400 on this endpoint",
            dialect=dialect.dialect,
            expected=dialect.message_parameter or "(none)",
            found=tuple(foreign),
        )
    # The COUNT of markers placed, not the number of distinct marker NAMES.
    # Anthropic's cap is on breakpoints, and one breakpoint per annotated block
    # is the whole point -- counting distinct names would report "1" for a
    # request carrying six of them, which is exactly the case the cap exists to
    # refuse.
    placed = len(markers_present)
    if dialect.wants_breakpoints() and placed > dialect.breakpoint_limit:
        raise CacheDialectRefused(
            "breakpoint_limit_exceeded",
            f"{dialect.dialect} accepts at most {dialect.breakpoint_limit} "
            f"cache breakpoints and this request carries {placed}",
            dialect=dialect.dialect,
            expected=str(dialect.breakpoint_limit),
            found=(str(placed),),
        )
    if (
        dialect.mechanism == "keyed"
        and dialect.request_parameter
        and not extra.get(dialect.request_parameter)
    ):
        raise CacheDialectRefused(
            "session_key_missing",
            f"{dialect.dialect} routes its cache on "
            f"{dialect.request_parameter!r} and none was supplied, so "
            f"every turn is a cold prefix",
            dialect=dialect.dialect,
            expected=dialect.request_parameter,
        )
    receipt = {
        "cache_dialect": dialect.dialect,
        "cache_dialect_mechanism": dialect.mechanism,
        "cache_breakpoints_placed": placed,
        "cache_breakpoint_limit": dialect.breakpoint_limit,
        "cache_dialect_note": dialect.note,
    }
    receipt.update(cache_session_key(plan, session_id))
    return receipt


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
    #: Phase 3b/W1: the dialect row this endpoint resolved to. Drives the
    #: marker spelling and whether a session key is sent at all.
    dialect: str = "openai"
    #: How the endpoint caches: ``breakpoints`` | ``implicit`` | ``keyed`` |
    #: ``bytes``. The reason ``requested`` means what it does for a non-
    #: Anthropic family, which the old ``provider_family`` string could not say.
    mechanism: str = "bytes"
    #: How many explicit breakpoints this request will carry, and the cap.
    breakpoints: int = 0
    breakpoint_limit: int = CACHE_BREAKPOINT_LIMIT
    #: Every named reason the prefix is not really stable (see
    #: :func:`prefix_instability`). Carried on the plan so the invalidation a
    #: clock causes names itself instead of looking like an operator's edit.
    instability: Tuple[Dict[str, Any], ...] = ()
    #: True when the tool catalog itself is unstable -- the one thing that
    #: invalidates the prefix for a reason no prompt edit caused.
    tool_surface_unstable: bool = False
    #: Phase 3b/W2: True when the digest was computed over at least one value
    #: this module could not canonicalise BY VALUE. Separate from
    #: ``cache_prefix_unstable`` on purpose: a clock in the prefix is a known,
    #: understood instability, whereas an uncanonicalisable value means the
    #: digest is a LOWER BOUND on identity -- two different values of one type
    #: collide -- and an operator needs to act on that differently.
    uncanonical: bool = False
    #: The distinct canonicalisation sources that fired, sorted. Named rather
    #: than counted, because ``non_canonical_prefix_value`` once and
    #: ``non_canonical_prefix_value`` a hundred times are the same problem.
    uncanonical_reasons: Tuple[str, ...] = ()

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
            "cache_dialect": self.dialect,
            "cache_dialect_mechanism": self.mechanism,
            "cache_breakpoints": self.breakpoints,
            "cache_breakpoint_limit": self.breakpoint_limit,
            "cache_prefix_unstable": bool(self.instability),
            "cache_prefix_instability": [dict(row) for row in self.instability],
            "cache_tool_surface_unstable": self.tool_surface_unstable,
            "cache_prefix_uncanonical": bool(self.uncanonical),
            "cache_prefix_uncanonical_reasons": list(self.uncanonical_reasons),
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
    dialect: str = "openai"

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
            "cache_dialect": self.dialect,
        }


def plan_cache(
    messages: Optional[Sequence[Any]],
    tools: Optional[Iterable[Any]] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_base: Optional[str] = None,
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

    Phase 3b/W1: the decision is made against the **dialect table**, not
    against a provider-name test. ``wants_breakpoints()`` is what separates
    "we place a marker" from "the endpoint caches on its own", so Bedrock's
    ``cachePoint`` and a local llama.cpp's raw bytes are both first-class
    rather than the single Anthropic special case this used to be.
    """
    prefix, _suffix, index = split_at_breakpoint(messages, tools, breakpoint_index)
    family = _provider_family(provider, model)
    dialect = cache_dialect_for(provider, model, api_base)
    digest = prefix_digest(messages, tools, breakpoint_index)
    schemas = tool_schema_digest(tools)
    estimate = sum(estimate_tokens(_message_text(item)) for item in prefix)
    if schemas:
        estimate += estimate_tokens(canonical_tool_schemas(tools))
    try:
        floor = max(0, int(min_prefix_tokens))
    except (TypeError, ValueError):
        floor = DEFAULT_MIN_PREFIX_TOKENS
    skip = ""
    if not enabled:
        skip = "disabled"
    elif not prefix:
        skip = "empty_prefix"
    elif not dialect.wants_breakpoints():
        # OpenAI-compatible, Google, DeepSeek and local endpoints cache a
        # stable prefix on their own terms; there is no marker to send, and
        # sending an unknown one is a hard 400 on most of them.
        #
        # The reason string stays the bare `implicit_prefix` and the dialect
        # rides alongside it on `cache_dialect`/`cache_dialect_mechanism`.
        # Making it `implicit_prefix:openai` reads better in isolation but it
        # is a rendered string another module's tests and UI already pin, and
        # the information is not lost by leaving it in its own field.
        skip = "implicit_prefix"
    elif estimate < floor:
        skip = "prefix_below_floor"
    instability = tuple(prefix_instability(messages, tools, breakpoint_index))
    # DISTINCT and SORTED, so "reported 40 times" and "reported once" read as the
    # same problem, and two runs of the same code produce the same list.
    uncanonical_reasons = tuple(
        sorted(
            {
                str(row.get("source"))
                for row in instability
                if str(row.get("source")) in _CANONICALISATION_DETAIL
            }
        )
    )
    return CachePlan(
        prefix_sha256=digest,
        tool_schema_sha256=schemas,
        breakpoint_index=index,
        prefix_tokens_estimate=estimate,
        provider_family=family,
        requested=skip == "",
        skip_reason=skip,
        dialect=dialect.dialect,
        mechanism=dialect.mechanism,
        # One message breakpoint plus the tool-catalog breakpoint. Two, which
        # is inside every cap in the table; a deferred tool search that wanted
        # a third is what the cap assertion exists to refuse.
        breakpoints=2
        if (dialect.wants_breakpoints() and tools)
        else (1 if dialect.wants_breakpoints() else 0),
        breakpoint_limit=dialect.breakpoint_limit,
        instability=instability,
        tool_surface_unstable=any(
            row.get("source") == "volatile_region" for row in instability
        ),
        uncanonical=bool(uncanonical_reasons),
        uncanonical_reasons=uncanonical_reasons,
    )


def cache_control_kwargs(
    messages: Sequence[Any],
    plan: CachePlan,
) -> Dict[str, Any]:
    """Return a shallow copy of ``messages`` with the dialect's marker attached.

    Only the LAST frozen-prefix message is annotated, so one marker covers the
    whole prefix. The marker's NAME comes from the dialect row, not from a
    literal here -- Bedrock's Converse API wants ``cachePoint`` and sending it
    ``cache_control`` fails. Message mappings are copied rather than mutated:
    the caller's list (and any conversation the harness still holds) is
    untouched.
    """
    dialect = next(
        (row for row in CACHE_DIALECTS if row.dialect == plan.dialect),
        CACHE_DIALECTS[-1],
    )
    marker = dialect.message_parameter
    if not plan.requested or plan.breakpoint_index < 0 or not marker:
        return list(messages)
    annotated: List[Any] = []
    for position, message in enumerate(messages):
        if position != plan.breakpoint_index or not isinstance(message, Mapping):
            annotated.append(message)
            continue
        updated = dict(message)
        updated[marker] = dict(dialect.parameter_value or {"type": "default"})
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
    of a stable agent prefix.

    **Phase 3b/W1: the catalog returned here is the CANONICAL form, and that
    is the fix, not a tidy-up.** The previous version passed the caller's list
    through and only tagged the last entry, while the digest hashed a
    normalised projection of it. Those are two different byte strings, and a
    provider keys on the bytes. Measured, with the same two schemas declared
    in two different orders on alternating turns:

    * ``cache_distinct_prefixes = 1``, ``cache_invalidation_count = 0``
      -- our receipt said the prefix never changed;
    * **two** provider cache creations -- the invoice disagreed.

    Now the digest hashes exactly this value, so digest equality implies byte
    equality. Everything is rebuilt rather than mutated, so the caller's list
    (and any conversation the harness still holds) is untouched.
    """
    out_messages = cache_control_kwargs(messages, plan)
    canonical = canonical_tool_schemas(tools)
    if not canonical:
        return out_messages, None
    if not plan.requested:
        return out_messages, canonical
    dialect = next(
        (row for row in CACHE_DIALECTS if row.dialect == plan.dialect),
        CACHE_DIALECTS[-1],
    )
    if not dialect.tool_parameter or not isinstance(canonical[-1], Mapping):
        return out_messages, canonical
    annotated_tools = list(canonical)
    last = dict(annotated_tools[-1])
    last[dialect.tool_parameter] = dict(dialect.parameter_value or {"type": "default"})
    annotated_tools[-1] = last
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
    * Bedrock Converse: ``cacheReadInputTokens``;
    * DeepSeek: ``prompt_cache_hit_tokens`` (with
      ``prompt_cache_miss_tokens`` as its explicit negative);
    * Gemini / Vertex: ``cachedContentTokenCount``;
    * proxies/aggregators: flat ``cached_tokens`` / ``cache_read_tokens`` /
      ``cache_hit_tokens`` aliases.

    Phase 3b/W1 added the DeepSeek, Bedrock and Gemini names. Before this, a
    perfectly working DeepSeek cache reported ``unreported`` on every single
    turn, which in a cost dashboard reads as "caching is broken here" -- and
    the honest-absent rule then keeps it out of the hit rate, so the whole
    saving was invisible rather than wrong.
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
                *EXTRA_CACHED_TOKEN_KEYS,
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
        # Phase 3b/W1: the three names this module previously did not read.
        # DeepSeek, Gemini/Vertex and Bedrock all report a working cache under
        # one of these, and reading none of them made every one of those
        # providers report `unreported` for the life of the session.
        "prompt_cache_hit_tokens",
        "cachedContentTokenCount",
        "cached_content_token_count",
        "cacheReadInputTokens",
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
        dialect=plan.dialect,
    )


#: The closed vocabulary of invalidation causes. Every value is producible by a
#: pin, and ``unknown`` is a real answer rather than filler: it means the prefix
#: moved for a reason this module cannot see, and calling that "the tool
#: surface changed" would send an operator to the wrong file.
INVALIDATION_CAUSES = (
    "tool_surface_changed",
    "clock_in_prefix",
    "volatile_region_in_prefix",
    "non_text_prefix_block",
    "unknown",
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
      shown to invalidate the cache exactly once;
    * **which** change caused it -- phase 3b/W1. A bare invalidation count is
      not actionable: an operator reading "the prefix changed 47 times" cannot
      tell a T1 tool-surface edit from a clock in their own prompt, and the
      second of those is invisible in every other report this repo produces.
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
        self._last_tool_digest: Optional[str] = None
        #: Phase 3b/W1: the CAUSE of each invalidation, counted. Keyed by a
        #: closed vocabulary so a new cause cannot be invented at a call site;
        #: ``unknown`` is a legitimate value and means "the prefix moved and
        #: nothing we measure explains it", which is worth seeing.
        self.invalidation_causes: Dict[str, int] = {}
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

        The CAUSE is attributed in this order, and the order is the point: the
        tool digest is checked before the prefix digest because a catalog
        change is a deliberate, attributable event while a prefix change on an
        unchanged catalog is the clock or a volatile region, and conflating
        the two is how a per-turn tool-surface change ends up "silently"
        disabling caching.
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
            cause = ""
            if changed:
                self.invalidations += 1
                cause = self._attribute(plan)
                self.invalidation_causes[cause] = (
                    self.invalidation_causes.get(cause, 0) + 1
                )
            self.prefixes_seen.add(plan.prefix_sha256)
            self._last_prefix = plan.prefix_sha256
            self._last_tool_digest = plan.tool_schema_sha256
            entry["invalidated"] = changed
            entry["invalidation_cause"] = cause
            if plan.instability:
                entry["cache_prefix_instability"] = [
                    dict(row) for row in plan.instability
                ]
            self.recent.append(entry)
            if len(self.recent) > self.max_recent:
                del self.recent[: len(self.recent) - self.max_recent]
        return entry

    def _attribute(self, plan: CachePlan) -> str:
        """Return the closed-vocabulary cause of one invalidation.

        ``INVALIDATION_CAUSES`` is the vocabulary and every value is
        producible; ``unknown`` is not a filler, it is the honest answer for a
        prefix that moved for a reason this module cannot see, and reporting
        it as anything else would be a guess.
        """
        if self._last_tool_digest is not None and (
            self._last_tool_digest != plan.tool_schema_sha256
        ):
            return "tool_surface_changed"
        for row in plan.instability:
            if row.get("source") == "full_timestamp":
                return "clock_in_prefix"
            if row.get("source") == "volatile_region":
                return "volatile_region_in_prefix"
            if row.get("source") == "non_text_content":
                return "non_text_prefix_block"
        return "unknown"

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
                "cache_hit_rate_window": decided,
                "cache_invalidation_count": self.invalidations,
                "cache_invalidation_causes": dict(self.invalidation_causes),
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
            self.invalidation_causes = {}
            self.prefixes_seen = set()
            self._last_prefix = None
            self._last_tool_digest = None
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
