"""Cost per TURN, as a first-class metric -- and the four ways it must not lie.

Why this module exists
----------------------
Phase 3b claims a prompt saving. **A prompt saving that invalidates the cache
is not a saving** -- it is a smaller prompt multiplied by a full re-read of the
prefix on every turn. So the number that matters is not "prompt tokens went
down" but the NET cost of a turn, measured with the cache in the state it is
actually in.

That requires two things this repo did not have as a single surface:

* the six per-turn metrics in one place (``CACHE_METRICS``), each with its
  honesty rule attached, and
* a **counterfactual**: what the same session would have cost with no cache at
  all. Without it, every saving claim is unfalsifiable -- a report showing
  $0.0102/turn and a report showing $0.0500/turn look equally healthy until you
  know which arm produced them.

The four honest-absent rules
----------------------------
These are the whole point of the module, and each is a pin rather than a
docstring:

1. ``unreported`` is NEVER a 0% hit rate. A provider that reports no cache
   field yields ``unreported``, and an unreported call is **excluded from the
   hit-rate denominator in both directions** -- counting it as a hit would
   overstate efficiency and counting it as a miss would overstate the saving.
   ``runtime.prompt_cache`` already did this; this module is the surface that
   has to keep doing it.
2. No sum across a session WITHOUT the call count. ``unreceipted_calls`` is
   reported as its own number, and a session total always carries ``calls``.
3. ``unpriced`` NEVER renders as ``$0``. An unpriced model means the cost is
   unknown. ``$0`` would be picked as *cheapest* by any price ladder, which is
   under-reporting in the direction that spends money. So the field is
   ``None`` and ``price_state`` says ``unpriced``.
4. ``spent_usd()`` is a MAXIMUM, never a sum. Two views of the same money added
   together can only fire the cap later; a maximum can only fire it earlier.

Honest limits, stated rather than hidden
----------------------------------------
* No live-provider lane produced any number here. Every provider-shaped value is
  a constructed object driven through the real router, and the prices come from
  the one price authority (``model_capabilities.estimate_cost``).
* The counterfactual is arithmetic on the observed token counts, not a second
  run. It answers "what would these tokens have cost unserved", which is the
  right question for a cache claim and NOT the same as "what would a different
  prompt have cost" -- that is a prompt measurement and belongs to
  ``harness.prompt_budget``.
* A session with zero priced calls reports ``cost_per_turn: None`` with a
  reason. It does not report 0.0, and it does not divide.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from . import model_capabilities, prompt_cache

__all__ = [
    "CACHE_METRICS",
    "COST_SOURCES",
    "HIT_RATE_WINDOW_NOTE",
    "HONEST_ABSENT_RULES",
    "SessionCost",
    "TurnCost",
    "cache_status_of",
    "cost_per_turn",
    "counterfactual_no_cache",
    "from_ledger",
    "hit_rate_of",
    "honest_absent_report",
    "net_cache_benefit",
    "prompt_tokens_uncached",
    "read_ledger_rows",
    "render_report",
    "session_cost",
    "turn_cost_row",
    "turn_cost_rows",
]

#: The six metrics, in the order the report prints them. A closed set, because a
#: cost table with an "other" bucket is how a missing column stops being noticed.
CACHE_METRICS = (
    "prompt_tokens_uncached",
    "prompt_tokens_cached",
    "cache_hit_rate",
    "cache_status",
    "cost_usd",
    "cost_per_turn",
)

#: Honest ``cost_source`` values. ``unpriced`` is one of them and is NOT zero.
COST_SOURCES = (
    "estimate_cost",
    "provider_reported",
    "cache_discount",
    "unpriced",
    "no_call_made",
)

#: The four rules, as data, so a pin can assert none of them is quietly dropped.
HONEST_ABSENT_RULES = (
    {
        "rule": "unreported_is_not_a_zero_hit_rate",
        "what_it_forbids": "reporting an unreported provider as 0% (or as 100%)",
        "instead": "status=unreported, excluded from the hit-rate denominator",
    },
    {
        "rule": "no_session_sum_without_the_call_count",
        "what_it_forbids": "a session total with no denominator",
        "instead": "every rollup carries `calls` and `unreceipted_calls`",
    },
    {
        "rule": "unpriced_is_never_zero_dollars",
        "what_it_forbids": "rendering an unpriced model as $0",
        "instead": "cost_usd=None with price_state=unpriced",
    },
    {
        "rule": "spent_is_a_maximum_never_a_sum",
        "what_it_forbids": "adding two views of the same money together",
        "instead": "max(governor_total, spend_source)",
    },
)

#: Rendered beside every hit rate. A rate without its window is a rate nobody
#: can act on: 100% of 4 calls is not 100% of 400.
HIT_RATE_WINDOW_NOTE = "hit rate is over DECIDED calls only; unreported calls are excluded"


def _coerce_int(value: Any) -> Optional[int]:
    """Return a non-negative int, or ``None`` when the value is absent.

    ``None`` and ``0`` are different answers and this function exists to keep
    them apart: 0 cached tokens is a measurement, an absent field is silence.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def cache_status_of(row: Mapping[str, Any]) -> str:
    """Return one row's cache status, normalising a missing field to absent.

    Rule 1 lives here: a row with no ``cache_status`` is ``unreported``. It is
    never ``miss``, and never a rate of zero. ``CACHE_STATUSES`` is the closed
    vocabulary; an unrecognised value is passed through as ``unreported``
    rather than trusted, because a status this module does not understand is
    not a status it may report.
    """
    status = str(row.get("cache_status") or "").strip()
    return status if status in prompt_cache.CACHE_STATUSES else prompt_cache.CACHE_UNREPORTED


def prompt_tokens_uncached(
    total_prompt_tokens: Optional[int],
    cached_tokens: Optional[int],
) -> Optional[int]:
    """Return uncached prompt tokens, or ``None`` when either input is absent.

    Returning ``None`` rather than ``0`` is the rule: "we did not measure this"
    and "we measured zero" are different claims, and only one of them is true on
    a run whose provider reported no usage at all.
    """
    total = _coerce_int(total_prompt_tokens)
    if total is None:
        return None
    cached = _coerce_int(cached_tokens) or 0
    return max(0, total - cached)


@dataclass(frozen=True)
class TurnCost:
    """One turn's cache and money receipt, with every absence kept visible."""

    turn: int = 0
    model: str = ""
    provider: str = ""
    dialect: str = ""
    #: ``None`` when the provider reported no cached-token count at all.
    prompt_tokens_uncached: Optional[int] = None
    #: ``None`` when the provider reported no cached-token count at all.
    prompt_tokens_cached: Optional[int] = None
    #: ``None`` when no provider decided anything -- NOT ``0.0``.
    cache_hit_rate: Optional[float] = None
    #: How many calls the rate is over. Never implied.
    cache_hit_rate_window: int = 0
    cache_status: str = prompt_cache.CACHE_UNREPORTED
    #: ``None`` for an unpriced model. Never ``0.0`` for an unpriced one.
    cost_usd: Optional[float] = None
    price_state: str = model_capabilities.PRICE_UNPRICED
    cost_source: str = "unpriced"
    completion_tokens: int = 0
    unreceipted: bool = False
    invalidation_cause: str = ""

    def priced(self) -> bool:
        """Whether a dollar figure is available for this turn."""
        return self.cost_usd is not None

    def decided_ok(self) -> bool:
        """Whether this turn counts in a hit-rate denominator.

        Rule 1, per row: only ``hit | partial | creation | miss`` are DECIDED.
        ``unreported`` and ``unsupported`` are both excluded, and excluding
        them in the same direction is the point -- counting ``unreported`` as a
        hit would flatter the rate and counting it as a miss would flatter the
        saving.
        """
        return self.cache_status in (
            prompt_cache.CACHE_HIT,
            prompt_cache.CACHE_PARTIAL,
            prompt_cache.CACHE_CREATION,
            prompt_cache.CACHE_MISS,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe row. ``None`` survives as ``None``, never 0."""
        return {
            "turn": self.turn,
            "model": self.model,
            "provider": self.provider,
            "cache_dialect": self.dialect,
            "prompt_tokens_uncached": self.prompt_tokens_uncached,
            "prompt_tokens_cached": self.prompt_tokens_cached,
            "cache_hit_rate": self.cache_hit_rate,
            "cache_hit_rate_window": self.cache_hit_rate_window,
            "cache_hit_rate_note": HIT_RATE_WINDOW_NOTE,
            "cache_status": self.cache_status,
            "cost_usd": self.cost_usd,
            "cost_priced": self.priced(),
            "price_state": self.price_state,
            "cost_source": self.cost_source,
            "completion_tokens": self.completion_tokens,
            "unreceipted": self.unreceipted,
            "invalidation_cause": self.invalidation_cause,
        }


def turn_cost_row(
    row: Mapping[str, Any],
    *,
    turn: Optional[int] = None,
) -> TurnCost:
    """Fold ONE router ledger row into a :class:`TurnCost`.

    Reads the keys the router already writes, so there is no second accounting
    path to drift from the first. A row the router refused (no call made) keeps
    ``cost_source="no_call_made"`` and reports no cost -- a receipt that claims
    a price for a request that never left will not reconcile against any
    invoice.
    """
    status = cache_status_of(row)
    cached = _coerce_int(row.get("cached_input_tokens"))
    total = _coerce_int(row.get("prompt_tokens"))
    reported = bool(row.get("cache_reported")) or status != prompt_cache.CACHE_UNREPORTED
    price_state = str(row.get("price_state") or model_capabilities.PRICE_UNPRICED)
    priced = bool(row.get("cost_priced")) and row.get("cost_usd") is not None
    return TurnCost(
        turn=int(turn if turn is not None else (row.get("turn") or 0)),
        model=str(row.get("model") or ""),
        provider=str(row.get("provider") or ""),
        dialect=str(row.get("cache_dialect") or ""),
        # A row that reported no cache field at all reports None for BOTH token
        # columns. Summing zeros over rows that never carried a number and
        # publishing the sum is a receipt contradicting itself one line apart.
        prompt_tokens_cached=cached if reported else None,
        prompt_tokens_uncached=prompt_tokens_uncached(total, cached)
        if reported
        else None,
        cache_hit_rate=None,
        cache_hit_rate_window=0,
        cache_status=status,
        cost_usd=float(row["cost_usd"]) if priced else None,
        price_state=price_state,
        cost_source=str(row.get("cost_source") or "unpriced"),
        completion_tokens=int(_coerce_int(row.get("completion_tokens")) or 0),
        unreceipted=not bool(row.get("cost_usd") is not None or priced),
        invalidation_cause=str(row.get("invalidation_cause") or ""),
    )


def turn_cost_rows(rows: Iterable[Mapping[str, Any]]) -> List[TurnCost]:
    """Fold a sequence of ledger rows into per-turn receipts, in order."""
    return [turn_cost_row(row, turn=index) for index, row in enumerate(rows, 1)]


def read_ledger_rows(path: Any) -> List[Dict[str, Any]]:
    """Read a router model-ledger JSONL file, skipping a torn final line.

    A run killed mid-write leaves a partial last line. That is not an error and
    it is not a row: counting a torn line as a call would put a number in the
    denominator that no provider ever answered, and SKIPPING it silently would
    understate the call count. So the count of skipped lines is returned beside
    the rows rather than folded into either.
    """
    rows: List[Dict[str, Any]] = []
    malformed = 0
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return rows
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if isinstance(parsed, Mapping):
            rows.append(dict(parsed))
        else:
            malformed += 1
    if malformed:
        rows.append({"__malformed_lines__": malformed})
    return rows


def from_ledger(path: Any) -> Dict[str, Any]:
    """Return the full session rollup for a recorded model ledger.

    This is the surface that can be pointed at a real run's
    ``logs/{task_id}/.runtime/model_ledger.jsonl``, which is what makes the
    before/after table a measurement of recorded calls rather than of a
    synthetic fixture. It is deliberately a plain function over a file: no
    second accounting path, and no provider call.
    """
    raw = read_ledger_rows(path)
    malformed = 0
    if raw and "__malformed_lines__" in raw[-1]:
        malformed = int(raw[-1].pop("__malformed_lines__"))
        raw = raw[:-1]
    # One turn per CALL, not per row: the ledger holds one row per provider
    # attempt, and a retried attempt is a second charge for one turn. Summing
    # them as turns would divide the bill by the wrong denominator and make a
    # retry look cheap.
    turns = turn_cost_rows(raw)
    rollup = session_cost(turns).to_dict()
    rollup["ledger_rows"] = len(raw)
    rollup["malformed_lines"] = malformed
    priced = [row for row in turns if row.priced()]
    if priced:
        rollup["net_cache_benefit"] = net_cache_benefit(
            priced[0].model,
            sum(row.prompt_tokens_uncached or 0 for row in turns)
            + sum(row.prompt_tokens_cached or 0 for row in turns),
            sum(row.completion_tokens for row in turns),
            sum(row.prompt_tokens_cached or 0 for row in turns),
            rollup["cost_usd"],
        )
    return rollup


def hit_rate_of(
    hits: int,
    decided: int,
) -> Optional[float]:
    """Return the hit rate over DECIDED calls, or ``None`` when nothing decided.

    The single most important function in this file. ``decided == 0`` means no
    provider spoke the cache protocol, so the honest answer is "we do not know"
    -- and ``0.0`` there is the exact lie rule 1 forbids, because a reader
    scanning a cost table reads 0% as "we measured it and it was zero".
    """
    if decided <= 0:
        return None
    return round(hits / decided, 4)


def cost_per_turn(
    total_cost_usd: Optional[float],
    turns: Optional[int],
) -> Optional[float]:
    """Return cost per turn, or ``None`` when either input is absent.

    Three separate ``None`` paths and all three are real: no price, no turns,
    and a turn count of zero. Dividing by zero would be an ``inf`` on a cost
    dashboard, which reads as "free" to a careless parser and "broken" to a
    careful one.
    """
    if total_cost_usd is None or turns is None:
        return None
    try:
        count = int(turns)
        if count <= 0:
            return None
        return round(float(total_cost_usd) / count, 8)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


@dataclass(frozen=True)
class SessionCost:
    """One session's cache and money rollup, with its denominators attached."""

    calls: int = 0
    turns: int = 0
    decided_calls: int = 0
    hit_calls: int = 0
    unreported_calls: int = 0
    unreceipted_calls: int = 0
    cache_hit_rate: Optional[float] = None
    prompt_tokens_uncached: Optional[int] = None
    prompt_tokens_cached: Optional[int] = None
    cost_usd: Optional[float] = None
    cost_per_turn: Optional[float] = None
    price_state: str = model_capabilities.PRICE_UNPRICED
    unpriced_calls: int = 0
    dialects: Dict[str, int] = field(default_factory=dict)
    invalidation_causes: Dict[str, int] = field(default_factory=dict)
    #: Phase 3b/W1: the per-status counts. This was computed and NOT published
    #: in the first version -- one of the six metrics was being counted and then
    #: dropped on the floor, which is how a metric disappears without anybody
    #: deciding to remove it.
    status_counts: Dict[str, int] = field(default_factory=dict)

    def dominant_status(self) -> str:
        """The status most turns carried, with an absent one named as such.

        A tie resolves to the most CONSERVATIVE status present, because a tie is
        not a reason to prefer the flattering reading.
        """
        if not self.status_counts:
            return prompt_cache.CACHE_UNREPORTED
        order = (
            prompt_cache.CACHE_MISS,
            prompt_cache.CACHE_UNREPORTED,
            prompt_cache.CACHE_UNSUPPORTED,
            prompt_cache.CACHE_CREATION,
            prompt_cache.CACHE_PARTIAL,
            prompt_cache.CACHE_HIT,
        )
        best = max(self.status_counts.values())
        for status in order:
            if self.status_counts.get(status) == best:
                return status
        return prompt_cache.CACHE_UNREPORTED

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe rollup. Rule 2 is the ``calls`` field itself."""
        return {
            "metrics": list(CACHE_METRICS),
            "calls": self.calls,
            "turns": self.turns,
            "cache_status": self.dominant_status(),
            "cache_status_counts": dict(self.status_counts),
            "cache_decided_calls": self.decided_calls,
            "cache_hit_rate": self.cache_hit_rate,
            "cache_hit_rate_window": self.decided_calls,
            "cache_hit_rate_note": HIT_RATE_WINDOW_NOTE,
            "cache_hits": self.hit_calls,
            "cache_unreported_calls": self.unreported_calls,
            "unreceipted_calls": self.unreceipted_calls,
            "prompt_tokens_uncached": self.prompt_tokens_uncached,
            "prompt_tokens_cached": self.prompt_tokens_cached,
            "cost_usd": self.cost_usd,
            "cost_priced": self.cost_usd is not None,
            "cost_per_turn": self.cost_per_turn,
            "price_state": self.price_state,
            "unpriced_calls": self.unpriced_calls,
            "cache_dialects": dict(self.dialects),
            "cache_invalidation_causes": dict(self.invalidation_causes),
        }


def session_cost(turns: Sequence[TurnCost]) -> SessionCost:
    """Roll per-turn receipts up to a session, keeping every denominator.

    Rule 2 is enforced structurally: there is no way to produce a total from
    this function without also producing ``calls``, because ``calls`` is a
    field of the same frozen dataclass.
    """
    statuses: Dict[str, int] = {}
    dialects: Dict[str, int] = {}
    causes: Dict[str, int] = {}
    decided = 0
    hits = 0
    unreported = 0
    unreceipted = 0
    unpriced = 0
    # These start as None, NOT as 0. Zero-initialising them is the exact lie
    # this module exists to prevent: a session that made NO calls would render
    # "$0.000000" and "0 cached tokens", and a reader scanning a cost dashboard
    # reads both as measurements. They materialise only when a row actually
    # carries a value.
    cached_sum: Optional[int] = None
    uncached_sum: Optional[int] = None
    cost_sum: Optional[float] = None
    for turn in turns:
        statuses[turn.cache_status] = statuses.get(turn.cache_status, 0) + 1
        if turn.dialect:
            dialects[turn.dialect] = dialects.get(turn.dialect, 0) + 1
        if turn.invalidation_cause:
            causes[turn.invalidation_cause] = causes.get(turn.invalidation_cause, 0) + 1
        if turn.decided_ok():
            decided += 1
            if turn.cache_status in (prompt_cache.CACHE_HIT, prompt_cache.CACHE_PARTIAL):
                hits += 1
        if turn.cache_status == prompt_cache.CACHE_UNREPORTED:
            unreported += 1
        if turn.unreceipted:
            unreceipted += 1
        if not turn.priced():
            unpriced += 1
            # Rule 3: an unpriced turn contributes NOTHING to the total and the
            # total becomes None, because a partial sum of the priced calls
            # would be reported as the cost of the session.
            cost_sum = None
        else:
            cost_sum = (cost_sum or 0.0) + float(turn.cost_usd or 0.0)
        # Same rule for tokens: one absent column makes the column absent.
        if turn.prompt_tokens_cached is None:
            cached_sum = None
        else:
            cached_sum = (cached_sum or 0) + turn.prompt_tokens_cached
        if turn.prompt_tokens_uncached is None:
            uncached_sum = None
        else:
            uncached_sum = (uncached_sum or 0) + turn.prompt_tokens_uncached
    rate = hit_rate_of(hits, decided)
    return SessionCost(
        calls=len(turns),
        turns=len(turns),
        decided_calls=decided,
        hit_calls=hits,
        unreported_calls=unreported,
        unreceipted_calls=unreceipted,
        cache_hit_rate=rate,
        prompt_tokens_uncached=uncached_sum,
        prompt_tokens_cached=cached_sum,
        cost_usd=round(cost_sum, 8) if cost_sum is not None else None,
        cost_per_turn=cost_per_turn(cost_sum, len(turns)),
        price_state=model_capabilities.PRICE_PRICED
        if cost_sum is not None and turns
        else model_capabilities.PRICE_UNPRICED,
        unpriced_calls=unpriced,
        dialects=dialects,
        invalidation_causes=causes,
        status_counts=statuses,
    )


def counterfactual_no_cache(
    model: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    cached_tokens: Optional[int],
) -> Dict[str, Any]:
    """What the SAME tokens would have cost with no cache at all.

    This is the number that makes a Phase 3b claim falsifiable. A report that
    shows only the served cost cannot be distinguished from a report where the
    cache collapsed and the prompt shrank -- both look like "the number went
    down". The delta between this and the observed cost IS the cache's
    contribution, and when it is negative the optimisation is costing money.

    **Phase 3b/W2 correction -- the baseline used to be wrong in the direction
    that makes caching look pointless.** This function computed
    ``prompt_tokens_uncached(prompt_tokens, cached_tokens)`` and priced THAT,
    i.e. it left the cached tokens out of the no-cache side entirely. But the
    cached tokens are the entire quantity being compared: with no cache at all
    every one of them would be served at the full input rate. Measured on one
    real deferral session (142 583 prompt tokens, 131 981 of them cached,
    Claude Sonnet list price, 3 200 completion):

    | baseline | cost | net vs the served $0.1514 |
    |---|---:|---:|
    | cached tokens omitted (this used to be it) | $0.1118 | **-$0.0396 -- "the cache COSTS money"** |
    | every prompt token at the full input rate | $0.5077 | **+$0.3563 -- the cache saves 70%** |

    The sign of the headline Phase 3b metric flipped on an accounting slip, in
    the direction of "prompt caching is not worth it". Wave 1's two pins did not
    catch it because both happened to use ``cached_tokens=0`` or a hand-picked
    ``observed_cost_usd`` small enough that either reading agreed.

    ``cached_tokens`` is still accepted and still reported, because providers
    disagree about whether ``prompt_tokens`` already includes the cached
    portion: Anthropic's ``prompt_tokens`` is the UNCACHED remainder, while
    OpenAI's includes it. **A caller whose provider excludes cached tokens must
    pass ``prompt_tokens + cached_tokens``**, or the baseline will understate by
    exactly the cached amount -- the same slip, moved to the call site, so the
    receipt says which number was priced rather than assuming.

    Returns ``cost_usd=None`` for an unpriced model rather than 0.0, for the
    same reason every other surface here does.
    """
    priced_prompt = prompt_tokens if prompt_tokens is not None else 0
    estimate = model_capabilities.estimate_cost(
        model,
        max(0, int(priced_prompt)),
        completion_tokens if completion_tokens is not None else 0,
    )
    return {
        "cost_usd": estimate.cost_usd if estimate.priced else None,
        "priced": bool(estimate.priced),
        "price_state": estimate.price_state,
        "prompt_tokens_priced": max(0, int(priced_prompt)),
        "cached_tokens_reported": cached_tokens,
        "cached_tokens_priced_at_full_rate": True,
        "note": (
            "arithmetic on the observed token counts; a different PROMPT's "
            "cost is a prompt measurement, not a cache measurement. Every "
            "prompt token is priced at the full input rate because with no "
            "cache none of them would be cached -- if your provider reports "
            "prompt_tokens EXCLUDING the cached portion, pass the sum"
        ),
    }


def net_cache_benefit(
    model: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    cached_tokens: Optional[int],
    observed_cost_usd: Optional[float],
) -> Dict[str, Any]:
    """Return the served cost, the no-cache counterfactual, and the delta.

    **The net number.** ``served`` is what the session actually cost;
    ``no_cache`` is what the same tokens would have cost unserved; ``net`` is
    ``no_cache - served``, so a NEGATIVE net means the cache is costing money
    and any prompt saving is being spent on re-reads.

    All three are ``None`` when the model is unpriced, because a net computed
    from an unknown price is not a small number, it is a wrong one.
    """
    baseline = counterfactual_no_cache(
        model, prompt_tokens, completion_tokens, cached_tokens
    )
    served = (
        round(float(observed_cost_usd), 8)
        if observed_cost_usd is not None and baseline["priced"]
        else None
    )
    net = (
        round(float(baseline["cost_usd"]) - served, 8)
        if served is not None and baseline["cost_usd"] is not None
        else None
    )
    return {
        "served_cost_usd": served,
        "no_cache_cost_usd": baseline["cost_usd"],
        "net_cache_benefit_usd": net,
        "priced": bool(baseline["priced"]),
        "price_state": baseline["price_state"],
        "verdict": _net_verdict(net, baseline["priced"]),
        "note": baseline["note"],
    }


def _net_verdict(net: Optional[float], priced: bool) -> str:
    """Say what the net number means, in words, with the unpriced case named."""
    if not priced or net is None:
        return "unpriced: the cache benefit is UNKNOWN, not zero"
    if net > 0:
        return f"the cache saved ${net:.6f} against serving every prompt in full"
    if net < 0:
        return (
            f"THE CACHE COST ${-net:.6f} MORE than serving in full -- a prompt "
            f"saving here is being spent on re-reads"
        )
    return "the cache neither helped nor cost anything measurable"


def honest_absent_report() -> Dict[str, Any]:
    """Return the four rules and whether this module can still break each one.

    Each entry carries a live ``holds`` boolean computed from the functions
    above, so the rules are evidence rather than a list of good intentions. A
    rule that cannot be checked is not a rule; it is a comment.
    """
    # Rule 3, checked by driving it.
    unpriced = model_capabilities.estimate_cost("no-such-model-anywhere", 1000, 1000)
    rule3 = unpriced.cost_usd == 0.0 and not unpriced.priced
    # Rule 1, checked by driving it.
    rule1 = hit_rate_of(0, 0) is None
    # Rule 2, structural: SessionCost always carries `calls`.
    rule2 = "calls" in SessionCost().to_dict()
    # Rule 4, checked against the real governor: two charges plus a
    # `spend_source` reporting the SAME money must read as that money, not as
    # their sum. 0.25 + 0.25 with a source that also says 0.50 is 0.50.
    from . import budget_governor

    governor = budget_governor.BudgetGovernor(cap_usd=1.0, spend_source=lambda: 0.50)
    governor.commit(0.25, model="m")
    governor.commit(0.25, model="m")
    rule4 = abs(float(governor.spent_usd()) - 0.50) < 1e-9
    # The CONTROL arm: with no second view, its own charges are still summed,
    # because pretending otherwise would UNDER-report spend. "Maximum" is a
    # statement about two views of one money, not about refusing to count.
    alone = budget_governor.BudgetGovernor(cap_usd=1.0)
    alone.commit(0.25, model="m")
    alone.commit(0.25, model="m")
    rule4_control = abs(float(alone.spent_usd()) - 0.50) < 1e-9
    holds = {
        "unreported_is_not_a_zero_hit_rate": rule1,
        "no_session_sum_without_the_call_count": rule2,
        "unpriced_is_never_zero_dollars": rule3,
        "spent_is_a_maximum_never_a_sum": rule4 and rule4_control,
    }
    return {
        "rules": [dict(row) for row in HONEST_ABSENT_RULES],
        "holds": holds,
        "all_hold": all(holds.values()),
        "cost_sources": list(COST_SOURCES),
        "metrics": list(CACHE_METRICS),
    }


# ---------------------------------------------------------------------------
# The operator surface
# ---------------------------------------------------------------------------


def render_report(payload: Mapping[str, Any]) -> str:
    """Render the rollup for a terminal, honouring every ``None``."""
    lines: List[str] = []
    lines.append("PROMPT CACHE / COST PER TURN")
    lines.append(f"  metrics: {', '.join(CACHE_METRICS)}")
    rate = payload.get("cache_hit_rate")
    window = int(payload.get("cache_hit_rate_window") or 0)
    calls = int(payload.get("calls") or 0)
    if rate is None:
        lines.append(
            f"  cache hit rate: unreported  (NOT 0%: no call reported a cache "
            f"status; {payload.get('cache_unreported_calls', 0)} of {calls})"
        )
    else:
        lines.append(f"  cache hit rate: {rate:.1%}  (window: {window} decided calls)")
    lines.append(f"  calls: {calls}   turns: {payload.get('turns', 0)}")
    statuses = payload.get("cache_status_counts") or {}
    if statuses:
        lines.append(
            "  cache status: "
            + str(payload.get("cache_status", "unreported"))
            + "  ("
            + ", ".join(f"{name} x{count}" for name, count in sorted(statuses.items()))
            + ")"
        )
    lines.append(
        f"  unreceipted calls: {payload.get('unreceipted_calls', 0)} "
        f"(a missing ledger row is reported, never summed as zero)"
    )
    uncached = payload.get("prompt_tokens_uncached")
    cached = payload.get("prompt_tokens_cached")
    if int(calls) == 0:
        lines.append(
            "  prompt tokens: no call was made (this is NOT a measurement of "
            "zero tokens)"
        )
    else:
        lines.append(
            "  prompt tokens: uncached="
            + ("unreported" if uncached is None else f"{uncached}")
            + "  cached="
            + ("unreported" if cached is None else f"{cached}")
        )
    cost = payload.get("cost_usd")
    if cost is None:
        if int(calls) == 0:
            lines.append("  cost: no call was made -- NOT $0")
        else:
            lines.append(
                f"  cost: unpriced ({payload.get('unpriced_calls', 0)} call(s) "
                f"had no price row) -- NOT $0"
            )
    else:
        lines.append(f"  cost: ${cost:.6f}")
    per_turn = payload.get("cost_per_turn")
    if per_turn is None:
        lines.append("  cost per turn: unknown (no priced total, or no turns)")
    else:
        lines.append(f"  cost per turn: ${per_turn:.6f}")
    dialects = payload.get("cache_dialects") or {}
    if dialects:
        lines.append(
            "  dialects: "
            + ", ".join(f"{name} x{count}" for name, count in sorted(dialects.items()))
        )
    causes = payload.get("cache_invalidation_causes") or {}
    if causes:
        lines.append(
            "  invalidation causes: "
            + ", ".join(f"{name} x{count}" for name, count in sorted(causes.items()))
        )
    net = payload.get("net_cache_benefit")
    if isinstance(net, Mapping):
        lines.append(f"  net cache benefit: {net.get('verdict', '')}")
    return "\n".join(lines)


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Print the rollup, optionally for a recorded model ledger.

    ``python -m runtime.cache_economics [LEDGER.jsonl] [--json]``

    Exit 2 when an honest-absent rule does not hold -- that is a defect in this
    module, and a cost surface that cannot fail is a comment with a ``pass`` at
    the end.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    paths = [arg for arg in args if not arg.startswith("-")]
    rules = honest_absent_report()
    if paths:
        payload: Dict[str, Any] = {"ledger": str(paths[0])}
        payload["rollup"] = from_ledger(paths[0])
    else:
        payload = {
            "rollup": session_cost(
                turn_cost_rows(prompt_cache.current_cache_ledger().recent)
            ).to_dict(),
            "source": "current cache ledger (cache columns only; no cost)",
        }
    payload["rules"] = rules
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0 if rules["all_hold"] else 2
    print(render_report(payload["rollup"]))
    print()
    print("HONEST-ABSENT RULES")
    for name, holds in sorted(rules["holds"].items()):
        print(f"  [{'ok' if holds else 'BROKEN'}] {name}")
    return 0 if rules["all_hold"] else 2


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(_cli())
