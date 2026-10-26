"""P1/W2 T3 -- the honest cost of the doubled turn ceiling.

T1 raised ``agent_max_turns`` from 25 to >=50. On its own that is a capability
change, not a cost change, because ``budget_cap_usd`` fires first (the ordering
is pinned in ``runtime/test_latency_pins.py``). But "the cap fires first" is an
argument about a *worst case*, and the brief for this wave is right that a
worst-case number alone is not a cost statement:

* **A worst case is per-tier, and the spread is enormous.** The project spec
  flagged roughly **300x** cost variation across models for comparable accuracy.
  Quoting one number without its tier describes no reachable configuration.
* **A ceiling nobody reaches is not a cost.** What a real task spends is a
  different number from what the ceiling permits, and only the first one appears
  on an invoice.

So this module publishes BOTH, per tier, and refuses to publish a single
"worst case" number at all.

Three sources, kept separate because they answer different questions:

===================== =========================================================
``worst_case_cost``   what the CEILING permits. Priced through
                      :func:`runtime.model_capabilities.estimate_cost` -- the
                      one price authority -- so the cap report and the cost
                      report cannot disagree about a model. Token shape is a
                      DELIBERATELY GENEROUS constant, and the receipt says so.
``typical_case_cost`` what a REAL task spent. Measured from this machine's own
                      accumulated ablation runs (below), never modelled. n and
                      the population are recorded, because a "typical" with no
                      sample size is a guess with a decimal point.
``preflight_estimate`` what THIS configuration would permit. The surface T4's
                      ``doctor`` renders BEFORE a run, so the number is on the
                      screen before the money is spent.
===================== =========================================================

The measured typical case, and it is the headline:

=========================================  ==========  ==========  ==========
population                                 n tasks    median      p95         max
=========================================  ==========  ==========  ==========
real ablation tasks (v4 + ir2-final arms)   50         **$0.0102** $0.0425     $0.0543
=========================================  ==========  ==========  ==========

against a $2.00 cap and a worst case of $3.15. **A real task spends about
0.5% of the cap, and the measured worst observed task spent 2.7% of it.** The
ceiling went up 2x and the bill did not move, because 50 turns is a permission,
not a plan.

Run ``python -m runtime.turn_cost`` for the table, ``--json`` for the receipt.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import model_capabilities

__all__ = [
    "DEFAULT_BUDGET_CAP_USD",
    "MEASURED_TYPICAL",
    "TURN_CEILING_NEW",
    "TURN_CEILING_OLD",
    "WorstCaseShape",
    "preflight_estimate",
    "tier_estimate",
    "turn_cost_report",
    "typical_case_cost",
    "worst_case_cost",
]

#: The ceiling T1 moved from, and the one it moved to. Both are named so a
#: receipt can say which change it is describing -- "the ceiling" alone is
#: ambiguous the moment there have been two of them.
TURN_CEILING_OLD = 25
TURN_CEILING_NEW = 50

#: The shipped default in ``harness/config.py``. Duplicated as a literal on
#: purpose rather than imported: ``harness`` is another owner's module, and a
#: cost report that silently followed a changed default would stop describing
#: the configuration a reader is running. ``assert_default_budget_cap`` is the
#: pin that notices when the two drift.
DEFAULT_BUDGET_CAP_USD = 2.0

#: The token shape a WORST case assumes per turn. Generous, not measured: the
#: point of a worst case is the largest request the harness will actually
#: build, and a real agent turn in this project runs a prompt of a few thousand
#: tokens with a few hundred of completion. 17 000/800 is roughly the shape of
#: the largest prompts the kernel assembles after several compaction cycles,
#: and it is stated in the receipt so nobody reads the number as a typical one.
DEFAULT_WORST_SHAPE: Dict[str, int] = {
    "prompt_tokens": 17_000,
    "completion_tokens": 800,
}


#: How the shipped tier table maps onto the price table. Read from
#: ``runtime.config.DEFAULT_MODEL_TIERS`` at call time rather than copied, so
#: a tier change cannot leave this report describing a model nothing calls.
def _default_tiers() -> Dict[str, str]:
    from . import config as runtime_config

    out: Dict[str, str] = {}
    for tier, spec in (
        getattr(runtime_config, "DEFAULT_MODEL_TIERS", None) or {}
    ).items():
        if isinstance(spec, Mapping) and spec.get("model"):
            out[str(tier)] = str(spec["model"])
    return out


#: The measured typical case, from this machine's own accumulated runs.
#:
#: SOURCE: the JSONL call ledgers under
#: ``logs/ablations/{v4,ir2-final}/tasklogs/*/model_ledger.jsonl`` -- 502 real
#: provider calls across 50 real tasks, real endpoints, real responses. These
#: are NOT the mock lane and NOT a cost estimate: each row carries the cost the
#: router priced from the provider's reported usage.
#:
#: Recorded as a literal for the same reason
#: ``runtime.startup_audit.FIRST_PARTY_IMPORT_MS`` is: a "typical case" that
#: has to be recomputed before it can be quoted is a typical case nobody will
#: quote. ``measured_typical_from_logs`` re-derives it from the same ledgers and
#: the pin asserts the two agree when the logs are present.
MEASURED_TYPICAL: Dict[str, Any] = {
    "source": "logs/ablations/{v4,ir2-final}/tasklogs/*/model_ledger.jsonl",
    "calls": 502,
    "tasks": 50,
    "cost_usd": {
        "median": 0.0102,
        "mean": 0.0159,
        "p95": 0.0425,
        "max": 0.0543,
        "total": 0.7925,
    },
    "calls_per_task": {"median": 9.0, "p95": 18.0, "max": 22.0, "min": 4.0},
    "tokens_per_call": {
        "prompt": {"median": 1314, "p95": 2847, "max": 4276},
        "completion": {"median": 223, "p95": 3852, "max": 16023},
    },
    "caveats": (
        "n=50 tasks on fixture + synthesized bug classes, free-tier BYO routers "
        "priced at published comparable-model rates, one machine, one evening "
        "each. It is a DIRECTIONAL typical case for this project's task shape, "
        "not a benchmark-grade cost for arbitrary repositories, and it is "
        "reported alongside the source so a reader can weigh it."
    ),
}


@dataclass(frozen=True)
class WorstCaseShape:
    """The per-turn token shape a worst case assumes, and where it came from.

    A shape is data, not policy, so it is named and reported. A worst case whose
    assumptions are invisible is a worst case nobody can argue with, which is
    the same failure as a cost report with no price state.
    """

    prompt_tokens: int = DEFAULT_WORST_SHAPE["prompt_tokens"]
    completion_tokens: int = DEFAULT_WORST_SHAPE["completion_tokens"]
    origin: str = "deliberately_generous"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "origin": self.origin,
        }


def worst_case_cost(
    model: str,
    turns: int = TURN_CEILING_NEW,
    *,
    shape: Optional[WorstCaseShape] = None,
    cached_input_tokens: int = 0,
) -> Dict[str, Any]:
    """What ``turns`` full turns at ``model`` would cost, priced by the authority.

    Returns a receipt that always carries ``price_state``: an unpriced model
    yields ``cost_usd=0.0`` with ``priced: false``, which is the one rule this
    module refuses to break. A worst case reported as ``$0`` for a model with no
    price row would be the most expensive number in the file.

    The multiplication is done on the PER-TURN estimate rather than by summing
    ``turns`` estimates, because per-turn cost is linear in tokens and one
    rounding cannot accumulate 50 times.
    """
    shape = shape or WorstCaseShape()
    estimate = model_capabilities.estimate_cost(
        model,
        shape.prompt_tokens,
        shape.completion_tokens,
        cached_input_tokens=int(cached_input_tokens),
    )
    per_turn = float(estimate.cost_usd)
    return {
        "model": model,
        "turns": int(turns),
        "per_turn_usd": round(per_turn, 10),
        "cost_usd": round(per_turn * int(turns), 10),
        "price_state": estimate.price_state,
        "priced": bool(estimate.priced),
        "cost_source": estimate.source,
        "shape": shape.to_dict(),
    }


def typical_case_cost(
    *,
    turns: Optional[int] = None,
    turns_fraction: float = 1.0,
    measured: Optional[Mapping[str, Any]] = None,
    model: str = "",
    shape: Optional[WorstCaseShape] = None,
) -> Dict[str, Any]:
    """What a REAL task spends, from the measured population.

    Three ways to ask, and they are not interchangeable:

    * ``turns=`` -- model a real task that ran exactly that many turns. Priced
      through the authority, so it is comparable with :func:`worst_case_cost`.
    * ``turns_fraction=`` -- take that fraction of :data:`TURN_CEILING_NEW`,
      e.g. ``0.18`` for the measured median of 9 calls against a 50-turn
      ceiling. This is the one to use when the question is "what does the
      ceiling mean in practice".
    * neither -- return the measured population's own median/p95/max, which is
      the honest answer and involves no modelling at all.

    Every path records ``origin`` so a reader can tell a measurement from a
    model, and the ``measured`` block always carries its own ``n``.
    """
    measured = dict(measured or MEASURED_TYPICAL)
    costs = dict(measured.get("cost_usd") or {})
    if turns is None and turns_fraction >= 1.0:
        return {
            "origin": "measured",
            "model": model or None,
            "cost_usd": costs.get("median"),
            "measured": costs,
            "sample": {
                "tasks": measured.get("tasks"),
                "calls": measured.get("calls"),
                "source": measured.get("source"),
            },
            "calls_per_task": dict(measured.get("calls_per_task") or {}),
            "caveats": measured.get("caveats"),
        }
    if turns is None:
        # `turns_fraction` is a fraction of the CURRENT ceiling, nothing else.
        # The first draft of this line multiplied by `ceiling / ceiling`, which
        # is 1.0 -- so the argument it documented had no effect on the answer.
        # A parameter that silently does nothing is worse than no parameter,
        # because a caller who set it would believe they had asked a question.
        # `round()` on a float with no ndigits already returns an int in
        # Python 3, so the `int(...)` the first draft wrapped it in was a
        # second conversion that could only ever agree or raise.
        turns = max(1, round(float(TURN_CEILING_NEW) * float(turns_fraction)))
    modelled = worst_case_cost(model or _frontier_model(), turns, shape=shape)
    # The measured median is the honest number; the modelled figure exists so a
    # reader can see what the SAME turn count would cost at frontier prices.
    median_calls = float((measured.get("calls_per_task") or {}).get("median") or 0.0)
    measured_rate = (
        float(costs.get("median") or 0.0) / median_calls if median_calls else None
    )
    return {
        "origin": "modelled_from_measured_calls",
        "model": modelled["model"],
        "turns": modelled["turns"],
        "cost_usd": round(measured_rate * int(turns), 6) if measured_rate else None,
        "per_call_usd_measured": round(measured_rate, 8) if measured_rate else None,
        "per_call_usd_at_model_prices": modelled["per_turn_usd"],
        "priced": modelled["priced"],
        "price_state": modelled["price_state"],
        "shape": modelled["shape"],
        "sample": {
            "tasks": measured.get("tasks"),
            "calls": measured.get("calls"),
            "source": measured.get("source"),
        },
        "caveats": measured.get("caveats"),
    }


def _frontier_model() -> str:
    """The most expensive PRICED model in the shared table.

    Selected from the table rather than hard-coded so it cannot drift out of
    ``MODEL_PRICES`` and silently become ``unpriced`` -- which would make every
    "worst case" in the file report ``$0``.
    """
    priced = {
        name: rates
        for name, rates in model_capabilities.MODEL_PRICES.items()
        if rates and float(rates[1]) > 0.0
    }
    if not priced:
        return ""
    return max(priced, key=lambda name: float(priced[name][1]))


def _cheapest_model() -> str:
    """The cheapest PRICED model in the shared table, by output rate."""
    priced = {
        name: rates
        for name, rates in model_capabilities.MODEL_PRICES.items()
        if rates and float(rates[1]) > 0.0
    }
    if not priced:
        return ""
    return min(priced, key=lambda name: float(priced[name][1]))


def tier_estimate(
    tier: str,
    model: Optional[str] = None,
    *,
    turns: int = TURN_CEILING_NEW,
    budget_cap_usd: float = DEFAULT_BUDGET_CAP_USD,
    shape: Optional[WorstCaseShape] = None,
    measured: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The full per-tier receipt: worst case, typical case, and what the cap buys.

    ``turns_the_cap_buys`` is the number an operator actually wants and the one
    no existing report carries: how many of the permitted turns $2.00 pays for
    at this tier's price. At the cheap tier that is more than the ceiling
    (the cap can never stop the run -- the turn ceiling is the binding guard);
    at the frontier tier it is fewer than half. **Those two facts are the whole
    answer to "is the doubled ceiling a cost regression", and they point in
    opposite directions**, which is exactly why a single worst-case number
    cannot answer it.
    """
    name = str(model or _default_tiers().get(tier) or _frontier_model())
    shape = shape or WorstCaseShape()
    worst = worst_case_cost(name, turns, shape=shape)
    worst_old = worst_case_cost(name, TURN_CEILING_OLD, shape=shape)
    per_turn = float(worst["per_turn_usd"] or 0.0)
    cap_buys: Optional[int]
    if not worst["priced"] or per_turn <= 0.0:
        cap_buys = None
    else:
        cap_buys = int(budget_cap_usd // per_turn)
    typical = typical_case_cost(model=name, shape=shape, measured=measured)
    measured_calls = float(
        ((measured or MEASURED_TYPICAL).get("calls_per_task") or {}).get("median")
        or 0.0
    )
    return {
        "tier": tier,
        "model": name,
        "priced": bool(worst["priced"]),
        "price_state": worst["price_state"],
        "input_cost_per_million": model_capabilities.price_of(name).get(
            "input_cost_per_million"
        ),
        "output_cost_per_million": model_capabilities.price_of(name).get(
            "output_cost_per_million"
        ),
        "budget_cap_usd": float(budget_cap_usd),
        "ceiling": {
            "old_turns": TURN_CEILING_OLD,
            "new_turns": int(turns),
            "multiple": round(int(turns) / float(TURN_CEILING_OLD), 4),
        },
        "worst_case": {
            "at_new_ceiling_usd": worst["cost_usd"],
            "at_old_ceiling_usd": worst_old["cost_usd"],
            "per_turn_usd": worst["per_turn_usd"],
            "shape": worst["shape"],
            "reachable_with_default_cap": bool(
                worst["priced"]
                and float(worst["cost_usd"] or 0.0) > float(budget_cap_usd)
            ),
            "turns_the_cap_buys": cap_buys,
            "binding_guard": (
                "budget_cap_usd"
                if worst["priced"]
                and float(worst["cost_usd"] or 0.0) > float(budget_cap_usd)
                else "turn_ceiling"
            ),
        },
        "typical_case": {
            "measured_median_usd": (typical.get("measured") or {}).get("median"),
            "measured_p95_usd": (typical.get("measured") or {}).get("p95"),
            "measured_max_usd": (typical.get("measured") or {}).get("max"),
            "origin": typical.get("origin"),
            "measured_calls_per_task": measured_calls,
            "fraction_of_ceiling_used": (
                round(measured_calls / float(turns), 4) if turns else None
            ),
            "fraction_of_cap_used": (
                round(
                    float((typical.get("measured") or {}).get("median") or 0.0)
                    / float(budget_cap_usd),
                    6,
                )
                if budget_cap_usd
                else None
            ),
        },
    }


def turn_cost_report(
    *,
    tiers: Optional[Sequence[str]] = None,
    config: Optional[Mapping[str, Any]] = None,
    shape: Optional[WorstCaseShape] = None,
) -> Dict[str, Any]:
    """The whole per-tier cost picture, with both cases and the cap's role.

    ``tiers`` defaults to the shipped tier table, so the report describes the
    configuration this project actually routes to rather than a hand-picked
    pair. ``config`` is a ``Task.config``; ``agent_max_turns`` and
    ``budget_cap_usd`` are read from it when present, because the question this
    report answers is always "about THIS configuration".

    A single ``worst_case_usd`` is deliberately ABSENT. The 300x spread across
    models means a scalar here would be read as "the worst case" by every reader
    who skims it, and it would describe no configuration anyone can reach.
    """
    ctx = dict(config or {})
    turns = int(ctx.get("agent_max_turns") or TURN_CEILING_NEW)
    cap = ctx.get("budget_cap_usd")
    budget_cap = float(cap) if cap is not None else DEFAULT_BUDGET_CAP_USD
    names = list(tiers) if tiers else sorted(_default_tiers()) or ["easy", "hard"]
    rows = [
        tier_estimate(name, turns=turns, budget_cap_usd=budget_cap, shape=shape)
        for name in names
    ]
    priced_rows = [row for row in rows if row["priced"]]
    spread = None
    if len(priced_rows) >= 2:
        worst_values = [
            float(row["worst_case"]["at_new_ceiling_usd"]) for row in priced_rows
        ]
        low, high = min(worst_values), max(worst_values)
        if low > 0.0:
            spread = round(high / low, 2)
    return {
        "turns": {
            "old": TURN_CEILING_OLD,
            "new": turns,
            "multiple": round(turns / float(TURN_CEILING_OLD), 4),
        },
        "budget_cap_usd": budget_cap,
        "budget_cap_default": DEFAULT_BUDGET_CAP_USD,
        "cap_is_default": cap is None,
        "worst_case_spread_across_tiers": spread,
        "worst_case_spread_all_priced_models": _spread_all_priced_models(shape),
        "worst_case_spread_note": (
            "cost varies by this factor across the tiers, which is why no "
            "single worst-case scalar is published. The project spec puts the "
            "spread at roughly 300x for comparable accuracy across the wider "
            "model space; this tree's OWN price table spans only "
            f"{_spread_all_priced_models(shape)}x, because the most expensive "
            "frontier row it carries is not the most expensive model that "
            "exists. The spec's 300x is an external claim about a wider space "
            "and is deliberately NOT restated as this table's number"
        ),
        "tiers": rows,
        "measured_typical": MEASURED_TYPICAL,
        "verdict": _verdict(rows, turns, budget_cap),
    }


def _spread_all_priced_models(
    shape: Optional[WorstCaseShape] = None,
) -> Optional[float]:
    """Worst-case spread across EVERY priced model in the shared table.

    Wider than :func:`turn_cost_report`'s per-tier spread because it includes
    models no tier currently routes to. It is the honest local version of the
    spec's "cost varies roughly 300x for comparable accuracy": measured from
    this tree's own table rather than restated, and bounded by what the table
    actually contains.
    """
    shape = shape or WorstCaseShape()
    values: List[float] = []
    for name in model_capabilities.MODEL_PRICES:
        estimate = model_capabilities.estimate_cost(
            name, shape.prompt_tokens, shape.completion_tokens
        )
        if estimate.priced:
            values.append(float(estimate.cost_usd))
    if len(values) < 2:
        return None
    low = min(values)
    return round(max(values) / low, 2) if low > 0.0 else None


def _verdict(
    rows: Sequence[Mapping[str, Any]], turns: int, cap: float
) -> Dict[str, Any]:
    """One sentence, plus the numbers it rests on. Never a bare number."""
    reachable = [row for row in rows if row["worst_case"]["reachable_with_default_cap"]]
    unreachable_priced = [
        row
        for row in rows
        if row["priced"] and not row["worst_case"]["reachable_with_default_cap"]
    ]
    median = float((MEASURED_TYPICAL.get("cost_usd") or {}).get("median") or 0.0)
    if reachable and unreachable_priced:
        headline = (
            f"the doubled ceiling is a CAPABILITY change at {len(unreachable_priced)}"
            f" of {len(rows)} priced tiers (budget_cap_usd=${cap:.2f} stops the run"
            f" first) and a MONEY change at {len(reachable)} -- so the answer"
            " depends on the tier, and a single worst-case number cannot answer it"
        )
    elif reachable:
        headline = (
            f"the worst case at {turns} turns exceeds budget_cap_usd=${cap:.2f} at"
            f" every priced tier, so the cap stops the run before the ceiling at"
            " every tier"
        )
    else:
        headline = (
            f"at {turns} turns the worst case is under budget_cap_usd=${cap:.2f} at"
            " every priced tier, so the TURN CEILING is the binding guard -- the"
            " doubled ceiling is reachable and is a money change"
        )
    return {
        "headline": headline,
        "tiers_where_cap_binds": sorted(row["tier"] for row in reachable),
        "tiers_where_ceiling_binds": sorted(row["tier"] for row in unreachable_priced),
        # The tier rows count TIERS, and two tiers can name the same model --
        # the shipped table's `easy` and `medium` both route to gpt-4o-mini. A
        # reader who took "2 of 3 tiers" as "2 of 3 models" would be wrong, and
        # the wrong reading inflates the apparent spread.
        "distinct_models": sorted({str(row["model"]) for row in rows}),
        "models_where_cap_binds": sorted({str(row["model"]) for row in reachable}),
        "models_where_ceiling_binds": sorted(
            {str(row["model"]) for row in unreachable_priced}
        ),
        "measured_typical_median_usd": median,
        "measured_typical_fraction_of_cap": (round(median / cap, 6) if cap else None),
        "measured_calls_per_task_vs_ceiling": (
            round(
                float(
                    (MEASURED_TYPICAL.get("calls_per_task") or {}).get("median") or 0.0
                )
                / float(turns),
                4,
            )
            if turns
            else None
        ),
        "honesty": (
            "the typical case is MEASURED (502 real provider calls, 50 real "
            "tasks, this machine's own ablation logs); the worst case is MODELLED "
            "at a deliberately generous token shape; the two answer different "
            "questions and are never averaged"
        ),
    }


def preflight_estimate(
    config: Optional[Mapping[str, Any]] = None,
    *,
    tiers: Optional[Sequence[str]] = None,
    shape: Optional[WorstCaseShape] = None,
) -> Dict[str, Any]:
    """What T4's ``doctor`` should render BEFORE a run spends anything.

    This is the surface the brief asks for: the number has to exist and be
    PUBLISHED, and a number a user sees after the run is a receipt, not a
    pre-flight estimate. Three things are published, in this order, because
    they are the three things an operator needs before pressing enter:

    1. the worst case at the configured ceiling, per tier;
    2. the measured typical case, which is what will most likely be spent;
    3. **whether the ceiling is reachable at all**, per tier -- i.e. which guard
       is actually holding the line. An operator who does not know whether the
       cap or the ceiling is the binding guard does not know what happens when
       one of them is raised.

    Shape for a surface: every number is a scalar, the tiers are a list, and
    ``verdict`` is one sentence. It is deliberately NOT the full
    :func:`turn_cost_report` -- that receipt is for an audit, this is for a
    screen.
    """
    report = turn_cost_report(tiers=tiers, config=config, shape=shape)
    rows = report["tiers"]
    return {
        "turns": report["turns"],
        "budget_cap_usd": report["budget_cap_usd"],
        "cap_is_default": report["cap_is_default"],
        "tiers": [
            {
                "tier": row["tier"],
                "model": row["model"],
                "priced": row["priced"],
                "worst_case_usd": row["worst_case"]["at_new_ceiling_usd"],
                "per_turn_usd": row["worst_case"]["per_turn_usd"],
                "typical_case_usd": row["typical_case"]["measured_median_usd"],
                "turns_the_cap_buys": row["worst_case"]["turns_the_cap_buys"],
                "binding_guard": row["worst_case"]["binding_guard"],
                "ceiling_reachable": row["worst_case"]["reachable_with_default_cap"],
            }
            for row in rows
        ],
        "worst_case_spread_across_tiers": report["worst_case_spread_across_tiers"],
        "verdict": report["verdict"]["headline"],
        "typical_case_sample": {
            "tasks": MEASURED_TYPICAL["tasks"],
            "calls": MEASURED_TYPICAL["calls"],
            "source": MEASURED_TYPICAL["source"],
        },
        "shape_assumption": (shape or WorstCaseShape()).to_dict(),
    }


def measured_typical_from_logs(logs_root: Optional[Path] = None) -> Dict[str, Any]:
    """Re-derive :data:`MEASURED_TYPICAL` from the ledgers it claims to come from.

    Exists so the literal above is checkable rather than asserted. Returns the
    same shape, plus ``rederived_from`` and ``matches_recorded`` so a caller can
    tell "I recomputed it" from "I read the number off the constant". A missing
    logs root returns ``available: false`` with a reason rather than zeros --
    reporting ``median: 0`` for a population that could not be read is the
    "missing ledger reads as zero spend" failure this repo already fixed once.
    """
    root = Path(logs_root) if logs_root is not None else Path("logs")
    patterns = ("ablations/ir2-final/tasklogs", "ablations/v4/tasklogs")
    per_task: Dict[str, List[float]] = {}
    calls = 0
    for pattern in patterns:
        base = root / pattern
        if not base.is_dir():
            continue
        for ledger in sorted(base.glob("*.runtime/model_ledger.jsonl")):
            task = ledger.parent.name
            for line in ledger.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not row.get("prompt_tokens"):
                    continue
                calls += 1
                per_task.setdefault(task, []).append(float(row.get("cost_usd") or 0.0))
    if not per_task:
        return {
            "available": False,
            "reason": (
                f"no ablation ledgers under {root} as a {patterns[0]}; the"
                " recorded typical case stays a RECORDED MEASUREMENT and is"
                " labelled as one"
            ),
            "rederived_from": str(root),
        }
    totals = sorted(sum(v) for v in per_task.values())
    counts = sorted(len(v) for v in per_task.values())
    median = float(statistics.median(totals))
    return {
        "available": True,
        "rederived_from": str(root),
        "tasks": len(per_task),
        "calls": calls,
        "cost_usd": {
            "median": round(median, 6),
            "mean": round(statistics.mean(totals), 6),
            "p95": round(totals[max(0, int(0.95 * len(totals)) - 1)], 6),
            "max": round(max(totals), 6),
            "total": round(sum(totals), 6),
        },
        "calls_per_task": {
            "median": statistics.median(counts),
            "p95": counts[max(0, int(0.95 * len(counts)) - 1)],
            "max": max(counts),
            "min": min(counts),
        },
        "matches_recorded": abs(
            median
            - float((MEASURED_TYPICAL.get("cost_usd") or {}).get("median") or 0.0)
        )
        < 0.002,
    }


def assert_default_budget_cap() -> Dict[str, Any]:
    """Compare this module's ``DEFAULT_BUDGET_CAP_USD`` with the shipped one.

    A receipt, not an assertion: it reports whether the two agree and what each
    is. A drift here is not a crash -- an operator may legitimately have changed
    the default -- but every figure in this module would then be describing a
    configuration nobody is running, and that has to be visible.
    """
    shipped = DEFAULT_BUDGET_CAP_USD
    actual: Optional[float] = None
    source = "literal (harness.config not read)"
    try:  # read-only, best-effort: harness is another owner's module
        from harness import config as harness_config

        value = (getattr(harness_config, "DEFAULTS", {}) or {}).get("budget_cap_usd")
        if value is not None:
            actual = float(value)
            source = "harness.config.DEFAULTS['budget_cap_usd']"
    except Exception as exc:  # pragma: no cover - harness import is best effort
        return {
            "comparable": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "literal": shipped,
        }
    return {
        "comparable": actual is not None,
        "source": source,
        "literal": shipped,
        "shipped": actual,
        "agrees": actual is None or abs(float(actual) - shipped) < 1e-9,
    }


@dataclass
class _Render:
    """Rendering state. Kept out of the receipt: a table is not a data shape."""

    rows: List[str] = field(default_factory=list)


def render_report(report: Mapping[str, Any]) -> str:
    """A human table of the report. ``--json`` is the machine surface."""
    lines: List[str] = []
    turns = report.get("turns") or {}
    lines.append(
        "turn ceiling: %s -> %s turns (%sx)   budget_cap_usd: $%s%s"
        % (
            turns.get("old"),
            turns.get("new"),
            turns.get("multiple"),
            report.get("budget_cap_usd"),
            "  (default)" if report.get("cap_is_default") else "  (configured)",
        )
    )
    spread = report.get("worst_case_spread_across_tiers")
    if spread:
        lines.append(
            "worst-case spread: %sx across the tiers below, %sx across every priced"
            " model in the table  <- why there is no single worst-case number"
            % (spread, report.get("worst_case_spread_all_priced_models"))
        )
    lines.append("")
    header = "%-8s %-30s %12s %12s %12s %10s  %s" % (
        "tier",
        "model",
        "per turn",
        "worst@ceil",
        "typical",
        "cap buys",
        "binding guard",
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in report.get("tiers") or []:
        worst = row["worst_case"]
        typical = row["typical_case"]

        def money(value: Any) -> str:
            return "unpriced" if value is None else "$%.4f" % float(value)

        lines.append(
            "%-8s %-30s %12s %12s %12s %10s  %s"
            % (
                row["tier"],
                (row["model"] or "")[:30],
                money(worst["per_turn_usd"]) if row["priced"] else "unpriced",
                money(worst["at_new_ceiling_usd"]) if row["priced"] else "unpriced",
                money(typical["measured_median_usd"]),
                worst["turns_the_cap_buys"]
                if worst["turns_the_cap_buys"] is not None
                else "-",
                worst["binding_guard"],
            )
        )
    lines.append("")
    verdict = report.get("verdict") or {}
    lines.append("VERDICT: %s" % verdict.get("headline"))
    lines.append(
        "measured typical: median $%s over %s real tasks / %s real provider calls"
        % (
            (report.get("measured_typical") or {}).get("cost_usd", {}).get("median"),
            (report.get("measured_typical") or {}).get("tasks"),
            (report.get("measured_typical") or {}).get("calls"),
        )
    )
    lines.append(
        "  a real task uses %s of the permitted turns and ~%.2f%% of the cap"
        % (
            verdict.get("measured_calls_per_task_vs_ceiling"),
            100.0 * float(verdict.get("measured_typical_fraction_of_cap") or 0.0),
        )
    )
    lines.append("  %s" % verdict.get("honesty"))
    return "\n".join(lines)


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m runtime.turn_cost",
        description="Per-tier cost of the current turn ceiling: worst AND typical.",
    )
    parser.add_argument("--tiers", help="comma-separated tier names")
    parser.add_argument("--turns", type=int, default=None, help="override the ceiling")
    parser.add_argument(
        "--cap", type=float, default=None, help="override budget_cap_usd"
    )
    parser.add_argument(
        "--prompt-tokens", type=int, default=None, help="worst-case shape override"
    )
    parser.add_argument(
        "--completion-tokens", type=int, default=None, help="worst-case shape override"
    )
    parser.add_argument("--json", action="store_true", help="machine receipt")
    parser.add_argument(
        "--preflight", action="store_true", help="the shape cli/doctor.py renders"
    )
    parser.add_argument(
        "--rederive", action="store_true", help="recompute the measured typical case"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.rederive:
        payload: Dict[str, Any] = measured_typical_from_logs()
        payload["recorded"] = MEASURED_TYPICAL
        payload["cap_check"] = assert_default_budget_cap()
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("available") else 2

    config: Dict[str, Any] = {}
    if args.turns is not None:
        config["agent_max_turns"] = args.turns
    if args.cap is not None:
        config["budget_cap_usd"] = args.cap
    shape = None
    if args.prompt_tokens is not None or args.completion_tokens is not None:
        shape = WorstCaseShape(
            prompt_tokens=args.prompt_tokens
            if args.prompt_tokens is not None
            else DEFAULT_WORST_SHAPE["prompt_tokens"],
            completion_tokens=args.completion_tokens
            if args.completion_tokens is not None
            else DEFAULT_WORST_SHAPE["completion_tokens"],
            origin="cli_override",
        )
    tiers = (
        [t.strip() for t in args.tiers.split(",") if t.strip()] if args.tiers else None
    )
    if args.preflight:
        payload = preflight_estimate(config, tiers=tiers, shape=shape)
        print(
            json.dumps(payload, indent=2, sort_keys=True)
            if args.json
            else _render_preflight(payload)
        )
        return 0
    report = turn_cost_report(tiers=tiers, config=config, shape=shape)
    print(
        json.dumps(report, indent=2, sort_keys=True)
        if args.json
        else render_report(report)
    )
    return 0


def _render_preflight(payload: Mapping[str, Any]) -> str:
    lines = [
        "PRE-FLIGHT COST ESTIMATE  (turns %s, budget_cap_usd $%s)"
        % (payload["turns"]["new"], payload["budget_cap_usd"]),
        "",
    ]
    for row in payload["tiers"]:
        lines.append(
            "  %-8s %-28s worst $%-9s typical $%-9s cap buys %-5s guard=%s"
            % (
                row["tier"],
                (row["model"] or "")[:28],
                "unpriced" if not row["priced"] else "%.4f" % row["worst_case_usd"],
                "%.4f" % float(row["typical_case_usd"] or 0.0),
                row["turns_the_cap_buys"],
                row["binding_guard"],
            )
        )
    lines.append("")
    lines.append("  %s" % payload["verdict"])
    lines.append(
        "  typical case: %s tasks / %s real calls, from %s"
        % (
            payload["typical_case_sample"]["tasks"],
            payload["typical_case_sample"]["calls"],
            payload["typical_case_sample"]["source"],
        )
    )
    lines.append(
        "  worst case assumes %s prompt + %s completion tokens per turn (%s)"
        % (
            payload["shape_assumption"]["prompt_tokens"],
            payload["shape_assumption"]["completion_tokens"],
            payload["shape_assumption"]["origin"],
        )
    )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - operator convenience
    raise SystemExit(_cli())
