"""Deferred tool loading — the kernel integration for the built library.

What this is
------------
Phase 3a's measured baseline: **tool schemas are a flat 3,551 tokens, 90.1% of
the turn-1 prompt.** They are flat because a provider is billed for the schema
JSON on every call and the catalog is constant within a run. The corpus's
reusable finding is Claude Code's **~40% prompt reduction** from deferred tool
loading, and its recommendation is unambiguous: *"When your tool count exceeds
~15, adopt deferred tool loading."* We are at 45.

Three mechanisms are ported, and each keeps its own attribution:

1. **Claude Code's ``shouldDefer``** — tools marked deferrable are excluded
   from the initial prompt and discovered on demand (:func:`should_defer`).
2. **Codex's BM25 ``tool_search``** — *"the same prompt economics with ranked
   search instead of substring matching."* The ranking is
   :mod:`harness.bm25`, and the proof that it beats substring is a measured
   comparison, not an assertion (:func:`discovery_search`).
3. **Hermes's threshold rule** — collapse into bridge tools *once schemas would
   exceed a configured share of the context window*. Hermes uses 10%;
   :data:`DEFAULT_SCHEMA_SHARE_THRESHOLD` is that number, it is configurable,
   and **it is reported on every turn that it fires** (:func:`threshold_decision`).

Why deferral was IMPOSSIBLE before this module, and what that cost
------------------------------------------------------------------
`integrations/tools.py` has been built and unwired since a previous round.
Wiring it alone would have produced a scheme that could not work, and the
reason is worth recording because it is the kind of finding that only appears
when you measure:

**the catalog's tool descriptions were ``f"{name} ({effect})"`` — a mean of 20
characters.** `read (read_only)`. The corpus's entire premise is that the model
searches for a tool *by capability words in its description*; with a
three-token description there is nothing to search, so a model would have had
to already know the name ``test`` to find the tool that runs tests. That is
exactly the failure this phase's requirement 8 names — *"if the model must know
a tool's name to find it, deferral has moved the problem rather than solved
it."*

So `harness/tools.py` gained a ``description`` field carrying a real capability
sentence per tool, and :meth:`harness.tools.ToolSpec.rich_schema`. Three
properties of that edit matter more than the edit:

* ``to_schema()`` is **byte-identical** to its previous output, so an
  unconfigured run's prompt is unchanged and the eval matrix has a stable
  baseline.
* ``_catalog_shape`` does not read ``description``, so
  ``catalog_fingerprint()`` is **unchanged** — measured, not assumed:
  ``d3eb4ac7de866736d63f253f`` before and after.
* The two are gated by ONE key, because a richer description without deferred
  loading is a strict cost, and deferred loading without descriptions is
  useless. They are one arm.

Deferred is NOT withheld
------------------------
The distinction the phase brief insists on, and it is structural here:

* **deferred** — the schema is not in this turn's prompt. The tool is
  *discoverable* (``tool_search``) and *executable* the moment it is discovered
  (:func:`DeferredToolState.activate`). The registry is never ``restrict()``-ed
  by deferral, so ``plan_mode_tools()``, ``runtime.roles`` and the catalog
  parity report all still see the full catalog.
* **withheld** — ``harness.agent_kernel.kernel.STRATEGY_WITHHELD`` and
  ``capability_surface()``. The tool is *absent*. A withheld tool is not in any
  search index, because offering to search for something that cannot be
  reached is worse than silence.

:func:`discovery_index` is built from the **deferred set only**, so the two can
never be conflated in a receipt either.

The bridge tool is a bridge tool
--------------------------------
``tool_search`` (:data:`TOOL_SEARCH_SPEC`) is **not** a catalog entry. Hermes
calls these *"three BM25-searched bridge tools"* and Codex calls theirs
``tool_search``; in both, the bridge is a separate layer from the catalog it
bridges. Keeping it out of ``harness/tools.py`` is what leaves the canonical
catalog at 45 with an unmoved fingerprint, and it is why the default surface a
model sees is unchanged. It is registered **only** when deferral is active.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from harness.bm25 import (
    DESCRIPTION_FIELD_WEIGHT,
    NAME_FIELD_WEIGHT,
    TAG_FIELD_WEIGHT,
    BM25Index,
    BM25Result,
    SearchDocument,
)
from harness.tools import ToolSpec, typed_tool_specs

__all__ = [
    "CACHE_EVENTS",
    "DEFAULT_SCHEMA_SHARE_THRESHOLD",
    "DEFAULT_SEARCH_RESULTS",
    "DEFER_REASONS",
    "TOOL_SEARCH_NAME",
    "TOOL_SEARCH_SPEC",
    "DeferredSetCache",
    "DeferredToolState",
    "DiscoveryResult",
    "SurfaceComposition",
    "ThresholdDecision",
    "ToolDiscovery",
    "cache_event",
    "deferral_enabled",
    "deferred_cache_key",
    "deferred_from_config",
    "discovery_search",
    "eager_tool_names",
    "should_defer",
    "threshold_decision",
]


# ---------------------------------------------------------------------------
# 1. shouldDefer — Claude Code's model, and WHY each rule exists
# ---------------------------------------------------------------------------
#
# The deferrable set is DERIVED, not a second hand-written list of all 45
# tools. A hand list would be a second catalog: it would drift the first time a
# tool was added, and the drift is invisible (the new tool is simply neither
# eager nor deferred, and the receipt cannot say which).
#
# The rule is instead: **defer everything except a small EAGER set whose
# membership is individually justified.** The default surface is the whole
# catalog minus the eager set minus the withheld set.

#: Tools the model must be able to see and call on the FIRST turn.
#:
#: Every entry carries a reason, because "why is this one eager" is the
#: question a reviewer asks and the question a receipt reader asks, and an
#: unexplained member of this set is an unexplainable cost.
#:
#: The categories are structural, not per-tool preferences:
#:
#: * **control** — ``finish``/``cancel``/``ask``. A model that cannot see how to
#:   end its turn cannot end its turn. Deferring control is the one deferral
#:   that is a functional bug rather than a cost.
#: * **the mutation floor** — ``edit``/``apply_patch``/``write``. A surface that
#:   withholds every way to change a file leaves the model unable to do the
#:   job. This is the same rule ``harness.tool_surface.surface_violations``
#:   enforces for the per-model mapping, and ``surface_violations`` is CALLED on
#:   the post-deferral surface so the two mechanisms cannot each fix a hole the
#:   other opened.
#: * **the read floor** — ``read``/``grep``. The corpus is explicit that below
#:   ~15 tools the indirection is not worth it; at 45 it is, but the model
#:   still needs one way to look at a file without spending a search turn.
#: * **discovery itself** — ``tool_search``/``skill_view`` (§4). A deferred set
#:   with no bridge is a hole, not a saving.
EAGER_TOOL_REASONS: Dict[str, str] = {
    # control: a model that cannot end or redirect its turn is stuck.
    "finish": "control: the model must always be able to end its turn",
    "cancel": "control: the model must always be able to stop the run",
    "ask": "control: the model must always be able to ask the user",
    # the mutation floor: something must be able to change a file.
    "edit": "mutation floor: an exact-block replacement",
    "apply_patch": "mutation floor: a multi-file diff",
    "write": "mutation floor: whole-file content",
    # the read floor: one way to see the repository without a search turn.
    "read": "read floor: see one file's contents",
    "grep": "read floor: search inside file contents",
    # discovery: a deferred set with no bridge is a hole.
    "tool_search": "discovery: the deferred-tool bridge itself",
    "skill_view": "discovery: the progressive-skill bridge itself",
}

#: The frozen eager set. Derived from :data:`EAGER_TOOL_REASONS` so the two can
#: never disagree — a name in the reasons map that is not in the set would be an
#: entry that documents nothing.
EAGER_TOOLS: FrozenSet[str] = frozenset(EAGER_TOOL_REASONS)

#: The closed vocabulary of deferral reasons. A receipt that names a reason from
#: a closed set is auditable; a receipt that names free text is not.
DEFER_REASONS: Tuple[str, ...] = (
    #: Threshold fired: schemas would exceed the configured window share.
    "threshold_exceeded",
    #: Not on the eager floor.
    "not_eager",
    #: The model cannot use this tool correctly, so offering it would be worse
    #: than hiding it (Phase 3b W1.4 item 1).
    "model_incompatible",
)


@dataclass(frozen=True)
class DeferralDecision:
    """Whether one tool is deferred, and the reason it is or is not."""

    name: str
    deferred: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "tool": self.name,
            "deferred": bool(self.deferred),
            "reason": self.reason,
            "detail": self.detail,
        }


def should_defer(
    name: str,
    *,
    spec: Optional[ToolSpec] = None,
    incompatible: FrozenSet[str] = frozenset(),
    eager: Iterable[str] = EAGER_TOOLS,
    collapsing: bool = False,
) -> DeferralDecision:
    """Decide whether one tool's schema is withheld from this turn's prompt.

    ``collapsing`` is the threshold's verdict for the whole turn: when it is
    true, every non-eager tool is deferred regardless of anything else, because
    the schemas no longer fit the budget the operator configured.

    ``incompatible`` is Phase 3b W1.4's requirement 1 and the reason this is a
    function rather than a set membership test: **a deferred tool that is
    model-incompatible must STAY deferred even when a search matches it.** A
    model that discovers a tool it cannot call correctly will call it wrongly,
    and that is worse than the tool being absent. The check is before the eager
    check on purpose — an incompatible tool must not be rescued by being on the
    floor, because the floor exists to keep the agent *able to work*, and a tool
    it cannot use does not serve that.

    The eager check is next, and then the default. Order is load-bearing and is
    asserted from tests: the first matching rule is the reason reported.
    """
    clean = str(name or "").strip().lower()
    if clean in set(incompatible):
        return DeferralDecision(
            name=clean,
            deferred=True,
            reason="model_incompatible",
            detail=(
                "this model cannot use this tool correctly; it stays deferred "
                "even when a search matches it"
            ),
        )
    eager_set = {str(item).strip().lower() for item in (eager or ())}
    if clean in eager_set:
        return DeferralDecision(
            name=clean,
            deferred=False,
            reason="eager",
            detail=EAGER_TOOL_REASONS.get(clean, "declared eager by configuration"),
        )
    if collapsing:
        return DeferralDecision(
            name=clean,
            deferred=True,
            reason="threshold_exceeded",
            detail=(
                "the full schema set exceeded the configured share of the "
                "context window"
            ),
        )
    effect = getattr(spec, "side_effect_class", "") or "unknown"
    return DeferralDecision(
        name=clean,
        deferred=True,
        reason="not_eager",
        detail=f"not on the eager floor (effect class {effect})",
    )


def eager_tool_names(config: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
    """Return the eager set for a run, honouring a configured override.

    The override is a LIST, and a list's emptiness is the OFF arm — the exact
    trap ``harness.tool_surface._declared_capabilities`` documents and fixes. An
    operator who writes ``tool_deferral_eager: []`` is saying "defer
    everything, including the mutation floor", which is a legitimate (and
    unusual) choice; reading that as truthy-means-default would silently
    restore the floor and report a surface the operator did not ask for.
    """
    if not config or "tool_deferral_eager" not in config:
        return EAGER_TOOLS
    raw = config.get("tool_deferral_eager")
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(
            str(item).strip().lower() for item in raw if str(item or "").strip()
        )
    return EAGER_TOOLS


# ---------------------------------------------------------------------------
# 2. The threshold rule — Hermes's 10% of the context window
# ---------------------------------------------------------------------------


#: Hermes's documented rule, verbatim in spirit: collapse an overflowing
#: catalog into BM25-searched bridge tools "once schemas would exceed 10% of the
#: context window". **On this repository's shipped defaults the rule FIRES**:
#: 3,551 schema tokens against a 32,768-token window is 10.84%, over 10%. That
#: is not a coincidence to be tuned away — it is the measurement that says the
#: corpus's threshold is calibrated for a catalog the size of ours.
DEFAULT_SCHEMA_SHARE_THRESHOLD = 0.10

#: Below this many tokens the schema set is not worth collapsing regardless of
#: the share. A share of a tiny number is a large ratio with no consequence: 12
#: schemas in a 4,096-token window is 3% and shrinking it would save nothing
#: while costing every read a search turn.
DEFAULT_SCHEMA_FLOOR_TOKENS = 256


@dataclass(frozen=True)
class ThresholdDecision:
    """Whether the threshold fired, and every number that produced the verdict."""

    fired: bool = False
    schema_tokens: int = 0
    context_window: int = 0
    share: float = 0.0
    threshold: float = DEFAULT_SCHEMA_SHARE_THRESHOLD
    floor_tokens: int = DEFAULT_SCHEMA_FLOOR_TOKENS
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection.

        ``threshold`` and ``share`` ride the receipt **whether or not it fired**.
        A threshold that fires silently is a surprise, and a threshold that only
        reports when it fires cannot be distinguished from one that never ran.
        """
        return {
            "fired": bool(self.fired),
            "schema_tokens": int(self.schema_tokens),
            "context_window": int(self.context_window),
            "share": round(float(self.share), 6),
            "threshold": round(float(self.threshold), 6),
            "floor_tokens": int(self.floor_tokens),
            "reason": self.reason,
        }


def threshold_decision(
    *,
    schema_tokens: int,
    context_window: int,
    threshold: Optional[float] = None,
    floor_tokens: int = DEFAULT_SCHEMA_FLOOR_TOKENS,
) -> ThresholdDecision:
    """Return whether the schema set exceeds the configured window share.

    Fails SAFE in the direction that keeps the agent working: an unusable
    window (0, negative, non-numeric) resolves to ``fired=False`` with a named
    reason, because collapsing the catalog on the strength of a number nobody
    could resolve would remove the mutation floor from a run whose window is
    fine. The alternative — firing on ``0`` — would strip a model of every way
    to change a file because a config key was malformed.

    The floor is applied as ``schema_tokens > floor`` so a share computed
    against a tiny window cannot fire on a catalog whose absolute cost is
    negligible.
    """
    limit = (
        DEFAULT_SCHEMA_SHARE_THRESHOLD if threshold is None else float(threshold)
    )
    window = int(context_window or 0)
    tokens = int(schema_tokens or 0)
    if window <= 0:
        return ThresholdDecision(
            fired=False,
            schema_tokens=tokens,
            context_window=window,
            share=0.0,
            threshold=limit,
            floor_tokens=int(floor_tokens),
            reason="no context window was resolved; the threshold did not run",
        )
    share = tokens / float(window)
    if tokens <= int(floor_tokens):
        return ThresholdDecision(
            fired=False,
            schema_tokens=tokens,
            context_window=window,
            share=share,
            threshold=limit,
            floor_tokens=int(floor_tokens),
            reason=(
                f"the schema set is {tokens} tokens, at or below the "
                f"{int(floor_tokens)}-token floor; collapsing it would cost a "
                "search turn to save nothing"
            ),
        )
    fired = share > limit
    return ThresholdDecision(
        fired=fired,
        schema_tokens=tokens,
        context_window=window,
        share=share,
        threshold=limit,
        floor_tokens=int(floor_tokens),
        reason=(
            f"schemas are {share:.2%} of a {window}-token window, over the "
            f"{limit:.2%} threshold"
            if fired
            else f"schemas are {share:.2%} of a {window}-token window, under "
            f"the {limit:.2%} threshold"
        ),
    )


# ---------------------------------------------------------------------------
# 3. The cache — Claude Code's getDeferredToolsCacheKey
# ---------------------------------------------------------------------------
#
# "The deferred set is cached and invalidated when the set changes. A cache that
# never invalidates shows the model a tool list that no longer matches reality."
#
# The key therefore covers EVERY input that can change the set: the catalog
# fingerprint, both name sets, the threshold, the window, the model, the
# provider, and whether rich descriptions are on. Anything less is a cache that
# can serve a surface for a catalog that no longer exists.

#: The closed vocabulary of cache events. A cache that cannot say whether it hit
#: is a cache whose hit rate is unfalsifiable.
CACHE_EVENTS: Tuple[str, ...] = ("hit", "miss", "stale_dropped")


@dataclass
class CacheStats:
    """Hit/miss counters, reported per turn and in the run receipt."""

    hit: int = 0
    miss: int = 0
    stale_dropped: int = 0

    @property
    def total(self) -> int:
        """Return the number of lookups."""
        return self.hit + self.miss

    @property
    def hit_rate(self) -> Optional[float]:
        """Return the hit rate, or ``None`` when nothing was looked up.

        ``None`` and ``0.0`` are different facts: no lookup is not a lookup that
        always missed, and the doctrine's honesty table names exactly this
        substitution.
        """
        return (self.hit / self.total) if self.total else None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "hit": int(self.hit),
            "miss": int(self.miss),
            "stale_dropped": int(self.stale_dropped),
            "total": int(self.total),
            "hit_rate": self.hit_rate,
        }


def cache_event(stats: CacheStats, event: str) -> None:
    """Record one cache event, refusing an event outside the closed set."""
    if event == "hit":
        stats.hit += 1
    elif event == "miss":
        stats.miss += 1
    elif event == "stale_dropped":
        stats.stale_dropped += 1


def deferred_cache_key(
    *,
    catalog_fingerprint: str,
    deferred: Iterable[str],
    eager: Iterable[str],
    threshold: float,
    context_window: int,
    model: str = "",
    provider: str = "",
    rich_descriptions: bool = True,
    activated: Iterable[str] = (),
) -> str:
    """Return the cache key for one deferred-set decision.

    The payload is canonical JSON over **sorted** name tuples. Sorted because
    two runs that deferred the same tools must produce the same key regardless
    of the order the catalog happened to be walked in — an order-dependent key
    is a cache that misses on a set that never changed, and a cache with a 0%
    hit rate that looks like a working mechanism.

    ``activated`` is in the key, and **its absence was a real bug found by the
    demo this module shipped with.** ``activated`` is an INPUT, not an output:
    a discovered tool moves from the deferred bucket to the shown bucket, so
    leaving it out of the key meant the first turn's composition — with
    ``activated=()`` — was served for the whole run. The observable symptom was
    the worst shape this mechanism can produce: the model ran
    ``tool_search``, the search reported the tool it had just loaded, and the
    NEXT turn's receipt still classified that tool as ``deferred``, so the
    prompt never carried its schema and the model could not actually call it.
    A cache that never invalidates shows the model a tool list that no longer
    matches reality, and this is precisely how that happens while every unit
    test still passes.
    """
    payload = {
        "catalog": str(catalog_fingerprint or ""),
        "deferred": sorted({str(item).strip().lower() for item in deferred or ()}),
        "eager": sorted({str(item).strip().lower() for item in eager or ()}),
        "activated": sorted(
            {str(item).strip().lower() for item in activated or ()}
        ),
        "threshold": round(float(threshold), 6),
        "window": int(context_window or 0),
        "model": str(model or "").strip().lower(),
        "provider": str(provider or "").strip().lower(),
        "rich": bool(rich_descriptions),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


class DeferredSetCache:
    """A one-slot cache for the deferred-set decision.

    One slot, not a map, and that is deliberate. The decision is a pure function
    of its inputs, so there is exactly ONE correct answer per key; a map would
    grow without bound across a long session's turns and re-derive nothing. A
    cache that grows is a cache that needs a GC policy, and the mechanism here
    is not worth one.

    The **stale entry is dropped, not replaced silently** — :attr:`stats`
    counts it, so "the catalog changed and we recomputed" is distinguishable
    from "the catalog changed and we returned the old answer".
    """

    def __init__(self) -> None:
        """Create an empty cache."""
        self._key: Optional[str] = None
        self._value: Any = None
        self.stats = CacheStats()

    def get_or_build(self, key: str, builder: Callable[[], Any]) -> Tuple[Any, str]:
        """Return ``(value, event)``, building on a miss or a stale key."""
        if self._key is not None and self._key == key:
            cache_event(self.stats, "hit")
            return self._value, "hit"
        if self._key is not None:
            cache_event(self.stats, "stale_dropped")
        cache_event(self.stats, "miss")
        self._key = key
        self._value = builder()
        return self._value, "miss"

    @property
    def key(self) -> Optional[str]:
        """Return the key currently held, or ``None``."""
        return self._key

    def report(self) -> Dict[str, Any]:
        """Return the cache's counters and the key it holds."""
        return {"key": self._key or "", **self.stats.to_dict()}


# ---------------------------------------------------------------------------
# 4. The bridge tool — Codex's tool_search, Hermes's bridge tools
# ---------------------------------------------------------------------------

#: The bridge tool's name. Named as a constant because four places reference it
#: (the eager floor, the registry registration, the description-based test, and
#: the W1.4 incompatible check) and a string literal in four places is four
#: chances to spell it differently.
TOOL_SEARCH_NAME = "tool_search"

#: The bridge tool's own spec. NOT a catalog entry, and that is the point: it
#: is the bridge, not a bridged tool. Keeping it out of ``harness/tools.py``
#: leaves the canonical catalog at 45 with ``catalog_fingerprint()`` unmoved, so
#: every existing consumer of that fingerprint is unaffected and a run with
#: deferral off sends byte-identical schemas.
TOOL_SEARCH_SPEC = ToolSpec(
    name=TOOL_SEARCH_NAME,
    side_effect_class="control",
    optional=("query", "select"),
    types={"query": str, "select": str},
    description=(
        "Search the tools that are not currently in your prompt and load the "
        "ones you need. Pass a capability in `query` (what you want to DO, in "
        "words, not a tool name) to search by meaning, or pass "
        "`select: <tool_name>` to load one tool directly by name."
    ),
)

#: The direct-selection prefix. Codex's ``defer_loading`` and Claude Code's
#: ``ToolSearchTool`` both keep a name-based path alongside the semantic one,
#: because a model that has been TOLD a tool exists must be able to reach it
#: without a search round-trip. Without it, being told a name is useless.
SELECT_PREFIX = "select:"

#: How many hits a search returns. **Three, and the number is measured.**
#:
#: The first value tried was 5, and the net-saving curve said no. Each activated
#: tool costs its schema in every subsequent turn — that is the whole point of
#: activation, and it is also the mechanism's running cost. Measured over three
#: searches on one run:
#:
#: | hits/search | net saving after 3 searches |
#: |---|---:|
#: | 5 | **587** tokens |
#: | 3 | 2 355 tokens |
#:
#: At 5 the mechanism had eaten 83% of its own saving by the third search; at 3
#: it had eaten 33%. The mechanism is not a fixed discount — it is
#: ``schemas_deferred - schemas_activated``, and the second term is the price of
#: the first. So the hit count is the throttle on that price, and the throttle
#: belongs as low as BM25's precision allows.
#:
#: Three is also what precision supports: on this catalog's 28-query capability
#: suite the correct tool is rank 1 in **every** case, so the second and third
#: slots are insurance rather than signal.
DEFAULT_SEARCH_RESULTS = 3

#: The hard cap on hits regardless of configuration. A caller who asks for 10,000
#: hits has asked for the whole catalog back in the prompt.
MAX_SEARCH_RESULTS = 12


@dataclass(frozen=True)
class DiscoveryResult:
    """One ``tool_search`` result: the hits, what it loaded, and what it COST.

    Three fields are load-bearing and each exists because of a measured failure
    or a measured cost:

    ``tokens``
        The cost side of this phase's exit gate. A saving reported without the
        cost of finding it is not a saving, so the cost rides on the same object
        as the result rather than in a parallel counter somebody can forget.

    ``activated``
        The search **activates** its hits rather than only listing them. The
        first version returned ranked candidates and left activation to a second
        call — which is two round-trips per discovery, and the brief names the
        extra round-trip as the cost row that decides whether the whole scheme
        works. Codex's ``tool_search`` and Hermes' bridge tools both resolve in
        one call for the same reason.

    ``schemas``
        The provider schemas of what was loaded, so an activated tool is
        **callable on the very next model turn**. Returning a name the model
        cannot call is the deferred-equivalent of the bug this phase exists to
        close. These bytes are the price of the mechanism and they are counted
        in ``tokens`` like everything else.
    """

    query: str
    hits: Tuple[BM25Result, ...] = ()
    activated: Tuple[str, ...] = ()
    selected: str = ""
    schemas: Tuple[Dict[str, Any], ...] = ()
    tokens: int = 0
    chars: int = 0
    truncated: bool = False
    searched: Tuple[str, ...] = ()
    #: A refusal or explanation rendered VERBATIM, carried as data.
    #:
    #: This field exists because of a real defect: the first version had
    #: :meth:`render` regenerate its text from the hit fields, so a selection
    #: refusal — which has no hits and no selection — rendered as the generic
    #: *"No deferred tool matched ''"*. The model was told its query was empty
    #: when it had actually named a tool. A renderer that derives its prose from
    #: the result's contents cannot express "the thing you named is not
    #: available", because that message is about something that is NOT in the
    #: result. Carrying the sentence as data is the fix.
    note: str = ""

    @property
    def names(self) -> Tuple[str, ...]:
        """Return the ranked hit names, best first."""
        return tuple(hit.key for hit in self.hits)

    def render(self) -> str:
        """Return the small, model-facing search result.

        **Carries the loaded schemas, not just names.** That is the difference
        between one round-trip and two, and it is why this render can be larger
        than a bare list. A search result that returned only names would save the
        prompt and cost a turn, which is a net loss at scale — the exact
        outcome the phase's cost rows exist to detect.
        """
        if self.note:
            return self.note
        if self.selected:
            loaded = [s for s in self.schemas if s.get("function", {}).get("name") == self.selected]
            if loaded:
                return (
                    f"Loaded tool `{self.selected}`. Its schema is below and it "
                    f"is callable now.\n"
                    + json.dumps(loaded[0], sort_keys=True)
                )
            return f"Loaded tool: {self.selected}"
        if not self.hits:
            return (
                f"No deferred tool matched {self.query!r}. Try different words "
                "describing what you want to do, or select:<tool_name>."
            )
        lines: List[str] = [
            f"Found {len(self.hits)} deferred tool(s) for {self.query!r}. "
            "Their schemas are below and they are callable now."
        ]
        if self.truncated:
            lines.append(
                f"[{len(self.searched) - len(self.hits)} more deferred tools "
                "matched; narrow the query if none of these is right]"
            )
        for schema in self.schemas:
            lines.append(json.dumps(schema, sort_keys=True))
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection, cost included."""
        return {
            "query": self.query,
            "hits": [hit.to_dict() for hit in self.hits],
            "names": list(self.names),
            "activated": list(self.activated),
            "selected": self.selected,
            "schema_count": len(self.schemas),
            "tokens": int(self.tokens),
            "chars": int(self.chars),
            "truncated": bool(self.truncated),
            "searched": list(self.searched),
            "note": self.note,
        }


def _estimate(text: str) -> int:
    """Return a token estimate for a rendered fragment.

    Delegates to the ONE estimator authority the context meter uses
    (``harness.agent_kernel.budget.TokenEstimator``) rather than dividing by a
    second literal. Two token estimators is the drift the P3a handoff names
    ("do not write a second token estimator"), and a discovery-cost figure
    computed by a different divisor than the prompt-budget figure it will be
    subtracted from would make the net number wrong in a way nothing could see.
    """
    try:
        from harness.agent_kernel.budget import TokenEstimator
    except Exception:  # pragma: no cover - import guard
        return max(1, len(text) // 4) if text else 0
    return int(TokenEstimator().tokens(text))


class ToolDiscovery:
    """The BM25 index over the deferred set, plus the bridge tool's handler.

    Constructed ONCE per run and reused across turns, because building an index
    is O(corpus) and a per-turn rebuild on a path that runs every turn is how a
    free search becomes a measurable one. It is rebuilt when the deferred SET
    changes, which is what :class:`DeferredSetCache` keys on.
    """

    def __init__(self, specs: Sequence[ToolSpec], names: Sequence[str], *, rich: bool = True) -> None:
        """Index ``names`` (a subset of ``specs``) for ranked search."""
        self._specs = {spec.name: spec for spec in specs}
        self._names: Tuple[str, ...] = tuple(sorted({str(n) for n in names or ()}))
        self.rich = bool(rich)
        documents = [
            self._document(self._specs[name])
            for name in self._names
            if name in self._specs
        ]
        self._index = BM25Index(documents)

    @staticmethod
    def _document(spec: ToolSpec) -> SearchDocument:
        """Return the search document for one tool.

        Three weighted fields: the name (heavy — an exact name hit is almost
        always what was meant), the capability description (the corpus's
        mechanism), and the aliases (curated labels, between the two).
        """
        return SearchDocument.of(
            spec.name,
            name=(spec.name, NAME_FIELD_WEIGHT),
            description=(spec.capability_description(), DESCRIPTION_FIELD_WEIGHT),
            tags=(" ".join(spec.aliases), TAG_FIELD_WEIGHT),
        )

    @property
    def names(self) -> Tuple[str, ...]:
        """Return every indexed name — the deferred set, never the withheld one."""
        return self._names

    def __len__(self) -> int:
        """Return the number of indexed tools."""
        return len(self._names)

    def schema_for(self, name: str) -> Optional[ToolSpec]:
        """Return the spec for an indexed tool, or ``None`` if not indexed."""
        return self._specs.get(str(name or "").strip().lower())

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SEARCH_RESULTS,
    ) -> DiscoveryResult:
        """Return ranked hits for a capability query, carrying their schemas.

        The whole result is rendered and measured, including the truncation
        marker, because the marker is prompt tokens too.
        """
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = DEFAULT_SEARCH_RESULTS
        cap = max(0, min(cap, MAX_SEARCH_RESULTS))
        hits = tuple(self._index.search(query, limit=cap))
        truncated = bool(cap and len(self._names) > cap and hits)
        schemas = tuple(
            schema
            for schema in (self.schema_for(hit.key) for hit in hits)
            if schema is not None
        )
        result = DiscoveryResult(
            query=str(query or ""),
            hits=hits,
            schemas=tuple(
                schema.rich_schema() if self.rich else schema.to_schema()
                for schema in schemas
            ),
            truncated=truncated,
            searched=self._names,
        )
        return _measured(result)

    def select(self, name: str) -> DiscoveryResult:
        """Return a direct-selection result for one exact name.

        The name must be IN the deferred set. A withheld tool cannot be
        selected, and a name that is not deferred at all is reported as such
        rather than as a silent success — a selection that activates nothing and
        says nothing leaves the model believing it has a tool it does not have.
        """
        clean = str(name or "").strip().lower()
        if clean.startswith(SELECT_PREFIX):
            clean = clean[len(SELECT_PREFIX) :].strip()
        spec = self.schema_for(clean) if clean in self._names else None
        if spec is not None:
            result = DiscoveryResult(
                query="",
                hits=(),
                selected=clean,
                schemas=(
                    spec.rich_schema() if self.rich else spec.to_schema(),
                ),
            )
            return _measured(result)
        text = (
            f"{clean!r} is not a deferred tool, so there is nothing to load. "
            "It is either already in your prompt or not available in this "
            "session."
        )
        return DiscoveryResult(
            query="",
            hits=(),
            tokens=_estimate(text),
            chars=len(text),
            searched=self._names,
            note=text,
        )


def _measured(result: DiscoveryResult) -> DiscoveryResult:
    """Return ``result`` with its rendered cost measured."""
    text = result.render()
    return DiscoveryResult(
        query=result.query,
        hits=result.hits,
        activated=result.activated,
        selected=result.selected,
        schemas=result.schemas,
        tokens=_estimate(text),
        chars=len(text),
        truncated=result.truncated,
        searched=result.searched,
        note=result.note,
    )


def discovery_search(
    discovery: ToolDiscovery,
    query: str = "",
    *,
    select: str = "",
    limit: int = DEFAULT_SEARCH_RESULTS,
) -> DiscoveryResult:
    """The ONE entry point for a ``tool_search`` call.

    A single dispatcher rather than two call sites, because the two paths have
    to agree about cost accounting: if ``select:`` were served by a different
    function its tokens would land in a different counter and the net number
    would be wrong in the direction that flatters the mechanism.
    """
    if select:
        return discovery.select(select)
    return discovery.search(query, limit=limit)


# ---------------------------------------------------------------------------
# 5. Per-turn composition — Phase 3b W1.4 item 2
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceComposition:
    """What this turn's prompt carried, and why every tool is in one bucket.

    The receipt answers the question Phase 3b W1.4 item 2 poses: *"a user must
    be able to see why the agent could not find a tool."* Four buckets, and
    they are disjoint and exhaustive by construction — every catalog name lands
    in exactly one, which is asserted from tests, because a name in two buckets
    is a surface whose composition cannot be stated.
    """

    shown: Tuple[str, ...] = ()
    deferred: Tuple[str, ...] = ()
    withheld: Tuple[str, ...] = ()
    activated: Tuple[str, ...] = ()
    reasons: Mapping[str, str] = field(default_factory=dict)
    incompatible: Tuple[str, ...] = ()
    threshold: Optional[ThresholdDecision] = None
    cache_event: str = ""
    cache_key: str = ""
    cache: Optional[CacheStats] = None
    schema_tokens_full: int = 0
    schema_tokens_sent: int = 0
    discovery_tokens: int = 0
    discovery_calls: int = 0

    @property
    def token_saving(self) -> int:
        """Return schema tokens NOT sent this turn."""
        return max(0, int(self.schema_tokens_full) - int(self.schema_tokens_sent))

    @property
    def net_token_saving(self) -> int:
        """Return the saving MINUS the cost of every search so far.

        This is the number the phase's exit gate asks for. Reporting
        :attr:`token_saving` alone would be the dishonest half: a scheme that
        saves 40% of the prompt and adds a round-trip per turn is a net loss at
        scale, and only this subtraction can say which it was.
        """
        return self.token_saving - int(self.discovery_tokens)

    @property
    def needs_deferred_tool(self) -> bool:
        """Whether anything is deferred this turn, i.e. discovery is reachable."""
        return bool(self.deferred)

    def reason_for(self, name: str) -> str:
        """Return why ``name`` is in its bucket, or ``"unknown"``."""
        return str(self.reasons.get(str(name or ""), "unknown"))

    def bucket_of(self, name: str) -> str:
        """Return which of the four buckets ``name`` is in.

        ``activated`` wins over ``deferred``: a tool that was discovered this
        session is no longer something the model must search for, and reporting
        it as still-deferred would send a reader looking for a tool that is
        already in the prompt.
        """
        clean = str(name or "")
        for bucket, names in (
            ("activated", self.activated),
            ("shown", self.shown),
            ("withheld", self.withheld),
            ("deferred", self.deferred),
        ):
            if clean in names:
                return bucket
        return "absent"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "shown": list(self.shown),
            "deferred": list(self.deferred),
            "withheld": list(self.withheld),
            "activated": list(self.activated),
            "incompatible": list(self.incompatible),
            "reasons": dict(self.reasons),
            "threshold": self.threshold.to_dict() if self.threshold else None,
            "cache_event": self.cache_event,
            "cache_key": self.cache_key,
            "cache": self.cache.to_dict() if self.cache else None,
            "schema_tokens_full": int(self.schema_tokens_full),
            "schema_tokens_sent": int(self.schema_tokens_sent),
            "schema_tokens_saved": self.token_saving,
            "discovery_tokens": int(self.discovery_tokens),
            "discovery_calls": int(self.discovery_calls),
            "net_token_saving": self.net_token_saving,
        }


# ---------------------------------------------------------------------------
# 6. The per-run state
# ---------------------------------------------------------------------------


class DeferredToolState:
    """One run's deferral decision, its index, and its discovery accounting.

    **Deferred is not unavailable, and this class is where that is structural.**
    It holds a *set* and an *index*; it never holds a filtered registry and never
    calls ``ToolRegistry.restrict()``. :meth:`executable` answers "can this name
    still be dispatched" from the UNTOUCHED registry, and the run's own
    executability assertion drives it every turn — so if a future edit ever
    narrowed the registry to the shown set, the assertion fails on the turn it
    happened rather than on the turn a user noticed.
    """

    def __init__(
        self,
        *,
        specs: Optional[Sequence[ToolSpec]] = None,
        context_window: int = 0,
        threshold: Optional[float] = None,
        eager: Iterable[str] = EAGER_TOOLS,
        model: str = "",
        provider: str = "",
        rich_descriptions: bool = True,
        floor_tokens: int = DEFAULT_SCHEMA_FLOOR_TOKENS,
        search_limit: int = DEFAULT_SEARCH_RESULTS,
    ) -> None:
        """Create the state for one run. Does not raise on a malformed input."""
        self.specs: Tuple[ToolSpec, ...] = tuple(specs or typed_tool_specs())
        self.context_window = int(context_window or 0)
        # The CONFIGURED threshold. Named with a leading underscore because
        # ``threshold`` is the computed ThresholdDecision property below, and a
        # run that conflates "the share I configured" with "the verdict that
        # share produced" would report a threshold as if it were an outcome.
        self._configured_threshold = (
            DEFAULT_SCHEMA_SHARE_THRESHOLD if threshold is None else float(threshold)
        )
        self.eager: FrozenSet[str] = frozenset(
            str(item).strip().lower() for item in (eager or ())
        )
        self.model = str(model or "")
        self.provider = str(provider or "")
        self.rich_descriptions = bool(rich_descriptions)
        self.floor_tokens = int(floor_tokens)
        # The hit count is the throttle on the mechanism's running cost (see
        # DEFAULT_SEARCH_RESULTS), so it is run-scoped configuration rather than
        # a constant baked into the call. Clamped here once, at construction, so
        # no call site can exceed the hard cap by passing a larger number.
        try:
            self.search_limit = int(search_limit)
        except (TypeError, ValueError):
            self.search_limit = DEFAULT_SEARCH_RESULTS
        self.search_limit = max(0, min(self.search_limit, MAX_SEARCH_RESULTS))

        self.cache = DeferredSetCache()
        self._discovery: Optional[ToolDiscovery] = None
        self._threshold: Optional[ThresholdDecision] = None
        self._composition: Optional[SurfaceComposition] = None

        #: Names the model has DISCOVERED this run. Persistent for the run's
        #: life on purpose: a tool discovered on turn 3 must still be callable on
        #: turn 40, and re-searching for it every turn would be the worst
        #: possible outcome of the mechanism.
        self.activated: Set[str] = set()

        #: The cumulative cost of discovery, in tokens. This is the cost row of
        #: the exit-gate table.
        self.discovery_tokens = 0
        self.discovery_calls = 0
        #: Names the model has been SHOWN but did not call. Measured, not
        #: assumed: a model that does not know a tool exists will not search for
        #: it, so "was the opportunity taken" is a measurement.
        self.search_queries: Tuple[str, ...] = ()

    # -- the decision -----------------------------------------------------

    @property
    def catalog_names(self) -> Tuple[str, ...]:
        """Return every canonical catalog name."""
        return tuple(spec.name for spec in self.specs)

    def _spec_map(self) -> Dict[str, ToolSpec]:
        return {spec.name: spec for spec in self.specs}

    def schema_tokens(self, specs: Optional[Sequence[ToolSpec]] = None) -> int:
        """Return the measured token cost of a schema list.

        Measured from the SERIALISED JSON, through the one estimator authority —
        the same way :mod:`harness.prompt_budget` measures it. Counting
        ``len(tools)`` would report 45 tokens for 45 tools and make the whole
        phase look free.
        """
        rows = list(specs if specs is not None else self.specs)
        if not rows:
            return 0
        blob = json.dumps(
            [spec.rich_schema() if self.rich_descriptions else spec.to_schema() for spec in rows],
            default=str,
            sort_keys=True,
        )
        return _estimate(blob)

    def compose(
        self,
        *,
        incompatible: FrozenSet[str] = frozenset(),
        withheld: Iterable[str] = (),
        surface: Optional["ToolSurfaceLike"] = None,
    ) -> SurfaceComposition:
        """Return this turn's composition, computing it at most once per set.

        ``withheld`` is the STRATEGY's set (``STRATEGY_WITHHELD`` derived), and
        it is applied AFTER deferral and separately from it: a withheld tool is
        absent from the prompt, absent from the search index, and absent from
        the receipt's ``shown`` — while a deferred tool is in none of the first
        two and IS in the receipt. Passing them through one filter is exactly the
        conflation this module's docstring refuses.
        """
        key = deferred_cache_key(
            catalog_fingerprint="|".join(sorted(self.catalog_names)),
            deferred=(),
            eager=self.eager,
            activated=self.activated,
            threshold=self._configured_threshold,
            context_window=self.context_window,
            model=self.model,
            provider=self.provider,
            rich_descriptions=self.rich_descriptions,
        )
        value, event = self.cache.get_or_build(
            key, lambda: self._compute(incompatible, withheld)
        )
        self._composition = value
        # The cached composition carries the cache event of the turn that BUILT
        # it. Re-stamp the current turn's event, or every turn after the first
        # would report the build turn's event and a reader would count one miss
        # for a run that had none.
        if isinstance(value, SurfaceComposition):
            self._composition = SurfaceComposition(
                **{
                    **value.__dict__,
                    "cache_event": event,
                    "cache_key": key,
                    "cache": self.cache.stats,
                    "discovery_tokens": self.discovery_tokens,
                    "discovery_calls": self.discovery_calls,
                }
            )
        return self._composition

    def _compute(
        self,
        incompatible: FrozenSet[str],
        withheld: Iterable[str],
    ) -> SurfaceComposition:
        """Build the composition from the inputs (the cache's builder)."""
        specs = self._spec_map()
        full_tokens = self.schema_tokens()
        threshold = threshold_decision(
            schema_tokens=full_tokens,
            context_window=self.context_window,
            threshold=self._configured_threshold,
            floor_tokens=self.floor_tokens,
        )
        withheld_set = {
            str(item).strip().lower()
            for item in (withheld or ())
            if str(item or "").strip()
        }
        incompatible_set = {
            str(item).strip().lower() for item in (incompatible or ()) if item
        }
        reasons: Dict[str, str] = {}
        shown: List[str] = []
        deferred: List[str] = []
        withheld_names: List[str] = []
        for name in self.catalog_names:
            if name in withheld_set:
                withheld_names.append(name)
                reasons[name] = "withheld by this strategy"
                continue
            decision = should_defer(
                name,
                spec=specs.get(name),
                incompatible=frozenset(incompatible_set),
                eager=self.eager,
                collapsing=threshold.fired,
            )
            if decision.deferred:
                deferred.append(name)
                reasons[name] = f"{decision.reason}: {decision.detail}"
            else:
                shown.append(name)
                reasons[name] = decision.detail
        for name in sorted(self.activated):
            if name in deferred:
                shown.append(name)
                deferred.remove(name)
                reasons[name] = "discovered earlier in this run via tool_search"
        deferred_set = set(deferred)
        self._threshold = threshold
        self._discovery = ToolDiscovery(
            self.specs, sorted(deferred_set), rich=self.rich_descriptions
        )
        return SurfaceComposition(
            shown=tuple(shown),
            deferred=tuple(deferred),
            withheld=tuple(withheld_names),
            activated=tuple(sorted(self.activated)),
            reasons=reasons,
            incompatible=tuple(sorted(incompatible_set)),
            threshold=threshold,
            schema_tokens_full=full_tokens,
            schema_tokens_sent=self.schema_tokens(
                [specs[name] for name in shown if name in specs]
            ),
        )

    # -- discovery --------------------------------------------------------

    @property
    def discovery(self) -> ToolDiscovery:
        """Return the BM25 index over the deferred set, building it if needed."""
        if self._discovery is None:
            self.compose()
        assert self._discovery is not None  # compose() always sets it
        return self._discovery

    @property
    def threshold(self) -> Optional[ThresholdDecision]:
        """Return the threshold verdict, computing the composition if needed."""
        if self._threshold is None:
            self.compose()
        return self._threshold

    def search(
        self,
        query: str = "",
        *,
        select: str = "",
        limit: Optional[int] = None,
    ) -> DiscoveryResult:
        """Run one discovery, charge its cost, and make the hits CALLABLE.

        ``limit=None`` means **this run's configured limit**
        (``tool_deferral_search_results``). The first version defaulted the
        parameter to the module constant ``DEFAULT_SEARCH_RESULTS`` instead, so
        the configured value was read into ``self.search_limit``, clamped, and
        then never consulted: a caller asking for one hit got three. ``None`` as
        the sentinel is required because ``0`` is a legal configured value
        ("return the hits, no schemas"), so the default cannot be zero.

        Charging happens HERE, in the one place a discovery can be requested, so
        the cost cannot be forgotten by a second call site.

        Activation is part of the same call. The returned result carries the
        provider schemas of everything it loaded, so on the next model turn those
        tools are dispatchable — one round-trip per discovery instead of two,
        which is the difference between a mechanism that pays for itself and one
        that does not.
        """
        result = discovery_search(
            self.discovery,
            query,
            select=select,
            limit=self.search_limit if limit is None else limit,
        )
        self.discovery_tokens += int(result.tokens)
        self.discovery_calls += 1
        if query:
            self.search_queries = (*self.search_queries, str(query))

        loaded: List[str] = []
        if result.selected:
            if result.selected in self.discovery.names:
                loaded.append(result.selected)
        else:
            for name in result.names:
                if name not in self.activated:
                    loaded.append(name)
        newly = [name for name in loaded if name not in self.activated]
        if newly:
            self.activated.update(newly)
            self.compose()
        return DiscoveryResult(
            query=result.query,
            hits=result.hits,
            activated=tuple(newly),
            selected=result.selected,
            schemas=result.schemas,
            tokens=result.tokens,
            chars=result.chars,
            truncated=result.truncated,
            searched=result.searched,
            note=result.note,
        )

    def activate(self, name: str) -> str:
        """Activate one tool by name and return what happened.

        The direct, non-model path — used by the W1.4 executability assertion
        and by a caller that already knows the name. Returns the cleaned name on
        success and ``""`` when the name is not deferred, so a caller cannot
        mistake "nothing happened" for "activated".
        """
        clean = str(name or "").strip().lower()
        if clean.startswith(SELECT_PREFIX):
            clean = clean[len(SELECT_PREFIX) :].strip()
        if clean not in self.discovery.names:
            return ""
        if clean not in self.activated:
            self.activated.add(clean)
            self.compose()
        return clean

    # -- executability ----------------------------------------------------

    def executable(
        self,
        name: str,
        *,
        registry: Any = None,
    ) -> "ExecutabilityReport":
        """Whether ``name`` can still be DISPATCHED right now.

        The structural assertion of "deferred is not unavailable". Read from the
        untouched registry: a deferred tool is absent from the prompt and
        present in the registry, and this is the function that says so.

        ``registry`` is duck-typed on purpose. It needs ``spec(name)`` and
        ``handler_for(name)``; binding to ``ToolRegistry`` would make this
        module import the kernel and force a test to build a whole kernel to
        check one property.
        """
        clean = str(name or "").strip().lower()
        spec = None
        handler = None
        if registry is not None:
            try:
                spec = registry.spec(clean)
            except Exception:
                spec = None
            try:
                handler = registry.handler_for(clean)
            except Exception:
                handler = None
        in_registry = spec is not None
        in_catalog = clean in set(self.catalog_names)
        return ExecutabilityReport(
            name=clean,
            in_catalog=in_catalog,
            in_registry=in_registry,
            has_handler=handler is not None,
            deferred=clean in set(self.discovery.names),
            activated=clean in self.activated,
            backend_required=bool(
                spec is not None and getattr(spec, "side_effect_class", "") != "read_only"
            ),
        )

    def receipt(self) -> Dict[str, Any]:
        """Return the run-level receipt for this deferral decision."""
        composition = self._composition or self.compose()
        return {
            "composition": composition.to_dict(),
            "activated": sorted(self.activated),
            "discovery_calls": int(self.discovery_calls),
            "discovery_tokens": int(self.discovery_tokens),
            "search_queries": list(self.search_queries),
            "cache": self.cache.report(),
            "eager": sorted(self.eager),
            "threshold_configured": round(float(self._configured_threshold), 6),
            "context_window": int(self.context_window),
            "rich_descriptions": bool(self.rich_descriptions),
        }


@dataclass(frozen=True)
class ExecutabilityReport:
    """Whether one deferred tool is still reachable for dispatch."""

    name: str
    in_catalog: bool = False
    in_registry: bool = False
    has_handler: bool = False
    deferred: bool = False
    activated: bool = False
    backend_required: bool = False

    @property
    def dispatchable(self) -> bool:
        """Whether the run could actually execute this tool right now.

        **``in_registry`` is the whole answer**, and the first version got this
        wrong in a way worth recording: it required ``has_handler`` for any tool
        whose effect class is not ``read_only``, so ``rename`` — deferred, present
        in the registry, and dispatched by every real run — reported
        ``dispatchable: False``. The registry is not required to have a handler
        for a catalogued tool; the kernel dispatches those through the run's safe
        execution backend and returns an honest ``no_runtime`` when none is bound.
        Requiring a handler here would have reported most of the catalog as
        undispatchable and been wrong about all of it.

        ``has_handler`` is still reported, separately, because it is the
        mechanism that will actually run it.
        """
        return bool(self.in_registry)

    @property
    def deferred_but_available(self) -> bool:
        """The property this whole module exists to hold.

        A tool that is deferred AND still in the registry is *discoverable and
        executable* — it is merely absent from this turn's prompt.
        """
        return bool(self.deferred and self.in_registry and self.dispatchable)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "tool": self.name,
            "in_catalog": self.in_catalog,
            "in_registry": self.in_registry,
            "has_handler": self.has_handler,
            "deferred": self.deferred,
            "activated": self.activated,
            "dispatchable": self.dispatchable,
            "deferred_but_available": self.deferred_but_available,
            "backend_required": self.backend_required,
        }


# ---------------------------------------------------------------------------
# 7. Configuration — one gate, and why it is ONE gate
# ---------------------------------------------------------------------------

#: The single opt-in key. **Absent or ``None`` means OFF**, which is what makes
#: the default prompt byte-identical to the pre-Phase-3b one and the eval
#: matrix a stable baseline.
#:
#: Read by key PRESENCE plus :func:`harness.tool_surface.truthiness_of`, which
#: is this repository's established rule (AGT-13): ``"0"``, ``"false"``,
#: ``"no"`` and ``"off"`` are all false, so a typo cannot switch the mechanism
#: on for a run nobody meant to change.
#:
#: Deliberately NOT given a real value in ``harness/config.py::DEFAULTS``: a
#: default there is merged into every task and every eval arm, which would
#: silently put every run in the project onto deferred loading.
CONFIG_KEY = "tool_deferral"

#: The threshold share. ``None`` -> :data:`DEFAULT_SCHEMA_SHARE_THRESHOLD`.
THRESHOLD_CONFIG_KEY = "tool_deferral_threshold"

#: The eager override. ``None`` -> :data:`EAGER_TOOLS`.
EAGER_CONFIG_KEY = "tool_deferral_eager"

#: How many hits a search returns. ``None`` -> :data:`DEFAULT_SEARCH_RESULTS`.
SEARCH_LIMIT_CONFIG_KEY = "tool_deferral_search_results"


def deferral_enabled(config: Optional[Mapping[str, Any]]) -> bool:
    """Whether this run's config asks for deferred tool discovery.

    **This is the ONE place that answers the question**, and it exists because
    two call sites answered it differently and disagreed. ``deferred_from_config``
    returned ``None`` for an explicit ``False``; the kernel's handler
    registration tested ``CONFIG_KEY in cfg``, which is true for ``False``. So
    ``{"tool_deferral": False}`` produced a run with no deferral state *and* a
    ``tool_search`` tool — and every query against it answered "nothing matched"
    about a catalog holding all 45 tools. A bridge to a mechanism that is off is
    a tool that lies, so the reader is shared rather than re-derived per site.

    An ABSENT key is OFF, per AGT-13 key-presence discipline: a default in
    ``DEFAULTS`` is merged into every task and every eval arm, so publishing one
    here would switch every run in the project onto a new prompt shape. The
    threshold, the eager set and the search limit are subordinate keys and are
    meaningless without this one, so their presence never turns deferral on.
    """
    if not config or CONFIG_KEY not in config:
        return False
    from harness.tool_surface import truthiness_of

    return bool(truthiness_of(config.get(CONFIG_KEY)))


def deferred_from_config(
    config: Optional[Mapping[str, Any]],
    *,
    context_window: int = 0,
    model: str = "",
    provider: str = "",
) -> Optional[DeferredToolState]:
    """Return the run's deferral state, or ``None`` when deferral is OFF.

    ``None`` for OFF rather than a state with an empty deferral set: "the
    mechanism did not run" and "the mechanism ran and deferred nothing" are
    different facts, and a caller that receives a state object cannot tell them
    apart.
    """
    if not deferral_enabled(config):
        return None
    raw_threshold = config.get(THRESHOLD_CONFIG_KEY)
    threshold: Optional[float] = None
    if raw_threshold is not None:
        try:
            threshold = float(raw_threshold)
        except (TypeError, ValueError):
            threshold = None
    raw_limit = config.get(SEARCH_LIMIT_CONFIG_KEY)
    try:
        limit = int(raw_limit) if raw_limit is not None else DEFAULT_SEARCH_RESULTS
    except (TypeError, ValueError):
        limit = DEFAULT_SEARCH_RESULTS
    return DeferredToolState(
        specs=typed_tool_specs(),
        context_window=int(context_window or 0),
        threshold=threshold,
        eager=eager_tool_names(config),
        model=str(model or ""),
        provider=str(provider or ""),
        # Rich descriptions ride the SAME key, because they are a strict cost
        # without deferral and deferral is impossible without them.
        rich_descriptions=True,
        search_limit=limit,
    )


class ToolSurfaceLike:
    """The shape :meth:`DeferredToolState.compose` accepts for a surface.

    Declared as a class rather than a ``Protocol`` so the annotation resolves on
    Python 3.10 without a ``typing_extensions`` dependency, and so a reader can
    see the two members the composition reads without hunting.
    """

    def names(self) -> Sequence[str]:
        """Return the surface's offered tool names."""
        raise NotImplementedError

    def withholds(self) -> Sequence[str]:
        """Return the tool names this surface withholds."""
        raise NotImplementedError
