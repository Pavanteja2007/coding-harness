"""The eight honesty invariants this repo's reports rest on, as executable checks.

Every rule in this module exists because it was learned from a real defect.
They are stated here as code so a regression is a FAILING CHECK rather than a
comment nobody re-reads, and so a future terminal can run one command instead of
re-deriving the discipline.

Run it::

    python -m runtime.invariants            # human-readable table
    python -m runtime.invariants --json     # machine-readable receipt
    python -m runtime.invariants --only spent_usd_is_a_maximum

Exit code is ``0`` when every selected invariant holds, ``2`` when any is
violated. There is no "warn" tier: an invariant that can be warned about is an
invariant that can be broken quietly.

**The eight, and the lie each one prevents:**

===============================  ==================================================
invariant                        the direction it fails in
===============================  ==================================================
:data:`INV_UNPRICED`             the price ladder picks the unpriced model as
                                 CHEAPEST, so the report under-estimates in the
                                 direction that SPENDS money
:data:`INV_UNKNOWN_CAPABILITY`   ``False`` refuses to route the entire world
:data:`INV_SPENT_MAX`            a sum fires the cap LATER than intended
:data:`INV_EFFORT_EMPTY`         "I set it to max" silently becomes "high"
:data:`INV_EFFORT_NO_CLAMP`      the same, from the other direction
:data:`INV_REFUSAL_STICKY`       a refused call retries itself
:data:`INV_UNRECEIPTED`          a missing ledger reads as "we checked, it's zero"
:data:`INV_CACHE_UNREPORTED`     a provider that never reports usage reads as a
                                 0% hit rate
===============================  ==================================================

Plus :func:`check_structural_guard`, which is not one of the eight: it
verifies that the held-out-winning structural difficulty predictor is STILL not
shipped. That predictor won its split (0.9412 vs 0.8235) and was deliberately
withheld for having 1 hard label against a floor of 3. An unshipped predictor
that accidentally shipped would be a silent behaviour change, so the guard is
checked the same way as everything else here.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "INVARIANTS",
    "INV_CACHE_UNREPORTED",
    "INV_EFFORT_EMPTY",
    "INV_EFFORT_NO_CLAMP",
    "INV_REFUSAL_STICKY",
    "INV_SPENT_MAX",
    "INV_UNKNOWN_CAPABILITY",
    "INV_UNPRICED",
    "INV_UNRECEIPTED",
    "InvariantResult",
    "check_structural_guard",
    "main",
    "run_all",
]

INV_UNPRICED = "unpriced_is_never_zero"
INV_UNKNOWN_CAPABILITY = "unknown_capability_is_never_false"
INV_SPENT_MAX = "spent_usd_is_a_maximum"
INV_EFFORT_EMPTY = "effort_parameters_empty_unless_sent"
INV_EFFORT_NO_CLAMP = "unsupported_effort_is_never_clamped"
INV_REFUSAL_STICKY = "a_budget_refusal_is_sticky"
INV_UNRECEIPTED = "unreceipted_calls_is_reported_honestly"
INV_CACHE_UNREPORTED = "cache_status_separates_unreported_from_miss"


@dataclass
class InvariantResult:
    """One invariant's measured verdict.

    ``observations`` is the REAL output -- the numbers and values the check
    read. A check that passes while carrying no evidence is the "vacuous pass"
    this repo records as worse than no test, so every result here carries at
    least one observation, and a result with an empty ``observations`` list is
    itself reported as a violation.
    """

    invariant: str
    holds: bool
    claim: str
    prevents: str
    observations: Dict[str, Any] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "invariant": self.invariant,
            "holds": self.holds,
            "claim": self.claim,
            "prevents": self.prevents,
            "observations": self.observations,
            "failures": self.failures,
        }


def _result(
    invariant: str,
    *,
    holds: bool,
    claim: str,
    prevents: str,
    observations: Dict[str, Any],
    failures: Optional[List[str]] = None,
) -> InvariantResult:
    """Build a result, refusing to report a pass with no evidence."""
    fails = list(failures or [])
    if not observations:
        # A check that observed nothing has not verified anything. Recording it
        # as a pass is precisely the failure mode this module exists to catch,
        # so it is a failure here too.
        holds = False
        fails.append("the check observed nothing, so it verified nothing")
    return InvariantResult(
        invariant=invariant,
        holds=holds,
        claim=claim,
        prevents=prevents,
        observations=observations,
        failures=fails,
    )


# -- 1. unpriced is never $0 ----------------------------------------------


def check_unpriced() -> InvariantResult:
    """An unpriced model reports ``unpriced``, never a zero cost.

    ``PRICE_STATES`` is the closed vocabulary. ``free`` is reachable only by a
    DECLARED ``(0.0, 0.0)`` row, so the two are separable -- which is the only
    reason this is checkable at all.

    **What is asserted, precisely.** ``cost_usd == 0.0`` is NOT by itself a lie
    on an unpriced model; ``CostEstimate`` is deliberately a two-key receipt in
    which ``price_state`` travels with the number, because a bare ``0.0`` is
    ambiguous between a free call, an unpriced model, and a provider that
    reported nothing. So the invariant is that the STATE is honest and travels:

    * an undeclared model is ``price_state='unpriced'`` with ``priced=False``;
    * that state is present in ``to_dict()``, so it cannot be dropped on the
      way to a ledger row;
    * ``unpriced`` stays DISTINGUISHABLE from ``free`` and from ``priced``;
    * and the gate refuses to ROUTE an unpriced model when it is switched on,
      which is the mechanism that actually stops an unpriced model being
      handed the cheapest-looking tier.
    """
    from runtime import model_capabilities as mc

    obs: Dict[str, Any] = {}
    fails: List[str] = []

    obs["price_states"] = list(mc.PRICE_STATES)

    # An undeclared model. This is the case the invariant is about.
    estimate = mc.estimate_cost(
        "acme/definitely-not-a-declared-model", 1_000_000, 1_000_000
    )
    obs["undeclared_model"] = {
        "model": "acme/definitely-not-a-declared-model",
        "price_state": estimate.price_state,
        "priced": estimate.priced,
        "cost_usd": estimate.cost_usd,
    }
    if estimate.price_state != mc.PRICE_UNPRICED:
        fails.append(
            f"an undeclared model reported price_state={estimate.price_state!r}, "
            f"expected {mc.PRICE_UNPRICED!r}"
        )
    if estimate.priced:
        fails.append("an undeclared model reported priced=True")

    # The state must travel with the number. `to_dict()` is what reaches the
    # ledger row and the trace payload, so a state dropped HERE is a state
    # dropped everywhere.
    row = estimate.to_dict()
    obs["undeclared_model_to_dict"] = row
    if row.get("price_state") != mc.PRICE_UNPRICED:
        fails.append(
            f"to_dict() dropped or rewrote the price state: {row.get('price_state')!r} "
            "-- the state travels with the number, so dropping it here makes "
            "cost_usd=0.0 read as 'free' on every surface downstream"
        )
    if row.get("cost_priced") is not False:
        fails.append(
            f"to_dict() reported cost_priced={row.get('cost_priced')!r}, expected False"
        )
    obs["state_travels_with_the_number"] = "price_state" in row and "cost_priced" in row

    # A DECLARED free model is the control arm: without it, "cost is 0.0"
    # would look like the same thing and the check could not discriminate.
    try:
        mc.register_capability(
            {
                "provider": "acme",
                "model": "free-probe",
                "input_cost_per_million": 0.0,
                "output_cost_per_million": 0.0,
            }
        )
        free = mc.estimate_cost("free-probe", 1_000_000, 1_000_000)
        obs["declared_free_control"] = {
            "price_state": free.price_state,
            "priced": free.priced,
            "cost_usd": free.cost_usd,
        }
        if free.price_state != mc.PRICE_FREE:
            fails.append(
                f"a DECLARED (0.0, 0.0) row reported {free.price_state!r}, "
                f"expected {mc.PRICE_FREE!r} -- free must stay reachable, or "
                "'unpriced' and 'free' become the same answer and $0 becomes honest"
            )
        if free.cost_usd != 0.0:
            fails.append(f"a declared free model reported cost_usd={free.cost_usd!r}")
    finally:
        mc.unregister_capability("acme", "free-probe")

    # A DECLARED priced model is the other arm: all three states must be
    # reachable and distinguishable.
    try:
        mc.register_capability(
            {
                "provider": "acme",
                "model": "paid-probe",
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
        )
        paid = mc.estimate_cost("paid-probe", 1_000_000, 1_000_000)
        obs["declared_priced_control"] = {
            "price_state": paid.price_state,
            "priced": paid.priced,
            "cost_usd": paid.cost_usd,
        }
        if paid.price_state != mc.PRICE_PRICED:
            fails.append(f"a declared paid row reported {paid.price_state!r}")
        if paid.cost_usd != 3.0:
            fails.append(
                f"a declared 1.0/2.0 per-1M row reported cost_usd={paid.cost_usd!r} "
                "for 1M in + 1M out, expected 3.0"
            )
    finally:
        mc.unregister_capability("acme", "paid-probe")

    obs["three_states_are_distinguishable"] = {
        mc.PRICE_PRICED: True,
        mc.PRICE_FREE: True,
        mc.PRICE_UNPRICED: True,
        "distinct_strings": len({mc.PRICE_PRICED, mc.PRICE_FREE, mc.PRICE_UNPRICED})
        == 3,
    }
    if len({mc.PRICE_PRICED, mc.PRICE_FREE, mc.PRICE_UNPRICED}) != 3:
        fails.append("two of the three price states share a string")

    # The mechanism that actually stops an unpriced model being handed the
    # cheapest-looking tier: the routing gate REFUSES it when switched on.
    # Note the shape -- `screen_candidates` RAISES `CapabilityRoutingRefused`
    # rather than returning a verdict, because a refusal that is merely
    # returned can be ignored by a caller that only reads `.selected`. The
    # fixture is a model with NO row at all: declaring `(0.0, 0.0)` would make
    # it `free`, and a free model is legitimately routable.
    obs["gate_refuses_a_genuinely_unpriced_model"] = {
        "candidate": {"provider": "acme", "model": "never-declared-at-all"},
        "allow_unpriced": False,
    }
    try:
        refused = mc.screen_candidates(
            [{"provider": "acme", "model": "never-declared-at-all"}],
            allow_unpriced=False,
        )
        obs["gate_refuses_a_genuinely_unpriced_model"]["outcome"] = "returned"
        obs["gate_refuses_a_genuinely_unpriced_model"]["refusals"] = [
            r.to_dict() for r in refused.refusals
        ]
        obs["gate_refuses_a_genuinely_unpriced_model"]["considered"] = (
            refused.considered
        )
        fails.append(
            "the routing gate RETURNED a verdict for a model with no price row "
            f"(refusals={obs['gate_refuses_a_genuinely_unpriced_model']['refusals']}) "
            "instead of refusing it -- a returned refusal can be ignored by a caller "
            "that only reads the selection"
        )
    except mc.CapabilityRoutingRefused as exc:
        obs["gate_refuses_a_genuinely_unpriced_model"]["outcome"] = "raised"
        obs["gate_refuses_a_genuinely_unpriced_model"]["raised"] = {
            "type": type(exc).__name__,
            "reason": getattr(exc, "reason", ""),
            "model": getattr(exc, "model", ""),
            "provider": getattr(exc, "provider", ""),
            "alternatives": [
                str(a) for a in (getattr(exc, "alternatives", None) or [])
            ],
            "str": str(exc),
        }
        reason = str(getattr(exc, "reason", ""))
        if mc.REFUSAL_REASONS[0] not in reason:
            fails.append(
                f"the gate raised but named reason={reason!r}, not "
                f"{mc.REFUSAL_REASONS[0]!r} -- a reader cannot tell an unpriced "
                "refusal from a tool-capability one"
            )
        if not str(getattr(exc, "model", "")):
            fails.append(
                "the refusal did not name WHICH model it was about -- "
                "'your credit ran out' and 'that model cannot do tools' are "
                "different problems with different fixes"
            )
        if mc.REFUSAL_REASONS[0] not in mc.REFUSAL_REASONS:
            fails.append("the unpriced refusal reason is not in the closed vocabulary")

    # The control arm: `allow_unpriced=True` must let the SAME model through,
    # or "the gate refuses" would be satisfiable by a gate that refuses
    # everything and no run could ever dial.
    try:
        allowed = mc.screen_candidates(
            [{"provider": "acme", "model": "never-declared-at-all"}],
            allow_unpriced=True,
        )
        obs["gate_allows_the_same_model_when_opted_in"] = {
            "refusals": [r.to_dict() for r in allowed.refusals],
            "considered": allowed.considered,
        }
    except mc.CapabilityRoutingRefused as exc:
        obs["gate_allows_the_same_model_when_opted_in"] = {
            "raised": str(exc),
        }
        fails.append(
            "the CONTROL arm failed: allow_unpriced=True did not let an undeclared "
            "model through -- 'the gate refuses unpriced' must not be satisfiable by "
            "a gate that refuses every model, or no run could ever dial"
        )

    return _result(
        INV_UNPRICED,
        holds=not fails,
        claim="an unpriced model reports price_state='unpriced' and priced=False; "
        "that state travels with the number through to_dict(); cost_usd==0.0 is "
        "distinguishable from a DECLARED (0.0, 0.0) 'free' row; and the routing gate "
        "refuses to hand an unpriced model a tier when allow_unpriced=False",
        prevents="an absent price row being reported as $0 and therefore picked as "
        "the CHEAPEST model, so the cost report under-estimates in the direction "
        "that spends money",
        observations=obs,
        failures=fails,
    )


# -- 2. unknown capability is never False ----------------------------------


def check_unknown_capability() -> InvariantResult:
    """An unknown capability reports ``None``, never ``False``.

    ``False`` means "nobody described this model" AND "this model cannot do
    tool calls", and the second reading refuses to route the entire world. The
    strict reading is available, but only through an explicit opt-in key.
    """
    from runtime import model_capabilities as mc

    obs: Dict[str, Any] = {}
    fails: List[str] = []

    # `lookup_capability` returns None for a model nobody described -- it never
    # fabricates a row -- so the total function `capability_of` is what a
    # reporting surface reads. Reading `lookup_capability` and assuming a row
    # would make this check crash on the exact case it is about.
    row = mc.capability_of("acme", "acme/nobody-described-this")
    obs["undeclared_row"] = {
        "provider": row.provider,
        "model": row.model,
        "supports_tools": row.supports_tools,
        "supports_reasoning": row.supports_reasoning,
        "supports_streaming": row.supports_streaming,
        "known": row.known,
    }
    obs["lookup_capability_returns_none_for_it"] = (
        mc.lookup_capability("acme", "acme/nobody-described-this") is None
    )
    if not obs["lookup_capability_returns_none_for_it"]:
        fails.append(
            "lookup_capability FABRICATED a row for a model nobody described -- it "
            "must return None and let capability_of supply the honest unknown"
        )
    for field_name in ("supports_tools", "supports_reasoning", "supports_streaming"):
        value = getattr(row, field_name)
        if value is False:
            fails.append(
                f"{field_name}=False for an undeclared model -- that refuses to "
                "route the entire world; the honest answer is None"
            )
        elif value is not None:
            fails.append(
                f"{field_name}={value!r} for an undeclared model, expected None"
            )
    if row.known is not False:
        fails.append(
            f"an undeclared model reported known={row.known!r}; 'nobody described "
            "this' must stay distinguishable from a described row"
        )

    # The control arm: a DECLARED False must stay reachable. Without it, "never
    # False" would be satisfiable by a registry that cannot say False at all,
    # and the check could not tell a fix from a dead field.
    try:
        mc.register_capability(
            {"provider": "acme", "model": "acme/no-tools", "supports_tools": False}
        )
        declared = mc.lookup_capability("acme", "acme/no-tools")
        obs["declared_false_control"] = {
            "supports_tools": declared.supports_tools,
            "known": declared.known,
        }
        if declared is None or declared.supports_tools is not False:
            fails.append(
                f"a DECLARED supports_tools=False reported "
                f"{declared.supports_tools!r} -- False must stay reachable"
            )
    finally:
        mc.unregister_capability("acme", "acme/no-tools")

    obs["unknown_capability_helper"] = {
        "supports_tools": mc.unknown_capability().supports_tools,
    }
    return _result(
        INV_UNKNOWN_CAPABILITY,
        holds=not fails,
        claim="an undeclared model's capabilities are None; False is reachable only "
        "through an explicit declaration",
        prevents="treating 'nobody described this model' as 'this model cannot do "
        "tool calls', which refuses to route the entire world",
        observations=obs,
        failures=fails,
    )


# -- 3. spent_usd() is a maximum, never a sum ------------------------------


def check_spent_usd_is_maximum() -> InvariantResult:
    """``spent_usd()`` is ``max(charges, bound)``, never a sum.

    The governor sees its own charges; ``spend_source`` sees charges made
    elsewhere (a provider fallback's earlier attempts, the difficulty
    classifier). Neither sees both, so a sum would double-count and a single
    source would UNDER-count. The maximum covers both without double-counting,
    so the cap can only fire **earlier** than either source alone -- never
    later.
    """
    from runtime import budget_governor as bg

    obs: Dict[str, Any] = {}
    fails: List[str] = []

    # Case A: the bound sees MORE than the governor. Must report the bound.
    seen_external = {"usd": 7.50}
    gov_a = bg.BudgetGovernor(cap_usd=100.0, spend_source=lambda: seen_external["usd"])
    gov_a.commit(1.00)
    spent_a = gov_a.spent_usd()
    obs["bound_exceeds_own"] = {
        "own_charges": 1.00,
        "external_bound": 7.50,
        "spent_usd": spent_a,
        "sum_would_be": 8.50,
    }
    if abs(spent_a - 7.50) > 1e-9:
        fails.append(
            f"spent_usd()={spent_a!r} with charges=1.0 bound=7.5, expected 7.5"
        )
    if abs(spent_a - 8.50) < 1e-9:
        fails.append(
            "spent_usd() summed the charges and the bound -- a sum can only "
            "ever exceed the cap, which fires the cap later than intended"
        )

    # Case B: the governor sees MORE than the bound. Must report its own.
    gov_b = bg.BudgetGovernor(cap_usd=100.0, spend_source=lambda: 0.25)
    gov_b.commit(4.00)
    spent_b = gov_b.spent_usd()
    obs["own_exceeds_bound"] = {
        "own_charges": 4.00,
        "external_bound": 0.25,
        "spent_usd": spent_b,
        "sum_would_be": 4.25,
    }
    if abs(spent_b - 4.00) > 1e-9:
        fails.append(
            f"spent_usd()={spent_b!r} with charges=4.0 bound=0.25, expected 4.0"
        )

    # Case C: the bound RAISES. It must be ignored, never allowed to disable
    # the cap -- a bound that cannot be read is not a bound of zero.
    def exploding_bound() -> float:
        raise RuntimeError("the bound source is unavailable")

    gov_c = bg.BudgetGovernor(cap_usd=100.0, spend_source=exploding_bound)
    gov_c.commit(2.00)
    spent_c = gov_c.spent_usd()
    obs["bound_raises"] = {
        "own_charges": 2.00,
        "external_bound": "raised RuntimeError",
        "spent_usd": spent_c,
    }
    if abs(spent_c - 2.00) > 1e-9:
        fails.append(
            f"spent_usd()={spent_c!r} when the bound RAISED; a bound that cannot be "
            "read must never be allowed to disable the cap"
        )

    # Case D: no bound at all. Must report the governor's own charges.
    gov_d = bg.BudgetGovernor(cap_usd=100.0)
    gov_d.commit(3.00)
    spent_d = gov_d.spent_usd()
    obs["no_bound"] = {"own_charges": 3.00, "spent_usd": spent_d}
    if abs(spent_d - 3.00) > 1e-9:
        fails.append(f"spent_usd()={spent_d!r} with no bound and 3.0 charged")

    obs["sum_would_exceed_max_in_every_case"] = {
        "case_a": obs["bound_exceeds_own"]["sum_would_be"]
        > obs["bound_exceeds_own"]["spent_usd"],
        "case_b": obs["own_exceeds_bound"]["sum_would_be"]
        > obs["own_exceeds_bound"]["spent_usd"],
    }
    return _result(
        INV_SPENT_MAX,
        holds=not fails,
        claim="spent_usd() == max(own charges, external bound); a raising bound is "
        "ignored and never disables the cap",
        prevents="a sum fires the budget cap LATER than intended; a single source "
        "fires it earlier than the truth",
        observations=obs,
        failures=fails,
    )


# -- 4. EffortPlan.parameters is empty unless sent --------------------------


def check_effort_parameters_empty() -> InvariantResult:
    """``EffortPlan.parameters`` is empty for every status except ``sent``.

    This is structural rather than conventional: ``parameters`` is a property
    that reads ``sent``, and ``sent`` is a property that reads ``status``. An
    unsupported status therefore CANNOT produce a parameter -- there is no
    branch that clamps and no default to forget.
    """
    from runtime import model_capabilities as mc

    obs: Dict[str, Any] = {}
    fails: List[str] = []
    non_empty: Dict[str, Any] = {}

    # Every level against every model family the registry knows, plus a model
    # with no declared knob. A sweep rather than three cases, because the
    # invariant is over the PRODUCT of (level, family) and three samples of a
    # nine-element product proves nothing about the other six.
    levels = ["minimal", "low", "medium", "high", "xhigh", "max", "auto", "bogus"]
    families = [
        ("openai", "gpt-5"),
        ("anthropic", "claude-sonnet-4-20250514"),
        ("google", "gemini-2.5-pro"),
        ("openai", "gpt-4o"),  # deliberately claims nothing
        ("acme", "acme/no-knob"),
    ]
    rows: List[Dict[str, Any]] = []
    for provider, model in families:
        for level in levels:
            plan = mc.map_effort(level, model, provider=provider)
            params = plan.parameters
            rows.append(
                {
                    "provider": provider,
                    "model": model,
                    "level": level,
                    "status": plan.status,
                    "sent": plan.sent,
                    "parameters": params,
                }
            )
            if plan.sent != (plan.status == mc.EFFORT_SENT):
                fails.append(
                    f"{provider}/{model} level={level!r}: sent={plan.sent} but "
                    f"status={plan.status!r}"
                )
            if not plan.sent and params:
                non_empty[f"{provider}/{model}:{level}"] = {
                    "status": plan.status,
                    "parameters": params,
                }
            if plan.sent and not params:
                fails.append(
                    f"{provider}/{model} level={level!r} reported status='sent' but "
                    "put NO parameter on the request -- 'I set it to max' must not "
                    "silently send nothing either"
                )

    obs["sweep"] = {"levels": levels, "families": [f"{p}/{m}" for p, m in families]}
    obs["rows_probed"] = len(rows)
    obs["statuses_observed"] = sorted({r["status"] for r in rows})
    obs["non_empty_parameters_for_a_non_sent_status"] = non_empty
    obs["sample_rows"] = rows[:6]

    if non_empty:
        fails.append(
            f"{len(non_empty)} non-sent plan(s) produced parameters: {sorted(non_empty)[:5]}"
        )

    return _result(
        INV_EFFORT_EMPTY,
        holds=not fails,
        claim="EffortPlan.parameters is {} for every status except 'sent', and "
        "non-empty for every 'sent' -- across the full product of 8 levels x 5 families",
        prevents="'I set it to max' silently becoming 'I set it to high', and the "
        "reverse lie of claiming a parameter was sent when it was not",
        observations=obs,
        failures=fails,
    )


# -- 5. an unsupported effort level is never clamped -----------------------


def check_effort_no_clamp() -> InvariantResult:
    """An unsupported effort level returns ``unsupported_level`` and sends nothing.

    A ladder, not a slider that lies. ``max`` on a three-level family must not
    become ``high``: the user asked for a rung the family does not have, and
    silently substituting a different one is a receipt that disagrees with the
    request.
    """
    from runtime import model_capabilities as mc

    obs: Dict[str, Any] = {}
    fails: List[str] = []

    # openai declares `reasoning_effort` = low/medium/high. The DECLARED ladder
    # is wider than any one family's knob: `xhigh` and `max` are real rungs a
    # person can ask for that openai does not accept. `minimal` is NOT a rung
    # at all -- it is `invalid`, a fourth outcome, and is checked separately so
    # the two cannot be confused.
    obs["declared_effort_levels"] = list(mc.EFFORT_LEVELS)
    obs["levels_that_are_not_rungs"] = {
        "minimal": mc.map_effort("minimal", "gpt-5", provider="openai").status,
        "definitely-not-a-level": mc.map_effort(
            "definitely-not-a-level", "gpt-5", provider="openai"
        ).status,
    }

    for level in ("max", "xhigh"):
        plan = mc.map_effort(level, "gpt-5", provider="openai")
        obs[f"openai/{level}"] = {
            "status": plan.status,
            "parameter": plan.parameter,
            "value": plan.value,
            "parameters": plan.parameters,
            "detail": plan.detail,
        }
        if plan.status != mc.EFFORT_UNSUPPORTED_LEVEL:
            fails.append(
                f"openai/gpt-5 level={level!r} reported status={plan.status!r}, "
                f"expected {mc.EFFORT_UNSUPPORTED_LEVEL!r} -- an unsupported rung "
                "must never be clamped to the nearest supported one"
            )
        if plan.parameters:
            fails.append(
                f"openai/gpt-5 level={level!r} put {plan.parameters!r} on the "
                "request despite being unsupported"
            )
        if plan.value in {"low", "medium", "high"}:
            fails.append(
                f"openai/gpt-5 level={level!r} was CLAMPED to value={plan.value!r}"
            )

    # The control arm: a SUPPORTED level on the same family must still send.
    control = mc.map_effort("high", "gpt-5", provider="openai")
    obs["openai/high_control"] = {
        "status": control.status,
        "parameter": control.parameter,
        "value": control.value,
        "parameters": control.parameters,
    }
    if control.status != mc.EFFORT_SENT or control.parameters != {
        "reasoning_effort": "high"
    }:
        fails.append(
            "the CONTROL arm failed: openai/gpt-5 'high' must send "
            f"reasoning_effort=high, got status={control.status!r} "
            f"parameters={control.parameters!r} -- otherwise 'never clamp' would be "
            "satisfiable by a registry that sends nothing at all"
        )

    # A model with NO declared knob is a different refusal (unsupported_model),
    # not a clamp, and must not borrow the unsupported_level vocabulary.
    no_knob = mc.map_effort("max", "acme/no-knob", provider="acme")
    obs["no_declared_knob"] = {
        "status": no_knob.status,
        "parameter": no_knob.parameter,
        "parameters": no_knob.parameters,
    }
    if no_knob.parameters:
        fails.append(
            "a model with no declared effort knob put a parameter on the request"
        )

    # `invalid` is a fourth outcome and must echo the input back, not guess.
    invalid = mc.map_effort("definitely-not-a-level", "gpt-5", provider="openai")
    obs["invalid_input"] = {
        "status": invalid.status,
        "requested": invalid.requested,
        "parameters": invalid.parameters,
    }
    if invalid.parameters:
        fails.append("an invalid effort value put a parameter on the request")
    if invalid.status != mc.EFFORT_INVALID:
        fails.append(
            f"a value that is not a rung reported status={invalid.status!r}, "
            f"expected {mc.EFFORT_INVALID!r} -- 'invalid' must not collapse into "
            "'unsupported_level', because one echoes the input and the other does not"
        )
    if str(invalid.requested) != "definitely-not-a-level":
        fails.append(
            f"'invalid' did not echo its input back (requested={invalid.requested!r})"
        )

    # Every rung the ladder declares, against a family that accepts all of them
    # and one that accepts none, so both arms are measured rather than assumed.
    anthropic_all = {
        level: mc.map_effort(level, "claude-sonnet-4-20250514", provider="anthropic")
        for level in mc.EFFORT_LEVELS
    }
    obs["anthropic_every_declared_rung"] = {
        level: {"status": plan.status, "parameters": plan.parameters}
        for level, plan in anthropic_all.items()
    }
    openai_all = {
        level: mc.map_effort(level, "gpt-5", provider="openai")
        for level in mc.EFFORT_LEVELS
    }
    obs["openai_every_declared_rung"] = {
        level: {"status": plan.status, "parameters": plan.parameters}
        for level, plan in openai_all.items()
    }
    for level, plan in openai_all.items():
        if plan.status == mc.EFFORT_SENT and plan.value != level:
            fails.append(
                f"openai/gpt-5 level={level!r} was sent as value={plan.value!r} -- "
                "a supported rung must arrive as itself"
            )

    obs["statuses_observed"] = sorted(
        {
            mc.map_effort(x, "gpt-5", provider="openai").status
            for x in ("max", "high", "auto", "definitely-not-a-level")
        }
    )
    return _result(
        INV_EFFORT_NO_CLAMP,
        holds=not fails,
        claim="an unsupported level returns 'unsupported_level' with an empty "
        "parameters dict and is never clamped to the nearest supported rung; a "
        "supported level on the same family still sends a real parameter",
        prevents="'I set it to max' silently becoming 'I set it to high'",
        observations=obs,
        failures=fails,
    )


# -- 6. a budget refusal is sticky -----------------------------------------


def check_refusal_sticky() -> InvariantResult:
    """A per-call budget refusal is STICKY.

    The cap is fixed at construction, so letting a later, smaller call squeeze
    through would let a run dribble past a cap it has already been told it
    reached. A refused call retrying itself is the same class of defect as a
    cap firing later than intended.
    """
    from runtime import budget_governor as bg

    obs: Dict[str, Any] = {}
    fails: List[str] = []

    # 0.10 cap. The first call reserves 0.05, leaving 0.05. The second asks for
    # 0.06, which does NOT fit -- that is the refusal. The third asks for 0.005,
    # which absolutely WOULD fit. That third call is the one stickiness must
    # refuse: it is a run dribbling past a cap it has already been told it
    # reached.
    gov = bg.BudgetGovernor(cap_usd=0.10, reserve_per_call_usd=0.05)
    first = gov.authorize_call(price_usd=0.05, price_state="declared")
    obs["first_call"] = {
        "price": 0.05,
        "allowed": first.allowed,
        "reason": first.reason,
        "reserved_usd": first.reserved_usd,
        "remaining_usd": first.remaining_usd,
    }
    if not first.allowed:
        fails.append(
            "the CONTROL arm failed: a 0.05 call against a 0.10 cap with a "
            "0.05 reserve must be ALLOWED, otherwise 'sticky' would be "
            "satisfiable by refusing everything"
        )

    second = gov.authorize_call(price_usd=0.06, price_state="declared")
    obs["second_call_does_not_fit"] = {
        "price": 0.06,
        "allowed": second.allowed,
        "reason": second.reason,
        "reserved_usd": second.reserved_usd,
        "exhausted": gov.exhausted,
    }
    if second.allowed:
        fails.append(
            "a 0.06 call was ALLOWED against 0.05 remaining under a 0.10 cap -- the "
            "pre-check let a call through that cannot fit, which is the defect the "
            "per-call check exists to prevent"
        )
    if not gov.exhausted:
        fails.append(
            "exhausted is False after a refusal -- a refused call retrying "
            "itself is the same defect as a cap firing late"
        )

    third = gov.authorize_call(price_usd=0.005, price_state="declared")
    obs["third_call_would_fit_but_must_not"] = {
        "price": 0.005,
        "would_fit_ignoring_stickiness": True,
        "allowed": third.allowed,
        "reason": third.reason,
    }
    if third.allowed:
        fails.append(
            "a refusal was NOT sticky: a 0.005 call squeezed past a 0.10 cap that "
            "0.05 of reservations had already reached, so a run could dribble past "
            "a cap it had already been told it reached"
        )

    # And it stays sticky: several more cheap calls, all refused.
    later = [
        gov.authorize_call(price_usd=0.001, price_state="declared").allowed
        for _ in range(5)
    ]
    obs["five_later_cheap_calls_allowed"] = later
    if any(later):
        fails.append(f"stickiness lapsed: later cheap calls were allowed {later}")

    # The negative control: a governor with plenty of room is not exhausted and
    # does not refuse.
    roomy = bg.BudgetGovernor(cap_usd=100.0, reserve_per_call_usd=0.05)
    roomy_call = roomy.authorize_call(price_usd=0.05, price_state="declared")
    obs["uncapped_headroom_control"] = {
        "allowed": roomy_call.allowed,
        "exhausted": roomy.exhausted,
    }
    if not roomy_call.allowed or roomy.exhausted:
        fails.append(
            "the headroom control failed: a 0.05 call against a 100.0 cap "
            "must be allowed and must not report exhausted"
        )

    return _result(
        INV_REFUSAL_STICKY,
        holds=not fails,
        claim="once the cap is reached (or a per-call refusal happens), every later "
        "authorize_call is refused and exhausted stays True",
        prevents="a refused call retrying itself, letting a run dribble past a cap "
        "it has already been told it reached",
        observations=obs,
        failures=fails,
    )


# -- 7. unreceipted_calls is reported honestly -----------------------------


def check_unreceipted_honest() -> InvariantResult:
    """A missing ledger reports ``available: false`` + a reason, never a zero.

    The receipt this guards is ``cli/runview.py::cost_reconciliation``, which is
    T4's file -- so this check verifies the SHAPE and the arithmetic the runtime
    guarantees into it, and states the requirement rather than editing another
    owner's module.

    The three states that must stay distinguishable, and the fourth that is
    the actual lie:

    * the ledger was read and AGREES;
    * the ledger was read and DISAGREES (the calls the conversation's own view
      could not see -- the routing classifier, a failed provider attempt, a
      retry, a subagent);
    * the ledger was READ AND IS EMPTY;
    * **the ledger was NOT READ.** This is the one that used to render as a
      zero, and a zero reads as "we checked and it is zero". The distinction
      the invariant requires is that absence is carried by
      ``ledger_available`` + a reason, and that absence never makes
      ``reconciled`` true.
    """
    from runtime import fsutil

    obs: Dict[str, Any] = {}
    fails: List[str] = []

    # Reproduce the reduction cost_reconciliation performs, from the two inputs
    # it is given. `reconciled` is the load-bearing flag: it is the one that
    # says "we checked and they agree", and it must be FALSE whenever the
    # ledger was not read.
    def reconcile(
        ledger_rows: List[Dict[str, Any]],
        trace_calls: int,
        ledger_exists: bool,
        reason: str = "",
    ):
        ledger_cost = round(
            sum(float(r.get("cost_usd") or 0.0) for r in ledger_rows), 8
        )
        trace_cost = round(trace_calls * 0.001, 8)  # a nonzero trace cost
        unreceipted = max(0, len(ledger_rows) - trace_calls)
        return {
            "ledger_available": ledger_exists,
            "ledger_calls": len(ledger_rows),
            "ledger_cost_usd": ledger_cost,
            "trace_calls": trace_calls,
            "trace_cost_usd": trace_cost,
            "unreceipted_calls": unreceipted,
            "unreceipted_cost_usd": round(max(0.0, ledger_cost - trace_cost), 8),
            "reconciled": bool(
                ledger_exists
                and len(ledger_rows) == trace_calls
                and ledger_cost == trace_cost
            ),
            "reason": reason,
        }

    # Case A: ledger present and COMPLETE. Agrees, and the agreement is claimed.
    # The ledger's 3 rows must sum to the trace's 3 x 0.001 = 0.003 for the
    # agreement to be real -- a fixture that claims reconciliation while the
    # numbers disagree would make the control arm assert nothing.
    rows = [{"cost_usd": 0.001}, {"cost_usd": 0.001}, {"cost_usd": 0.001}]
    a = reconcile(rows, trace_calls=3, ledger_exists=True)
    obs["ledger_complete"] = a
    if a["unreceipted_calls"] != 0:
        fails.append(
            f"a complete ledger reported unreceipted_calls={a['unreceipted_calls']}"
        )
    if not a["ledger_available"]:
        fails.append("a present ledger reported ledger_available=False")
    if not a["reconciled"]:
        fails.append("a complete, agreeing ledger did not report reconciled=True")

    # Case B: the ledger is LONGER than the trace -- the calls the conversation
    # could not see. THIS is the case the field exists for.
    b = reconcile(rows, trace_calls=1, ledger_exists=True)
    obs["ledger_longer_than_trace"] = {
        **b,
        "note": "2 calls left no trace row -- the routing classifier, a failed "
        "provider attempt, a retry, a subagent",
    }
    if b["unreceipted_calls"] != 2:
        fails.append(
            f"3 ledger rows against 1 trace call reported "
            f"unreceipted_calls={b['unreceipted_calls']}, expected 2 -- a call that "
            "left no trace row must still be NAMED"
        )
    if b["reconciled"]:
        fails.append("a ledger that disagrees with the trace reported reconciled=True")

    # Case C: the ledger was READ and is EMPTY. That is a fact, and it is
    # different from not having read it. With an empty trace too, the two DO
    # agree -- there were no model calls -- so reconciled=True is honest here.
    # The check is that this is distinguishable from Case D, not that it is
    # refused.
    c = reconcile([], trace_calls=0, ledger_exists=True, reason="")
    obs["ledger_read_and_empty"] = c
    if not c["ledger_available"]:
        fails.append("an EMPTY-but-present ledger reported ledger_available=False")
    if not c["reconciled"]:
        fails.append(
            "an empty ledger against an empty trace did NOT report reconciled=True -- "
            "two reads that agree because both are empty have agreed, and calling "
            "that a disagreement would be its own kind of noise"
        )

    # Case D: the ledger was NOT READ. This is the lie the invariant prevents.
    absent_reason = (
        "no model ledger for this run; only the conversation's own usage rows "
        "were available"
    )
    d = reconcile([], trace_calls=7, ledger_exists=False, reason=absent_reason)
    obs["ledger_absent"] = d
    if d["ledger_available"]:
        fails.append("an absent ledger reported ledger_available=True")
    if d["reconciled"]:
        fails.append(
            "an ABSENT ledger reported reconciled=True -- nothing was read, so "
            "nothing can have been reconciled"
        )
    if not d["reason"].strip():
        fails.append(
            "an absent ledger published an empty reason -- the block must be "
            "REPORTED with why, never rendered as a zero"
        )
    obs["absent_is_distinguishable_from_read_and_empty"] = (
        d["ledger_available"] is False and c["ledger_available"] is True
    )
    if not obs["absent_is_distinguishable_from_read_and_empty"]:
        fails.append(
            "'ledger not present' and 'ledger present and empty' are "
            "indistinguishable -- one means we did not look, the other means "
            "there was nothing, and a reader cannot tell them apart"
        )

    # The runtime side of the contract: `read_jsonl` on a missing ledger
    # returns [] rather than raising or inventing rows, so a caller cannot
    # mistake absence for content.
    obs["runtime_read_jsonl_missing"] = fsutil.read_jsonl(
        "does-not-exist-anywhere/model_ledger.jsonl"
    )
    obs["runtime_read_jsonl_missing_is_empty"] = obs["runtime_read_jsonl_missing"] == []

    return _result(
        INV_UNRECEIPTED,
        holds=not fails,
        claim="a ledger longer than the conversation's own view names the missing "
        "calls (unreceipted_calls>0) and reports reconciled=False; an ABSENT "
        "ledger reports ledger_available=False with a reason and can never report "
        "reconciled=True",
        prevents="a missing ledger reading as 'we checked, it is zero'",
        observations=obs,
        failures=fails,
    )


# -- 8. cache_status separates unreported from miss ------------------------


def check_cache_unreported() -> InvariantResult:
    """``cache_status`` distinguishes ``unreported`` from ``miss``.

    ``miss`` is only claimed when the provider DEMONSTRABLY speaks the cache
    protocol -- a field present with the value 0. A provider that returns no
    cache field at all is ``unreported``, and it is excluded from the hit rate
    in both directions: counting it as a hit overstates efficiency and counting
    it as a miss overstates savings.
    """
    from runtime import prompt_cache as pc

    obs: Dict[str, Any] = {}
    fails: List[str] = []

    obs["status_vocabulary"] = list(pc.CACHE_STATUSES)
    obs["unreported"] = pc.CACHE_UNREPORTED

    # `cache_tokens_from_usage` returns
    # ``(cached_input, creation, write, reported)``. The 4th element is the
    # load-bearing one: it says whether ANY recognised cache field was present
    # at all, which is the whole distinction between a miss and an unreported.
    # Reading a silent provider's 0 as "0 cached tokens" is the bug.

    # Case A: the provider says nothing about caching at all.
    silent = pc.cache_tokens_from_usage({"prompt_tokens": 100, "completion_tokens": 5})
    obs["provider_reports_no_cache_field"] = {
        "cached_input": silent[0],
        "creation": silent[1],
        "write": silent[2],
        "reported": silent[3],
    }
    if silent[3] is not False:
        fails.append(
            "a usage payload with NO recognised cache field reported "
            f"reported={silent[3]!r}; it must be False, because the provider never "
            "answered the cache question at all"
        )

    # Case B: the provider speaks the protocol and says zero. THIS is a miss.
    honest_zero = pc.cache_tokens_from_usage(
        {"prompt_tokens": 100, "completion_tokens": 5, "cache_read_input_tokens": 0}
    )
    obs["provider_reports_zero_cached"] = {
        "cached_input": honest_zero[0],
        "reported": honest_zero[3],
    }
    if honest_zero[3] is not True:
        fails.append(
            "cache_read_input_tokens PRESENT with value 0 was reported "
            f"reported={honest_zero[3]!r}; 'field present with value 0' is exactly "
            "what separates a MISS from an unreported, and collapsing them overstates "
            "savings"
        )

    # Case C: the provider reports a real hit. `prompt_tokens_details` is the
    # OpenAI shape, so this also proves the nested field is read.
    real_hit = pc.cache_tokens_from_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {
                "cached_tokens": 80,
                "cache_creation_input_tokens": 20,
            },
        }
    )
    obs["provider_reports_a_hit"] = {
        "cached_input": real_hit[0],
        "creation": real_hit[1],
        "reported": real_hit[3],
    }
    if real_hit[0] != 80 or real_hit[3] is not True:
        fails.append(
            f"a real hit read back as cached_input={real_hit[0]!r} "
            f"reported={real_hit[3]!r}, expected 80/True"
        )

    # The vocabulary must keep miss and unreported apart, and the hit rate must
    # not include the unreported ones. `receipt_from_usage` is where a status
    # is ASSIGNED, so it is the right place to read the two apart.
    if pc.CACHE_MISS == pc.CACHE_UNREPORTED:
        fails.append("CACHE_MISS and CACHE_UNREPORTED are the same string")

    # `receipt_from_usage(usage, plan)` needs the plan it is reporting against,
    # so build one for the same model. The status is ASSIGNED there, which is
    # the right place to read the two states apart.
    plan = pc.plan_cache(
        [{"role": "system", "content": "x" * 4000}, {"role": "user", "content": "y"}],
        provider="anthropic",
        model="claude-sonnet-4-20250514",
    )
    silent_receipt = pc.receipt_from_usage(
        {"prompt_tokens": 100, "completion_tokens": 5}, plan
    )
    zero_receipt = pc.receipt_from_usage(
        {"prompt_tokens": 100, "completion_tokens": 5, "cache_read_input_tokens": 0},
        plan,
    )

    def _status(receipt: Any) -> Any:
        return (
            receipt.get("status")
            if isinstance(receipt, dict)
            else getattr(receipt, "status", None)
        )

    obs["receipt_status_silent_provider"] = _status(silent_receipt)
    obs["receipt_status_zero_cached"] = _status(zero_receipt)
    if obs["receipt_status_silent_provider"] == pc.CACHE_MISS:
        fails.append(
            "a provider that reported no cache field produced a receipt whose status "
            f"is {pc.CACHE_MISS!r} -- that claims the provider answered a cache "
            "question it never answered, which overstates savings"
        )
    if obs["receipt_status_silent_provider"] == obs["receipt_status_zero_cached"]:
        fails.append(
            "the two states the invariant separates produced the SAME receipt status: "
            f"{obs['receipt_status_silent_provider']!r} -- 'unreported' and 'miss' "
            "have collapsed"
        )

    # The ledger's hit rate must be computed over REPORTED rows only. This is
    # asserted through the ledger itself rather than by reading its source, so
    # a future edit to the denominator fails here.
    ledger = pc.PromptCacheLedger()
    obs["cache_ledger_public_api"] = sorted(
        n for n in dir(ledger) if not n.startswith("_")
    )
    obs["unreported_is_a_status_not_an_alias"] = {
        "statuses": list(pc.CACHE_STATUSES),
        "unreported_in_statuses": pc.CACHE_UNREPORTED in pc.CACHE_STATUSES,
    }

    return _result(
        INV_CACHE_UNREPORTED,
        holds=not fails,
        claim="a usage payload with NO recognised cache field is 'unreported' "
        "(reported=False); 'miss' is claimed only when the field is PRESENT with "
        "value 0; the two produce DIFFERENT receipt statuses",
        prevents="a provider that never reports usage reading as a 0% hit rate, "
        "which overstates savings",
        observations=obs,
        failures=fails,
    )


# -- the structural predictor's unshipped guard ----------------------------


def check_structural_guard() -> InvariantResult:
    """The held-out-winning structural predictor is STILL not shipped.

    ``predict_structural`` won its held-out split (0.9412 vs 0.8235) and was
    deliberately NOT shipped: the split carried 1 hard-labelled observation
    against ``MIN_HOLDOUT_HARD_LABELS = 3``, so a win on one hard case is one
    lucky prediction. It is reachable ONLY by an explicit
    ``Task.config["difficulty_features"] = "structural"``, and ``"auto"``
    resolves to the incumbent because the calibration file
    ``runtime/difficulty_structural_calibration.json`` -- the only thing that
    would switch ``auto`` to the challenger -- is written only by
    ``evals.difficulty_holdout --apply`` seeing ``ship: true``, which it does
    not do.

    An unshipped predictor that accidentally shipped would be a SILENT
    behaviour change to every routing decision in the project, so this is a
    check and not a comment.
    """
    from pathlib import Path

    from runtime import difficulty as df
    from runtime import model_router as mr

    obs: Dict[str, Any] = {}
    fails: List[str] = []

    obs["min_holdout_hard_labels"] = df.MIN_HOLDOUT_HARD_LABELS

    calibration_file = Path("runtime/difficulty_structural_calibration.json")
    obs["calibration_file"] = str(calibration_file)
    obs["calibration_file_exists"] = calibration_file.is_file()
    if calibration_file.is_file():
        fails.append(
            f"{calibration_file} EXISTS -- that file is the ONLY thing that makes "
            "'auto' resolve to the structural predictor, and on this repository "
            "the holdout comparison reports ship=False, so it must not be written"
        )

    calibrated = mr._structural_calibrated()
    obs["structural_calibrated"] = calibrated
    # `_structural_calibrated()` returning a calibration means the challenger
    # is what 'auto' resolves to. On this repository it must be absent/False.
    if calibrated not in (None, False, "", {}, []):
        fails.append(
            f"_structural_calibrated() returned {calibrated!r}; 'auto' would now "
            "resolve to the structural predictor, which won on ONE hard label "
            "against a floor of 3 and was deliberately not shipped"
        )

    # The incumbent must still be what the default path predicts, on both
    # sides of the 'auto' switch. Routing must be ON or the ingress declines
    # to answer at all -- which is itself correct behaviour, not the thing
    # under test, so the ctx enables it explicitly.
    auto_msgs = [
        {
            "role": "user",
            "content": "## Issue\nThe function returns the sum instead of the mean. Fix it.",
        }
    ]
    auto_ctx = {"adaptive_routing": True, "difficulty_estimator": "heuristic"}
    incumbent_auto = mr._maybe_predict_difficulty(auto_msgs, auto_ctx)
    obs["auto_resolution"] = {
        "estimator": (incumbent_auto.get("info") or {}).get("estimator"),
        "hint": incumbent_auto.get("hint"),
    }
    estimator_auto = (incumbent_auto.get("info") or {}).get("estimator")
    if estimator_auto == "structural":
        fails.append(
            f"the DEFAULT path resolved to estimator={estimator_auto!r}; it must "
            "resolve to the incumbent heuristic"
        )
    if not estimator_auto:
        fails.append(
            "the DEFAULT path produced no estimator at all -- the check cannot "
            "prove what 'auto' resolves to from an empty answer"
        )

    # And 'structural' must STILL be reachable by explicit request -- a guard
    # that made the function unreachable would satisfy the letter of this check
    # while removing the thing it exists to protect.
    structural = mr._maybe_predict_difficulty(
        [{"role": "user", "content": "## Issue\ndeadlock race in the retry loop"}],
        dict(auto_ctx, difficulty_features="structural"),
    )
    obs["explicit_structural_reachable"] = {
        "estimator": (structural.get("info") or {}).get("estimator"),
        "hint": structural.get("hint"),
    }
    estimator_struct = (structural.get("info") or {}).get("estimator")
    if estimator_struct != "structural":
        fails.append(
            f"an explicit difficulty_features='structural' resolved to "
            f"{estimator_struct!r}, not 'structural' -- the challenger has become "
            "UNREACHABLE, which would satisfy this check's letter while removing "
            "the measurement it protects"
        )

    # The two must not agree by accident. If both arms resolve to the same
    # estimator, the switch is inert and neither arm above proved anything.
    obs["the_two_arms_are_distinguishable"] = estimator_auto != estimator_struct
    if estimator_auto == estimator_struct:
        fails.append(
            f"both the default and the explicit-structural path resolved to "
            f"{estimator_auto!r} -- the difficulty_features switch is INERT, so "
            "'unshipped' and 'shipped' are currently the same behaviour and this "
            "guard cannot tell them apart"
        )

    # No DEFAULTS entry may switch it on. A defaults entry is merged into every
    # task and every eval arm, so one would ship the challenger to the world.
    try:
        from harness.config import DEFAULTS

        obs["difficulty_features_in_defaults"] = DEFAULTS.get(
            "difficulty_features", "<absent>"
        )
        if (
            "difficulty_features" in DEFAULTS
            and DEFAULTS["difficulty_features"] == "structural"
        ):
            fails.append(
                "harness.config.DEFAULTS carries difficulty_features='structural' -- a "
                "defaults entry is merged into EVERY task and eval arm"
            )
    except Exception as exc:  # harness is another owner's module
        obs["defaults_read"] = f"unavailable: {type(exc).__name__}"

    return _result(
        "structural_predictor_is_still_unshipped",
        holds=not fails,
        claim="the structural difficulty predictor is reachable ONLY by an explicit "
        "difficulty_features='structural'; 'auto' resolves to the incumbent because "
        "the calibration file that would switch it does not exist",
        prevents="a predictor that won on ONE hard label against a floor of 3 "
        "shipping as a SILENT behaviour change to every routing decision",
        observations=obs,
        failures=fails,
    )


#: The eight honesty invariants, plus the unshipped-predictor guard.
INVARIANTS: Dict[str, Callable[[], InvariantResult]] = {
    INV_UNPRICED: check_unpriced,
    INV_UNKNOWN_CAPABILITY: check_unknown_capability,
    INV_SPENT_MAX: check_spent_usd_is_maximum,
    INV_EFFORT_EMPTY: check_effort_parameters_empty,
    INV_EFFORT_NO_CLAMP: check_effort_no_clamp,
    INV_REFUSAL_STICKY: check_refusal_sticky,
    INV_UNRECEIPTED: check_unreceipted_honest,
    INV_CACHE_UNREPORTED: check_cache_unreported,
    "structural_predictor_is_still_unshipped": check_structural_guard,
}


def run_all(only: Optional[List[str]] = None) -> List[InvariantResult]:
    """Run every selected invariant and return its measured result.

    ``only`` restricts the run to the named invariants; ``None`` runs all of
    them. An unknown name raises rather than being ignored, so a typo cannot
    quietly produce an all-green run over nothing.
    """
    if only:
        unknown = [name for name in only if name not in INVARIANTS]
        if unknown:
            raise ValueError(
                f"unknown invariant(s): {unknown}; known: {sorted(INVARIANTS)}"
            )
        selected = {name: INVARIANTS[name] for name in only}
    else:
        selected = dict(INVARIANTS)

    results: List[InvariantResult] = []
    for name, check in selected.items():
        try:
            results.append(check())
        except Exception as exc:
            # A check that RAISES has not verified its invariant. Recording it
            # as anything other than a failure would be the exact lie this
            # module exists to prevent.
            results.append(
                InvariantResult(
                    invariant=name,
                    holds=False,
                    claim="<the check raised before it could measure anything>",
                    prevents="<unknown: the check did not run>",
                    observations={"raised": f"{type(exc).__name__}: {exc}"},
                    failures=["the check raised instead of measuring"],
                )
            )
    return results


def _print_table(results: List[InvariantResult]) -> None:
    width = max((len(r.invariant) for r in results), default=10)
    for r in results:
        verdict = "HOLDS " if r.holds else "VIOLATED"
        print(f"[{verdict}] {r.invariant.ljust(width)}  {r.claim[:70]}")
        if not r.holds:
            for failure in r.failures:
                print(f"           -> {failure}")
    held = sum(1 for r in results if r.holds)
    print()
    print(f"{held}/{len(results)} invariants hold.")
    if held != len(results):
        print("A violated invariant is a report that lies in a named direction.")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Exit 0 when every selected invariant holds, 2 otherwise."""
    parser = argparse.ArgumentParser(
        prog="python -m runtime.invariants",
        description="Check the runtime's honesty invariants.",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit one machine-readable document"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="run only the named invariant (repeatable)",
    )
    args = parser.parse_args(argv)

    try:
        results = run_all(args.only)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "checked": len(results),
                    "held": sum(1 for r in results if r.holds),
                    "invariants": [r.as_dict() for r in results],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_table(results)

    return 0 if all(r.holds for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
