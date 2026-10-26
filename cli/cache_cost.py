"""What the cache and the money actually did — including what nobody knows.

Six numbers, six different ways to be wrong
-----------------------------------------
``/cost`` already renders a cache line. Measured on the tree this module was
written for, that line renders **"0% hit of 0 decided call(s)"** for a run whose
provider reported nothing about caching, because
:func:`cli.interactive.trace_cache_summary` seeds ``hit_rate`` at ``0.0`` and
only overwrites it when a call was *decided*. ``0%`` on a denominator of zero is
the most confident kind of wrong: it reads as a measured failure of the cache,
which sends a user to disable a feature that was simply never reported on.

So every field here is ``Optional`` and ``None`` means UNMEASURED, and the
renderers have a separate branch for it. The six fields the brief asks for,
each with the way it lies:

=========================  ==================================  ==================
field                      honest absent rule                  the lie it replaces
=========================  ==================================  ==================
``cached_prompt_tokens``   only when the ledger reported one    0, meaning "none"
``uncached_prompt_tokens`` total prompt known, else ``None``   0, meaning "all cached"
``hit_rate``               ``None`` while ``decided == 0``     ``0.0``
``window``                 only when a dial declares one       a made-up default
``cost_per_turn``          priced AND ``turns > 0``, else None $0.00
``dialect``                only when a call declared one       "openai" as a guess
=========================  ==================================  ==================

The sixth is the quiet one. ``CacheReceipt.dialect`` defaults to ``"openai"``,
so an undeclared dialect and a genuinely-OpenAI dialect are the same bytes. The
default is the right ANSWER for an OpenAI request and the wrong LABEL for a
provider that never spoke, so this module only renders a dialect that a call
actually declared, and says ``unreported`` otherwise.

Two numbers that are easy to confuse and must never be one
---------------------------------------------------------
* **cache hit rate** — how often the PROVIDER served a prefix it had already
  computed. A property of the cache.
* **prompt saving** — how many tokens this run did not have to send because of
  that cache. A property of the PROMPT.

Both are real, they move independently, and a receipt that reports only the
first makes a 0% hit rate look like a 0% saving — which is exactly the
inversion this module is here to prevent. :func:`prompt_saving_lines` renders
them as two rows and names the authority for each.

Deliberately NOT rebuilt here
-----------------------------
The cache-token SUM is delegated to :func:`cli.interactive.trace_cache_summary`
rather than re-derived from the journal. A second reader of ``trace.jsonl`` is
the failure class this repository records four times over, and the fix for the
``0%`` defect is to correct the CONTRACT at this layer and render it, not to
open a sixth reader of the same file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ABSENT",
    "CACHE_STATUSES",
    "COST_ABSENT",
    "DECIDED_STATUSES",
    "HIT_STATUSES",
    "CacheCostReport",
    "TurnCostPoint",
    "assert_absent_is_never_zero",
    "cache_cost_report",
    "cache_lines",
    "cost_lines",
    "cost_sparkline",
    "hit_rate_of",
    "is_priced_cost",
    "prompt_saving_lines",
    "uncached_prompt_tokens",
]

#: The one word this module renders for "we could not measure this". A
#: constant so a reader can search the whole tree for it and a test can assert
#: it appears wherever a ``None`` did.
ABSENT = "unreported"

#: Rendered for a cost this run could not price. Never ``$0.00``: a free call
#: and an unpriced call are different facts and only one of them is good news.
COST_ABSENT = "cost unknown"

#: Mirrors :data:`runtime.prompt_cache.CACHE_STATUSES`, re-exported rather than
#: re-typed: this module must not hold a second list of the vocabulary, or one
#: of the two rots and a reader cannot tell which one the run used.
try:  # pragma: no cover - import guard
    from runtime.prompt_cache import (
        CACHE_HIT,
        CACHE_MISS,
        CACHE_PARTIAL,
        CACHE_UNREPORTED,
        CACHE_UNSUPPORTED,
    )

    #: Statuses where the provider ANSWERED. Everything else is silence, and
    #: silence is not a miss.
    DECIDED_STATUSES: Tuple[str, ...] = (CACHE_HIT, CACHE_PARTIAL, CACHE_MISS)
    #: Statuses that mean "the cache served part of the prefix". A partial hit
    #: is a hit for the prefix it covered, which is why it counts.
    HIT_STATUSES: Tuple[str, ...] = (CACHE_HIT, CACHE_PARTIAL)
    CACHE_STATUSES: Tuple[str, ...] = (
        CACHE_HIT,
        CACHE_PARTIAL,
        CACHE_MISS,
        CACHE_UNSUPPORTED,
        CACHE_UNREPORTED,
    )
except Exception:  # pragma: no cover - a build without runtime
    DECIDED_STATUSES = ("hit", "partial", "miss")
    HIT_STATUSES = ("hit", "partial")
    CACHE_STATUSES = ("hit", "partial", "miss", "unsupported", "unreported")


@dataclass(frozen=True)
class CacheCostReport:
    """Six cache/cost fields, each with its own absent rule.

    Every number is ``Optional`` and every ``None`` is rendered as
    :data:`ABSENT`. A zero is a measurement; a ``None`` is an admission, and the
    two must never share a column.
    """

    label: str = "run"
    #: Whether any call reported cache fields at all. ``False`` with a reason is
    #: a different surface from ``True`` with a zero.
    available: bool = False
    reason: str = ""
    calls: int = 0
    decided: int = 0
    hits: int = 0
    #: ``None`` while ``decided == 0``. THIS is the field that was wrong.
    hit_rate: Optional[float] = None
    cached_prompt_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    total_prompt_tokens: Optional[int] = None
    #: ``None`` unless total prompt tokens were reported. Computing it as
    #: ``total - cached`` would answer "how much was NOT cached" with a number
    #: that assumes the ledger is complete.
    uncached_prompt_tokens: Optional[int] = None
    #: Provider cache window, only when something declared one.
    window_tokens: Optional[int] = None
    #: The provider's own cache dialect, only when a call declared one.
    dialect: str = ""
    #: Measured spend. ``None`` when any call was UNPRICED — an average that
    #: silently drops the unpriced calls under-reports, and the reader cannot
    #: see which calls went missing.
    cost_usd: Optional[float] = None
    priced: bool = False
    unpriced_calls: int = 0
    turns: Optional[int] = None
    #: ``None`` unless ``cost_usd`` is not ``None`` and ``turns`` is positive.
    cost_per_turn: Optional[float] = None
    #: status -> count, exactly as the ledger reported it.
    statuses: Mapping[str, int] = field(default_factory=dict)
    #: Per-turn costs for the sparkline. Empty is not zero turns.
    per_turn: Tuple["TurnCostPoint", ...] = ()

    def unmeasured(self) -> Tuple[str, ...]:
        """The names of every field this report could NOT measure."""
        out: List[str] = []
        if not self.available:
            out.append("cache")
        if self.hit_rate is None:
            out.append("hit_rate")
        if self.cached_prompt_tokens is None:
            out.append("cached_prompt_tokens")
        if self.uncached_prompt_tokens is None:
            out.append("uncached_prompt_tokens")
        if not self.dialect:
            out.append("dialect")
        if self.window_tokens is None:
            out.append("window")
        if self.cost_usd is None:
            out.append("cost")
        if self.cost_per_turn is None:
            out.append("cost_per_turn")
        return tuple(out)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "available": bool(self.available),
            "reason": self.reason,
            "calls": int(self.calls),
            "decided": int(self.decided),
            "hits": int(self.hits),
            "hit_rate": self.hit_rate,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "uncached_prompt_tokens": self.uncached_prompt_tokens,
            "window_tokens": self.window_tokens,
            "dialect": self.dialect,
            "cost_usd": self.cost_usd,
            "priced": bool(self.priced),
            "unpriced_calls": int(self.unpriced_calls),
            "turns": self.turns,
            "cost_per_turn": self.cost_per_turn,
            "statuses": dict(self.statuses),
            "unmeasured": list(self.unmeasured()),
        }


@dataclass(frozen=True)
class TurnCostPoint:
    """One turn's measured cost, or the fact that it was unpriced."""

    turn: int
    cost_usd: Optional[float] = None
    reason: str = ""

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None


def hit_rate_of(hits: int, decided: int) -> Optional[float]:
    """``hits / decided``, or ``None`` when nothing was DECIDED.

    A zero denominator has no ratio, and the ``0.0`` this replaces is the one
    that made an unreported cache look like a broken one.
    """
    try:
        decided = int(decided)
    except (TypeError, ValueError):
        return None
    if decided <= 0:
        return None
    return float(hits or 0) / float(decided)


def uncached_prompt_tokens(
    total_prompt_tokens: Optional[int],
    cached: Optional[int],
    cache_write: Optional[int] = None,
) -> Optional[int]:
    """Prompt tokens the provider actually had to process, or ``None``.

    ``None`` unless the ledger reported BOTH the total prompt tokens and the
    cached half. A ``total - cached`` subtraction against a partial ledger
    produces a confident number for a population nobody counted.
    """
    if total_prompt_tokens is None or cached is None:
        return None
    try:
        total = int(total_prompt_tokens)
        served = int(cached) + int(cache_write or 0)
    except (TypeError, ValueError):
        return None
    if total < 0 or served < 0:
        return None
    return max(0, total - served)


def is_priced_cost(cost_usd: Any, record: Optional[Mapping[str, Any]] = None) -> bool:
    """Is this cost a MEASUREMENT, or the ``0.0`` an unpriced run reports?

    Rung 10's "vacuous zero": an unpriced run writes ``cost_usd: 0.0`` and
    ``cost_known: False``, so the old ``cost_usd is not None`` test rendered
    ``$0.000000`` for work nobody priced.

    The rule, and why it is shaped this way:

    * a **positive** cost is always a measurement. A local provider can
      genuinely be free, and reporting that as unpriced would be its own lie —
      in the opposite direction, which is the one this module keeps finding;
    * a bare ``0.0`` is a measurement only when the producer SAID it priced the
      run — ``cost_known``, ``priced``, or a ``cost_source`` naming a real
      price. Nothing in this repository produces a priced-and-zero receipt, so
      the bare zero is the unpriced spelling;
    * ``None`` and a non-number are never priced.

    One function, and every caller — ``briefing_facts``, the plain card, and
    the Textual card — goes through it. Two renderers with two copies of this
    rule is the drift the finding was reported as.
    """
    try:
        value = float(cost_usd)
    except (TypeError, ValueError):
        return False
    if value > 0.0:
        return True
    data = record if isinstance(record, Mapping) else {}
    return bool(
        data.get("cost_known")
        or data.get("priced")
        or str(data.get("cost_source") or "") in {"provider", "priced", "measured"}
    )


def cache_cost_report(
    summary: Optional[Mapping[str, Any]] = None,
    *,
    label: str = "run",
    turns: Optional[int] = None,
    cost_usd: Optional[float] = None,
    unpriced_calls: int = 0,
    dialect: str = "",
    window_tokens: Optional[int] = None,
    total_prompt_tokens: Optional[int] = None,
    per_turn: Sequence[TurnCostPoint] = (),
) -> CacheCostReport:
    """Build the report from a cache SUMMARY and the run's own facts.

    ``summary`` is the dict :func:`cli.interactive.trace_cache_summary`
    produces. Delegating to it is the point: this module corrects the
    ``0.0``-on-zero contract and adds the fields the summary does not carry,
    and it does that by reading the summary — not by opening ``trace.jsonl`` a
    second time.
    """
    data: Mapping[str, Any] = summary if isinstance(summary, Mapping) else {}
    available = bool(data.get("available"))
    reason = str(data.get("reason") or "") if not available else ""
    calls = int(data.get("calls") or 0)
    decided = int(data.get("decided") or 0)
    hits = int(data.get("hits") or 0)
    statuses = {
        str(key): int(value)
        for key, value in (data.get("statuses") or {}).items()
        if str(key)
    }
    cached = data.get("cached_tokens")
    creation = data.get("creation_tokens")
    # A ledger whose every status is unreported did not MEASURE the token
    # columns either — it summed zeros over rows that never carried a number.
    # Publishing that sum as ``0`` beside ``hit rate: unreported`` is a receipt
    # contradicting itself one line apart, so the sums follow the same absent
    # rule as the rate.
    if decided > 0:
        cached_tokens: Optional[int] = (
            int(cached) if isinstance(cached, (int, float)) else None
        )
        write_tokens: Optional[int] = (
            int(creation) if isinstance(creation, (int, float)) else None
        )
    else:
        cached_tokens = None
        write_tokens = None
    priced = cost_usd is not None
    per_turn_tuple = tuple(per_turn or ())
    turn_count = turns
    if turn_count is None and per_turn_tuple:
        turn_count = len(per_turn_tuple)
    cost_per_turn: Optional[float] = None
    if priced and turn_count:
        try:
            if int(turn_count) > 0:
                cost_per_turn = float(cost_usd) / float(int(turn_count))
        except (TypeError, ValueError, ZeroDivisionError):
            cost_per_turn = None
    return CacheCostReport(
        label=str(label or "run"),
        available=available,
        reason=reason or ("" if available else "no cache fields in the ledger"),
        calls=calls,
        decided=decided,
        hits=hits,
        # THE FIX. `data["hit_rate"]` is deliberately not read: it is `0.0` on
        # a zero denominator, and a stale 0.0 from a caller is the same lie.
        hit_rate=hit_rate_of(hits, decided),
        cached_prompt_tokens=cached_tokens,
        cache_write_tokens=write_tokens,
        total_prompt_tokens=(
            int(total_prompt_tokens)
            if isinstance(total_prompt_tokens, (int, float))
            else None
        ),
        uncached_prompt_tokens=uncached_prompt_tokens(
            total_prompt_tokens if isinstance(total_prompt_tokens, (int, float)) else None,
            cached_tokens,
            write_tokens,
        ),
        window_tokens=(
            int(window_tokens) if isinstance(window_tokens, (int, float)) else None
        ),
        dialect=str(dialect or ""),
        cost_usd=float(cost_usd) if priced else None,
        priced=priced,
        unpriced_calls=int(unpriced_calls or 0),
        turns=int(turn_count) if turn_count else None,
        cost_per_turn=cost_per_turn,
        statuses=statuses,
        per_turn=per_turn_tuple,
    )


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def assert_absent_is_never_zero(report: CacheCostReport) -> None:
    """Raise if an unmeasured field is carrying a number.

    Checked at the PROJECTION layer with a control arm in both directions: it
    fires on a ``0``/``0.0`` that no measurement supports, and it passes a real
    ``0`` — which is a fact, not an absence. A run whose provider reported
    ``0`` cached tokens is a measured miss; a run that reported nothing is not.
    """
    problems: List[str] = []
    if report.decided == 0 and report.hit_rate is not None:
        problems.append(
            f"hit_rate {report.hit_rate!r} with 0 decided calls: no call was decided, "
            "so there is no ratio to report"
        )
    if report.turns is not None and int(report.turns) <= 0 and report.cost_per_turn is not None:
        problems.append(
            f"cost_per_turn {report.cost_per_turn!r} with {report.turns} turn(s)"
        )
    if report.cost_usd is None and report.cost_per_turn is not None:
        problems.append(
            f"cost_per_turn {report.cost_per_turn!r} with an unpriced run: an unpriced "
            "run has no per-turn cost"
        )
    for point in report.per_turn:
        if point.cost_usd == 0.0 and not point.reason:
            problems.append(
                f"turn {point.turn} claims $0.000000 with no reason: a free turn and an "
                "unpriced turn are different facts"
            )
    if problems:
        listing = "\n".join(f"  {item}" for item in problems)
        raise AssertionError(f"{len(problems)} unmeasured field(s) carry a number:\n{listing}")


# ---------------------------------------------------------------------------
# The per-turn sparkline
# ---------------------------------------------------------------------------

#: One character per rung, cheapest bucket last. ASCII deliberately: this
#: repository has a recorded outage from a marker a legacy console cannot
#: encode, and a cost sparkline is decoration that must not be able to take a
#: receipt down.
_SPARKLINE_RAMP = " .:-=+*#"


def cost_sparkline(
    points: Sequence[TurnCostPoint],
    *,
    width: int = 24,
    width_chars: int = 78,
) -> str:
    """Render per-turn costs as a bounded ASCII sparkline. ``""`` when empty.

    An unpriced turn is drawn as a SPACE, not a zero rung, and the receipt that
    uses it says how many were unpriced — a sparkline where the expensive turn
    looks cheap because its price is missing is a chart that lies, and a chart
    is the easiest thing in a product to be believed by eye.
    """
    rows = [point for point in (points or ())]
    if not rows:
        return ""
    priced = [p for p in rows if p.cost_usd is not None]
    if not priced:
        return " " * min(width, len(rows))
    top = max(float(p.cost_usd) for p in priced) or 1.0
    shown = rows[-max(1, int(width)) :]
    out: List[str] = []
    for point in shown:
        if point.cost_usd is None:
            out.append(" ")
            continue
        scaled = float(point.cost_usd) / top
        index = round(scaled * (len(_SPARKLINE_RAMP) - 1))
        out.append(_SPARKLINE_RAMP[max(0, min(index, len(_SPARKLINE_RAMP) - 1))])
    return "".join(out)


# ---------------------------------------------------------------------------
# The renderers — plain lines, no markup, every claim on its own row
# ---------------------------------------------------------------------------


def _int(value: Optional[int]) -> str:
    return f"{int(value):,}" if value is not None else ABSENT


def _money(value: Optional[float]) -> str:
    if value is None:
        return COST_ABSENT
    return f"${float(value):.6f}"


def cache_lines(
    report: CacheCostReport,
    *,
    label: str = "",
    width_chars: int = 78,
) -> List[str]:
    """Render the cache fields. Non-empty for EVERY report, including a bare one.

    The headline is the hit rate or the admission that there is not one. The
    two token counts follow, then the window and the dialect. Status counts are
    rendered whenever any call reported one, because ``unreported: 12`` is the
    fact that explains every ``None`` above it.
    """
    name = str(label or report.label or "run")
    lines: List[str] = []
    if not report.available:
        lines.append(
            f"{name} cache: {ABSENT} - {report.reason or 'no cache fields in the ledger'}"
        )
        # The disclaimer deliberately says "zero" and never prints the numeral
        # the renderer would otherwise print. A substring test for a confident
        # zero cannot tell a claim from a disclaimer, so the disclaimer does not
        # contain the claim.
        lines.append("  this is NOT a measured zero: no call reported a cache status")
        return lines

    if report.hit_rate is None:
        lines.append(
            f"{name} cache: {ABSENT} hit rate - {report.decided} of {report.calls} call(s) "
            "reported a cache status"
        )
    else:
        lines.append(
            f"{name} cache: {report.hit_rate * 100:.0f}% hit over "
            f"{report.decided} decided call(s) of {report.calls}"
        )
    lines.append(
        f"  cached prompt tokens: {_int(report.cached_prompt_tokens)}"
        f" · uncached prompt tokens: {_int(report.uncached_prompt_tokens)}"
    )
    lines.append(
        f"  cache-write tokens: {_int(report.cache_write_tokens)}"
        f" · window: {_int(report.window_tokens)}"
        f" · dialect: {report.dialect or ABSENT}"
    )
    if report.statuses:
        rendered = " · ".join(
            f"{status}: {count}" for status, count in sorted(report.statuses.items())
        )
        lines.append(f"  statuses: {rendered}")
    unreported = sum(
        int(count)
        for status, count in report.statuses.items()
        if status not in DECIDED_STATUSES
    )
    if unreported:
        # The sentence that turns every ``unreported`` above into an
        # explanation rather than a mystery.
        lines.append(
            f"  {unreported} call(s) reported no cache status; they are excluded from the "
            "hit rate rather than counted as misses"
        )
    return lines


def cost_lines(
    report: CacheCostReport,
    *,
    label: str = "",
    width_chars: int = 78,
) -> List[str]:
    """Render measured spend, per-turn cost, and the sparkline."""
    name = str(label or report.label or "run")
    lines: List[str] = []
    if report.cost_usd is None:
        lines.append(f"{name} cost: {COST_ABSENT}")
        if report.unpriced_calls:
            lines.append(
                f"  {report.unpriced_calls} call(s) carried no price: the total is unknown, "
                "NOT $0.000000"
            )
    else:
        lines.append(f"{name} cost: {_money(report.cost_usd)} over {report.calls} call(s)")
        if report.unpriced_calls:
            lines.append(
                f"  {report.unpriced_calls} call(s) carried no price and are NOT in that "
                "total: it is a floor, not the spend"
            )
    if report.cost_per_turn is None:
        if report.cost_usd is None:
            lines.append(f"  cost per turn: {COST_ABSENT}")
        elif not report.turns:
            lines.append("  cost per turn: " + ABSENT + " - no turn count was recorded")
        else:
            lines.append("  cost per turn: " + ABSENT)
    else:
        lines.append(
            f"  cost per turn: {_money(report.cost_per_turn)} over {report.turns} turn(s)"
        )
    spark = cost_sparkline(report.per_turn)
    if spark:
        unpriced = sum(1 for point in report.per_turn if not point.priced)
        note = "  (space = an unpriced turn, not a cheap one)" if unpriced else ""
        lines.append(f"  per turn: |{spark}|{note}")
    return lines


def prompt_saving_lines(
    report: CacheCostReport,
    *,
    saved_tokens: Optional[int] = None,
    saved_usd: Optional[float] = None,
    cache_cost_usd: Optional[float] = None,
) -> List[str]:
    """Render prompt SAVING beside the cache's own cost. Two rows, never one.

    * ``saved_*`` is the measured consequence of the cache — tokens and dollars
      this run did not send.
    * ``cache_cost_usd`` is what the cache COST (a cache write is billed at a
      premium on most providers).

    They are separate numbers because they move independently: a 0% hit rate
    with a large saving is a coherent state (an early write the next turn
    reused), and one row cannot hold both.
    """
    lines = ["prompt cache accounting"]
    lines.append(
        f"  saved by the cache: {_int(saved_tokens)} prompt token(s)"
        + (f" · {_money(saved_usd)}" if saved_usd is not None else f" · {ABSENT} in dollars")
    )
    lines.append(
        "  cache cost: "
        + (
            _money(cache_cost_usd)
            if cache_cost_usd is not None
            else f"{ABSENT} - no provider cache-write price was applied"
        )
    )
    if saved_usd is None or cache_cost_usd is None:
        lines.append(
            "  net effect: " + ABSENT + " - a saving and a cost that were not both measured "
            "cannot be netted, and netting them would invent a verdict"
        )
    else:
        net = float(saved_usd) - float(cache_cost_usd)
        verdict = "the cache paid for itself" if net > 0 else "the cache cost more than it saved"
        if net == 0:
            verdict = "the cache exactly paid for itself"
        lines.append(f"  net effect: {_money(net)} - {verdict}")
    return lines
