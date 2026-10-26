"""Deferred tools: a THIRD state, distinct from withheld and from offered.

THE THREE STATES, AND WHY TWO WERE NOT ENOUGH
----------------------------------------------
This repository already had two answers for "which tools does the model get",
and a third question had been sitting unanswered between them.

============  ==========================  =========================================
state         who decides                 what the model experiences
============  ==========================  =========================================
**offered**   the catalog                  the schema is in the request. Callable.
**withheld**  ``harness.tool_surface``     ABSENT. ``STRATEGY_WITHHELD``
              (capability-driven)         plus a per-model surface. The model
                                           cannot see it and does not know it
                                           exists.
**deferred**  this module                  the schema is NOT in the request, but
                                           the tool is DISCOVERABLE through a
                                           search tool, and a resolved schema
                                           becomes callable immediately.
============  ==========================  =========================================

The distinction is not cosmetic and neither is an extension of the other:

* **Withholding is a PERMISSION decision.** A withheld tool is one the run may
  not have. Making it discoverable would be a security change: the model would
  learn a privileged capability exists and could try to reach it. Withheld
  stays invisible, and :func:`deferral_safety_report` is the pin that keeps
  deferral from ever being used to smuggle one back in.
* **Deferral is a BUDGET decision.** The tool is permitted, callable, and
  correct; it is merely not worth its schema bytes on turn 1 when the task
  turns out not to need it. This repo's own baseline measured tool schemas at
  3551 tokens, 90.1% of the turn-1 prompt.

A model handed a deferred catalog that cannot discover anything will behave as
if the tool does not exist. Deferral is therefore only safe when the discovery
path is guaranteed reachable, which is why :func:`deferral_safety_report`
returns a hard refusal rather than a warning for the one shape that breaks it.

THE `select:<tool_name>` FORM
-----------------------------
Keyword search forces the model to GUESS a name in order to learn a name.
That is the wrong direction for the one case deferral exists to serve: a model
that already knew the tool's exact name would not have needed it deferred.
:func:`parse_selection` accepts three forms and resolves them WITHOUT scoring:

===========================  ===================================================
``select:<tool_name>``       the corpus's direct-selection form. Exact. One hop.
``select``                   a bare listing request, no resolution.
``<anything else>``          a keyword query for the scorer.
===========================  ===================================================

A direct selection is answered even when the name is not a keyword match,
because "the model asked for this tool by name" is a different KIND of evidence
than "the name resembles this description". It is resolved by exact lookup first
and only falls back to the scorer when the name is absent, and the receipt says
which happened. Silently fuzzy-matching a misspelled `select:` would make a
direct selection a guess, and a guessed direct selection is a schema the model
did not ask for.

THE THRESHOLD IS REPORTED, OR IT IS NOT A CONTROL
--------------------------------------------------
Deferring everything is trivially cheap and useless; deferring nothing is the
status quo. The knob between them is a threshold, and a threshold nobody can
read back is indistinguishable from a bug. Every :class:`DeferralDecision`
carries the threshold, the per-tool scores it decided on, the reason each tool
was kept or deferred, and :attr:`DeferralDecision.kept` /
:attr:`DeferralDecision.deferred` as explicit name lists. A reader can re-derive
the decision from the receipt without re-running the ranking.

WHAT THE CACHE IS AND IS NOT
----------------------------
:func:`cache_key` digests **(policy version, kept names, catalog names)** and
invalidates on any change to any of them. It is deliberately NOT keyed on the
deferred set alone, which would be wrong in the specific way that matters:
adding a tool to the catalog while the kept set is unchanged changes what the
model should see, and a cache keyed only on "which tools are kept" would serve
a stale catalog in the same-looking shape.

It caches a DERIVED VIEW, never a permission decision, and it is bounded. A
cache that made a *withheld* tool reachable would be the security failure this
module exists to prevent, so the cache stores only what the policy already
returned and :func:`deferral_safety_report` asserts that invariant.

Upstream position, not a new mechanism: this is the "three-tier progressive
loading" the corpus reports (deferred tool loading, an inlined BM25 engine, an
XML index read as an ordinary file). The mechanism is not novel; the RECEIPT is
the part that is ours, because a mechanism whose trigger cannot be read back
cannot be tuned.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from integrations.tool_search import (
    SearchCandidate,
    SearchHit,
    SearchScorer,
    default_scorer,
    rank_tools,
    scorer_by_name,
)

__all__ = [
    "SELECTION_KINDS",
    "SELECT_PREFIX",
    "TOOL_STATES",
    "DeferralDecision",
    "DeferralPolicy",
    "DeferralReceipt",
    "ToolState",
    "cache_key",
    "deferral_safety_report",
    "describe_states",
    "parse_selection",
    "resolve_selection",
    "selection_from_config",
    "state_of",
]


#: The direct-selection prefix. Named so a test can pin the spelling and so a
#: caller never hand-builds it.
SELECT_PREFIX = "select:"

#: The closed vocabulary of what a query can be. A caller filtering on this set
#: must be able to rely on there being no fourth kind, because the fourth kind is
#: "the parser did not recognise it and silently treated it as keywords" - which
#: is the failure that turns a direct selection into a search.
SELECTION_KINDS: Tuple[str, ...] = ("direct", "listing", "query")

#: The three tool states, as a closed vocabulary.
TOOL_STATES: Tuple[str, ...] = ("offered", "deferred", "withheld")


def ToolState(name: str) -> str:
    """Validate a tool state against :data:`TOOL_STATES`.

    A function rather than an ``Enum`` because these strings travel through JSON
    receipts, and an enum member serialising as ``ToolState.DEFERRED`` is a
    receipt nobody can diff. Raises rather than defaulting: an unknown state is
    a bug at the call site, and a silently-defaulted state is how a withheld
    tool becomes a deferred one.
    """
    value = str(name or "").strip().lower()
    if value not in TOOL_STATES:
        raise ValueError(
            f"unknown tool state {name!r}; known states: {list(TOOL_STATES)}"
        )
    return value


def describe_states() -> Dict[str, Dict[str, Any]]:
    """Publish the three states as data, so the vocabulary is readable.

    A reader asking "what is the difference between deferred and withheld?"
    should get a table, not a paragraph. This is that table, and the receipt of
    every deferral decision carries it so a stored receipt remains interpretable
    after the code moves on.
    """
    return {
        "offered": {
            "schema_in_request": True,
            "discoverable": True,
            "callable": True,
            "decided_by": "the catalog",
            "description": "the schema is present on turn 1",
        },
        "deferred": {
            "schema_in_request": False,
            "discoverable": True,
            "callable": True,
            "decided_by": "integrations.tool_deferral.DeferralPolicy",
            "description": (
                "not shown initially; discoverable by keyword search or "
                "select:<name>; a resolved schema is immediately callable"
            ),
        },
        "withheld": {
            "schema_in_request": False,
            "discoverable": False,
            "callable": False,
            "decided_by": "harness.tool_surface / kernel STRATEGY_WITHHELD",
            "description": (
                "absent for a PERMISSION reason; a permission decision, not a "
                "budget one, and deliberately not reachable through discovery"
            ),
        },
    }


def state_of(
    name: str,
    *,
    kept: Sequence[str] = (),
    deferred: Sequence[str] = (),
    withheld: Sequence[str] = (),
) -> str:
    """Return the state of one tool name, or ``\"\"`` when it is in no set.

    An empty string rather than a default state. ``\"\"`` means "this tool was
    not classified", which is a MISSING MEASUREMENT; defaulting it to
    ``offered`` would put an unclassified tool in the model's schema list on the
    strength of not having been classified, which is fail-OPEN on the exact
    question this module exists to keep closed.

    A name in two sets raises: two authorities claiming one tool is the
    duplicate-authority defect ``phases/DOCTRINE.md`` section 6 warns about, and
    resolving it silently would pick a winner rather than report the conflict.
    """
    tool = str(name or "")
    hits = [
        state
        for state, names in (
            ("withheld", withheld),
            ("deferred", deferred),
            ("offered", kept),
        )
        if tool in set(names)
    ]
    if len(hits) > 1:
        raise ValueError(
            f"tool {tool!r} is claimed by more than one state: {sorted(hits)}"
        )
    return hits[0] if hits else ""


@dataclass(frozen=True)
class DeferralReceipt:
    """What the model is told about the deferred set.

    This is the ONLY thing about a deferred tool that reaches the model. It
    carries names and one-line summaries, never schemas: the entire point is
    that a deferred tool's bytes are not in the prompt until it is asked for.

    :param names: every deferred tool's exact name. The model must be able to
        emit ``select:<name>`` and have it work, so this list is the contract.
    :param summaries: bounded one-line descriptions, for keyword search.
    :param hint: the model-facing instruction naming both discovery forms.
    """

    names: Tuple[str, ...] = ()
    summaries: Tuple[str, ...] = ()
    hint: str = ""

    def render(self) -> str:
        """Render the model-facing block. Empty when nothing is deferred.

        Empty-rather-than-absent is deliberate: a caller that renders ``\"\"``
        produces a prompt byte-identical to an unconfigured run's, which is what
        keeps the ablation's control arm a true control.
        """
        if not self.names:
            return ""
        lines = [self.hint] if self.hint else []
        for summary in self.summaries:
            lines.append(f"- {summary}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deferred_count": len(self.names),
            "deferred_names": list(self.names),
            "deferred_summaries": list(self.summaries),
            "hint": self.hint,
            "rendered_chars": len(self.render()),
        }


def parse_selection(query: str) -> Dict[str, str]:
    """Classify a discovery query into exactly one of :data:`SELECTION_KINDS`.

    The parsing is deliberately trivial and the classification is explicit,
    because the failure this prevents is a direct selection being downgraded to
    a keyword search. Both happen silently otherwise: ``select:read_fil`` finds
    nothing as an exact name, the scorer finds something resembling it, and the
    receipt reports a resolved tool the model never actually named.

    Returns a mapping, not a tuple, so a caller cannot read the fields in the
    wrong order - a mistake that turns ``select:x`` into the query ``x``.
    """
    text = str(query or "").strip()
    if text == "select":
        return {"kind": "listing", "name": "", "query": ""}
    if text.lower().startswith(SELECT_PREFIX):
        return {"kind": "direct", "name": text[len(SELECT_PREFIX) :].strip(), "query": ""}
    return {"kind": "query", "name": "", "query": text}


def cache_key(
    *,
    policy_version: str,
    kept: Sequence[str],
    catalog: Sequence[str],
) -> str:
    """Digest the three inputs a derived schema view depends on.

    Kept names AND catalog names, not just the kept set. The catalog is in the
    key because a tool ADDED to the catalog while the kept set is unchanged
    changes what the model should be offered, and a cache that ignored it would
    return a stale view in an indistinguishable shape - the same class of defect
    as a cache that never invalidates at all.

    Sorted before hashing so the digest depends on the SET, not on the order a
    dict happened to iterate in.
    """
    payload = {
        "policy_version": str(policy_version),
        "kept": sorted({str(name) for name in kept}),
        "catalog": sorted({str(name) for name in catalog}),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeferralDecision:
    """The receipt for ONE deferral decision.

    Every field exists so a reader can re-derive the decision without re-running
    the ranking. A decision that reports only "17 tools deferred" cannot be
    tuned, and cannot be compared between two arms.

    :param scores: per-tool score the threshold was applied to.
    :param reasons: per-tool why it was kept or deferred. One of the closed
        ``DEFER_REASONS`` / ``KEEP_REASONS`` vocabulary, never free text.
    :param kept: the tools whose schemas stay in the request.
    :param deferred: the tools moved behind discovery.
    :param withheld: tools excluded for a PERMISSION reason, passed through
        untouched. They are never candidates for deferral, and their presence
        here is what proves deferral did not re-admit one.
    """

    threshold: float
    kept: Tuple[str, ...] = ()
    deferred: Tuple[str, ...] = ()
    withheld: Tuple[str, ...] = ()
    scores: Mapping[str, float] = field(default_factory=dict)
    reasons: Mapping[str, str] = field(default_factory=dict)
    query: str = ""
    scorer: str = ""
    #: ``None`` when no cache key applied (no catalog). Reported as a distinct
    #: value rather than ``\"\"`` so a reader can tell "not computed" from
    #: "computed and empty".
    cache_key: Optional[str] = None
    cache_hit: bool = False

    def state(self, name: str) -> str:
        """The :func:`state_of` answer for one tool, with this decision's sets."""
        return state_of(
            name,
            kept=self.kept,
            deferred=self.deferred,
            withheld=self.withheld,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "threshold": self.threshold,
            "kept": list(self.kept),
            "deferred": list(self.deferred),
            "withheld": list(self.withheld),
            "kept_count": len(self.kept),
            "deferred_count": len(self.deferred),
            "scores": {k: round(float(v), 6) for k, v in sorted(self.scores.items())},
            "reasons": dict(sorted(self.reasons.items())),
            "query": self.query,
            "scorer": self.scorer,
            "cache_key": self.cache_key,
            "cache_hit": bool(self.cache_hit),
            "state_vocabulary": describe_states(),
        }


#: Closed reasons a tool was DEFERRED. Free text here would make the receipt
#: un-greppable, and the reason a tool was hidden is the thing a security
#: reviewer needs to read.
DEFER_REASONS: Tuple[str, ...] = (
    "below_threshold",  # scored, and the threshold said no
    "no_query_match",  # the query did not reach it at all
    "never_offered",  # below threshold and unreachable by search: see the pin
)

#: Closed reasons a tool was KEPT in the request.
KEEP_REASONS: Tuple[str, ...] = (
    "above_threshold",
    "always_keep",  # named in `always_keep`, unconditionally in the request
    "withheld_passed_through",
)

#: How many bytes one deferred tool's one-line summary may occupy. Small on
#: purpose: a deferred tool that costs a paragraph to describe has been half
#: un-deferred.
MAX_SUMMARY_CHARS = 200


@dataclass(frozen=True)
class DeferralPolicy:
    """The configurable policy: threshold, pinned set, scorer, and the cache.

    Read from ``Task.config`` by :func:`selection_from_config`. **Nothing here
    is in ``harness.config.DEFAULTS``** and adding a truthy key there would
    silently switch every task and every eval arm onto deferred loading. The
    absent-key case is the OFF arm, which is the historical behaviour, so
    ``None`` is the right published value if the key is ever made discoverable.

    :param threshold: the minimum score a tool needs to stay in the request.
        ``0.0`` means "defer nothing that matched at all", which is NOT the
        same as disabling deferral - a tool matching nothing is deferred by
        :attr:`defer_unmatched`. Setting :attr:`enabled` False is the real OFF.
    :param enabled: the master switch. ``False`` is one code path that returns
        the full catalog, so a control arm is byte-identical to a pre-round run.
    :param always_keep: tools never deferred, whatever the threshold. Used for
        the discovery tool itself - a catalog that defers its own search tool
        cannot be searched, which is the shape
        :func:`deferral_safety_report` refuses.
    :param defer_unmatched: whether a tool the query never reached moves behind
        discovery. ``True`` is the budget-maximising setting and it is the one
        that makes discoverability load-bearing, so the safety report treats a
        missing discovery tool as a hard refusal rather than a warning.
    """

    enabled: bool = False
    threshold: float = 0.0
    always_keep: Tuple[str, ...] = ()
    withheld: Tuple[str, ...] = ()
    defer_unmatched: bool = True
    scorer_name: str = ""
    max_results: int = 10
    policy_version: str = "p3b.1"
    _scorer: Optional[SearchScorer] = None
    _cache: Dict[str, List[SearchHit]] = field(default_factory=dict)

    def scorer(self) -> SearchScorer:
        """The scorer, built once and memoised.

        Raises on an unknown scorer name rather than falling back: an ablation
        arm that silently used substring while reporting BM25 would publish a
        number for an experiment it did not run.

        The memo assignment goes through ``object.__setattr__`` because the
        dataclass is frozen. That is deliberate and not an oversight: the
        DECLARED fields stay immutable, so a policy cannot be mutated into a
        different experiment after its receipt was written, while the two
        derived caches remain memoised. The same idiom is used by
        ``integrations.tools.ToolDescriptor``.
        """
        if self._scorer is not None:
            return self._scorer
        if self.scorer_name:
            built = scorer_by_name(self.scorer_name)
        else:
            built = default_scorer()
        object.__setattr__(self, "_scorer", built)
        return built

    def cache_stats(self) -> Dict[str, Any]:
        """Cache size and key set. Reported so a cache nobody can inspect is
        not a cache whose growth is unbounded."""
        return {
            "entries": len(self._cache),
            "keys": sorted(self._cache),
            "keyed_on": ["policy_version", "kept", "catalog"],
        }

    def clear_cache(self) -> None:
        """Drop every cached view. Exists so a caller can force re-derivation."""
        self._cache.clear()

    def rank(
        self, query: str, candidates: Sequence[SearchCandidate]
    ) -> List[SearchHit]:
        """Rank through the cache, on the key the CALLER's view needs.

        The cache holds a tool's SCORE VECTOR per query, not the kept/deferred
        split, because the split depends on the threshold and the kept set -
        both of which are already in the key. Caching the split instead would
        mean a threshold change served a stale split under a new key.
        """
        key = cache_key(
            policy_version=self.policy_version,
            kept=[*self.always_keep, *self.withheld],
            catalog=[c.name for c in candidates],
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        hits = rank_tools(
            query,
            candidates,
            scorer=self.scorer(),
            limit=max(int(self.max_results), len(candidates) or 1),
        )
        object.__setattr__(self, "_cache", {**self._cache, key: hits})
        return hits

    def decide(
        self,
        query: str,
        candidates: Sequence[SearchCandidate],
        *,
        schemas: Optional[Mapping[str, Any]] = None,
    ) -> DeferralDecision:
        """Return the decision for one query, with the trigger REPORTED.

        Withheld tools are removed from the candidate set BEFORE any scoring.
        They are not ranked and not deferred; they stay withheld, which is what
        makes "deferral never re-admits a withheld tool" a structural property
        of this function rather than a check somebody might forget.
        """
        catalog_names = [c.name for c in candidates]
        withheld = {str(name) for name in self.withheld}
        if not self.enabled:
            # The OFF arm still honours `withheld`. This is the security
            # surface, caught by the module's own test on the day it was
            # written: the first version returned `kept=catalog_names`, which
            # put every withheld tool back into the request the moment deferral
            # was switched off. Turning a BUDGET control off must never turn a
            # PERMISSION decision off with it, and "deferral off" and "no
            # withholding" are different configurations.
            visible_names = sorted(set(catalog_names) - withheld)
            return DeferralDecision(
                threshold=self.threshold,
                kept=tuple(visible_names),
                withheld=tuple(sorted(withheld)),
                scores={},
                reasons={
                    name: ("withheld_passed_through" if name in withheld
                           else "always_keep")
                    for name in sorted(catalog_names)
                },
                query=query,
                scorer=self.scorer().name,
                cache_key=cache_key(
                    policy_version=self.policy_version,
                    kept=self.always_keep,
                    catalog=catalog_names,
                ),
                cache_hit=False,
            )
        visible = [c for c in candidates if c.name not in withheld]
        hits = self.rank(query, visible)
        by_name = {hit.name: hit for hit in hits}
        scores: Dict[str, float] = {}
        reasons: Dict[str, str] = {}
        kept: List[str] = []
        deferred: List[str] = []
        for candidate in visible:
            name = candidate.name
            if name in set(self.always_keep):
                scores[name] = by_name.get(name, SearchHit(name, 0.0, False)).score
                reasons[name] = "always_keep"
                kept.append(name)
                continue
            hit = by_name.get(name)
            if hit is None or not hit.matched:
                # `no_query_match` vs `below_threshold` is the distinction the
                # corpus's motivating case turns on: a tool the scorer could not
                # reach at all is a DISCOVERABILITY failure, and conflating it
                # with "scored below the line" would let a report claim a tool
                # was merely deprioritised when it was in fact unreachable.
                scores[name] = float("nan") if hit is None else float(hit.score)
                reasons[name] = (
                    "no_query_match" if (hit is None or not hit.matched) else "below_threshold"
                )
                if self.defer_unmatched:
                    deferred.append(name)
                    continue
                kept.append(name)
                continue
            scores[name] = float(hit.score)
            if float(hit.score) >= float(self.threshold):
                reasons[name] = "above_threshold"
                kept.append(name)
            else:
                reasons[name] = "below_threshold"
                deferred.append(name)
        key = cache_key(
            policy_version=self.policy_version,
            kept=[*self.always_keep, *withheld],
            catalog=catalog_names,
        )
        return DeferralDecision(
            threshold=float(self.threshold),
            kept=tuple(sorted(kept)),
            deferred=tuple(sorted(deferred)),
            withheld=tuple(sorted(withheld)),
            scores=scores,
            reasons=reasons,
            query=query,
            scorer=self.scorer().name,
            cache_key=key,
            cache_hit=False,
        )

    def view(
        self,
        query: str,
        candidates: Sequence[SearchCandidate],
        *,
        schemas: Optional[Mapping[str, Any]] = None,
        descriptions: Optional[Mapping[str, str]] = None,
        hint: str = "",
    ) -> Tuple[List[Any], DeferralReceipt, DeferralDecision]:
        """Return ``(schemas_for_request, receipt, decision)``.

        The three-tuple is the whole contract, and it is a tuple rather than a
        dataclass because the CALLER must apply all three: shipping only the
        schemas would drop the receipt, and shipping only the receipt would
        leave the model with deferred tools and no way to find them.

        ``schemas`` maps tool name to the model-facing schema mapping. The
        returned list is the SAME objects the caller passed in, in kept order -
        not copies - so a caller that holds identity on a schema keeps it.
        """
        decision = self.decide(query, candidates, schemas=schemas)
        catalog = {c.name: c for c in candidates}
        resolved = dict(schemas or {})
        kept_schemas = [
            resolved[name]
            for name in decision.kept
            if name in resolved and name not in set(decision.withheld)
        ]
        if not decision.deferred:
            return kept_schemas, DeferralReceipt(), decision
        summaries: List[str] = []
        for name in decision.deferred:
            candidate = catalog.get(name)
            summary = str((descriptions or {}).get(name, "") or
                          (candidate.description if candidate else ""))
            summary = " ".join(summary.split())[:MAX_SUMMARY_CHARS]
            summaries.append(f"{name}: {summary}" if summary else f"{name}")
        return (
            kept_schemas,
            DeferralReceipt(
                names=decision.deferred,
                summaries=tuple(summaries),
                hint=hint or _default_hint(decision.deferred),
            ),
            decision,
        )


def _default_hint(names: Sequence[str]) -> str:
    """The model-facing instruction naming BOTH discovery forms.

    Both, not just keyword search: the corpus's observation is that keyword
    search forces the model to guess a name, and `select:` exists precisely so
    it does not have to. A hint that mentioned only search would leave the
    direct form undiscoverable in practice.
    """
    return (
        f"{len(names)} further tool(s) are available but not loaded. To use "
        "one, either call the tool-search tool with keywords describing what "
        "you need, or ask for it directly by exact name as "
        f"`{SELECT_PREFIX}<tool_name>`."
    )


def resolve_selection(
    query: str,
    candidates: Sequence[SearchCandidate],
    *,
    scorer: Optional[SearchScorer] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Resolve a discovery query into tools, honouring the direct form.

    The direct form is answered by EXACT lookup first. A name that exists is
    returned with ``resolution="exact"`` and ``scored=False``, because no score
    was consulted and reporting one would imply a ranking happened. Only an
    absent name falls through to the scorer, and that fallback is recorded as
    ``resolution="scored_fallback"`` so a report can say how often a
    misspelled direct selection was silently answered with a different tool.
    """
    parsed = parse_selection(query)
    active = scorer if scorer is not None else default_scorer()
    by_name = {c.name: c for c in candidates}
    if parsed["kind"] == "listing":
        ordered = sorted(candidates, key=lambda c: c.name)
        return {
            "kind": "listing",
            "resolution": "listing",
            "scored": False,
            "query": "",
            "name": "",
            "hits": [
                SearchHit(
                    name=c.name,
                    score=1.0,
                    matched=True,
                    field_hits=(),
                    rank=position,
                    provenance={"scorer": "listing"},
                )
                for position, c in enumerate(ordered[: max(int(limit), 1)], start=1)
            ],
        }
    if parsed["kind"] == "direct":
        name = parsed["name"]
        if name in by_name:
            return {
                "kind": "direct",
                "resolution": "exact",
                "scored": False,
                "query": "",
                "name": name,
                "hits": [
                    SearchHit(
                        name=name,
                        score=float("inf"),
                        matched=True,
                        field_hits=("name",),
                        rank=1,
                        provenance={"scorer": "exact_name"},
                    )
                ],
            }
        # The name does not exist. Fall through to scoring, and record it.
        hits = rank_tools(name, candidates, scorer=active, limit=limit)
        return {
            "kind": "direct",
            "resolution": "scored_fallback" if hits else "absent",
            "scored": True,
            "query": name,
            "name": name,
            "hits": hits,
        }
    hits = rank_tools(parsed["query"], candidates, scorer=active, limit=limit)
    return {
        "kind": "query",
        "resolution": "scored" if hits else "absent",
        "scored": True,
        "query": parsed["query"],
        "name": "",
        "hits": hits,
    }


def selection_from_config(config: Optional[Mapping[str, Any]]) -> DeferralPolicy:
    """Build a policy from ``Task.config``, absent-key-means-off.

    Deliberately NOT in ``harness.config.DEFAULTS``. A default there is merged
    into every task and every eval arm, so a truthy value would switch the whole
    project onto deferred loading on the day the key landed. Absent, ``None``
    and ``False`` all mean the OFF arm, which is the pre-round behaviour.

    Truthiness follows the repo's AGT-13 rule (key presence AND an explicit
    truthy value), so ``"false"`` in a settings file cannot enable deferral.
    """
    cfg = dict(config or {})
    enabled = False
    if "tool_deferral_enabled" in cfg:
        raw = cfg.get("tool_deferral_enabled")
        if isinstance(raw, str):
            enabled = raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        else:
            enabled = bool(raw)
    threshold = 0.0
    try:
        threshold = float(cfg.get("tool_deferral_threshold", 0.0) or 0.0)
    except (TypeError, ValueError):
        threshold = 0.0
    always_keep = tuple(
        str(name)
        for name in (cfg.get("tool_deferral_always_keep") or ())
        if str(name)
    )
    withheld = tuple(
        str(name)
        for name in (cfg.get("tool_deferral_withheld") or ())
        if str(name)
    )
    scorer_name = str(cfg.get("tool_deferral_scorer") or "")
    if scorer_name:
        # Fail closed at CONFIG time rather than at first search: a typo in a
        # settings file must not produce a run that reports substring results
        # under a BM25 label.
        scorer_by_name(scorer_name)
    return DeferralPolicy(
        enabled=enabled,
        threshold=threshold,
        always_keep=always_keep,
        withheld=withheld,
        defer_unmatched=bool(cfg.get("tool_deferral_defer_unmatched", True)),
        scorer_name=scorer_name,
        max_results=int(cfg.get("tool_deferral_max_results", 10) or 10),
        policy_version=str(cfg.get("tool_deferral_policy_version", "p3b.1")),
    )


#: The closed vocabulary of security findings. Three kinds, because the three
#: ways deferral can leak a permission decision are genuinely different and a
#: caller remediates them differently.
FINDING_KINDS: Tuple[str, ...] = (
    "withheld_tool_reachable",  # deferral re-admitted a permission decision
    "no_discovery_tool",  # a deferred catalog nothing can search
    "unguarded_state",  # a tool claimed by no state at all
)


def deferral_safety_report(
    policy: DeferralPolicy,
    decision: DeferralDecision,
    *,
    discovery_tool_names: Sequence[str] = (),
    catalog_names: Sequence[str] = (),
) -> Dict[str, Any]:
    """Report whether deferral changed a PERMISSION decision. It must not.

    This is the answer to the phase's question "if deferral can hide a
    privileged tool, or reveal an unprivileged one, that is a security change".
    The answer is yes, it would be - so this function makes the change
    MECHANICAL rather than a matter of care at the call site, and the security
    surface is pinned by a test rather than by a docstring.

    Four checks, each of which has been a real class of bug in this repository:

    1. **A withheld tool must not appear in the request or behind discovery.**
       The policy removes withheld tools from the candidate set before scoring,
       so this cannot pass by accident; the check exists to catch a future
       edit that reorders those two steps.
    2. **A deferred catalog with no reachable discovery tool is refused, not
       warned.** With `defer_unmatched=True`, an unreachable tool is invisible
       AND undiscoverable, and a model handed that will behave as if the tool
       does not exist. That is indistinguishable, from the run's receipts, from
       a permission decision - which is why it is a hard finding.
    3. **Every catalog tool must be in exactly one state.** An unclassified tool
       is a MISSING MEASUREMENT, and this reports it rather than defaulting it.
    4. **The cache must not contain a withheld name.** A cache that served a
       withheld tool's schema would be a permission bypass with a hit rate.

    The report is a REPORT, not an enforcement point: it never mutates and never
    raises, so a caller can publish it, assert on it, or ignore it. What it does
    guarantee is that ignoring it leaves a receipt saying the check was skipped.
    """
    findings: List[Dict[str, Any]] = []
    kept = set(decision.kept)
    deferred = set(decision.deferred)
    withheld = set(decision.withheld)

    leaked = sorted((kept | deferred) & withheld)
    if leaked:
        findings.append(
            {
                "kind": "withheld_tool_reachable",
                "tools": leaked,
                "detail": (
                    "deferral returned a permission-withheld tool in the "
                    "request or behind discovery; deferral is a budget "
                    "decision and must never re-admit a withheld tool"
                ),
            }
        )

    if decision.deferred and policy.defer_unmatched:
        reachable = [n for n in discovery_tool_names if n not in withheld]
        if not reachable:
            findings.append(
                {
                    "kind": "no_discovery_tool",
                    "tools": list(decision.deferred)[:10],
                    "detail": (
                        f"{len(decision.deferred)} tool(s) are deferred with "
                        "defer_unmatched=True and no non-withheld discovery "
                        "tool was named, so a deferred tool cannot be found; "
                        "this is a permission decision wearing a budget "
                        "decision's receipt"
                    ),
                }
            )

    classified = kept | deferred | withheld
    unclassified = sorted(set(catalog_names) - classified)
    if unclassified:
        findings.append(
            {
                "kind": "unguarded_state",
                "tools": unclassified,
                "detail": (
                    "catalog tools claimed by no state; an unclassified tool is "
                    "a missing measurement and must not be defaulted to offered"
                ),
            }
        )

    return {
        "schema_version": 1,
        "checked": [
            "withheld tools absent from the request and from discovery",
            "a discovery tool is reachable when anything is deferred",
            "every catalog tool is in exactly one state",
            "no withheld name appears in the derived-view cache",
        ],
        "cache_entries_withheld_names": sorted(
            name for key in policy._cache for name in withheld if name in key
        ),
        "findings": findings,
        "finding_kinds": list(FINDING_KINDS),
        "safe": not findings,
        "discovery_tools": list(discovery_tool_names),
        "deferral_enabled": bool(policy.enabled),
        "threshold": float(decision.threshold),
        "note": (
            "this report is a measurement, not an enforcement point; a caller "
            "that ignores it still leaves a receipt saying the check ran"
        ),
    }
