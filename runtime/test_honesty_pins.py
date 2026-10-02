"""Runtime-local pins for the eight honesty invariants (P0/W2 T3).

``runtime/invariants.py`` is the executable form of the eight rules. This
module is the *failing-if-broken* form: every test here asserts a live
behaviour of the module that actually serves a run, and every assertion is
paired with a CONTROL arm, because the single easiest way to satisfy "never
does X" is to never do anything at all.

Scope: ``runtime/**`` plus READ-ONLY imports of other owners' modules for the
cross-module contract surfaces (``cli.runview.cost_reconciliation`` and
``harness.config.DEFAULTS``). Nothing outside ``runtime/`` is edited.

These modules are NOT collected by a bare ``python -m pytest`` because
``pyproject.toml`` pins ``testpaths = ["tests"]``. Run them explicitly::

    python -m pytest runtime/test_honesty_pins.py -q

Requires no Docker, no provider, no network and no credential.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import pytest

from runtime import budget_governor as bg
from runtime import invariants as inv
from runtime import model_capabilities as mc
from runtime import prompt_cache as pc

# The closed effort families exercised by the sweep. `acme/no-knob` claims
# nothing at all, which is the arm that makes "parameters is empty unless
# sent" distinguishable from "parameters is empty because nothing was sent".
_EFFORT_FAMILIES: Tuple[Tuple[str, str], ...] = (
    ("openai", "gpt-5"),
    ("anthropic", "claude-sonnet-4-20250514"),
    ("google", "gemini-2.5-pro"),
    ("openai", "gpt-4o"),
    ("acme", "acme/no-knob"),
)

_EFFORT_LEVELS: Tuple[str, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "auto",
    "bogus",
)


# -- 1. unpriced is never $0 ------------------------------------------------


def test_an_undeclared_model_is_unpriced_and_the_price_state_travels() -> None:
    """Invariant 1: a bare 0.0 is ambiguous, so the STATE must accompany it."""
    estimate = mc.estimate_cost("acme/pin-never-declared", 1_000_000, 1_000_000)
    assert estimate.price_state == mc.PRICE_UNPRICED
    assert estimate.priced is False
    row = estimate.to_dict()
    assert row["price_state"] == mc.PRICE_UNPRICED
    assert row["cost_priced"] is False


def test_the_price_ladder_would_pick_the_unpriced_model_and_the_gate_refuses_it() -> (
    None
):
    """Invariant 1, directional: an absent row compared as 0.0 is the CHEAPEST.

    The hazard is asserted to be real before the mitigation is asserted to work,
    so the refusal cannot pass vacuously.
    """
    try:
        mc.register_capability(
            {
                "provider": "acme",
                "model": "pin-paid",
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
        )
        candidates = [
            {"provider": "acme", "model": "pin-never-declared"},
            {"provider": "acme", "model": "pin-paid"},
        ]
        by_number = sorted(
            candidates,
            key=lambda c: mc.estimate_cost(c["model"], 1_000_000, 1_000_000).cost_usd,
        )
        assert by_number[0]["model"] == "pin-never-declared", (
            "an unpriced model no longer compares as $0, so this test can no "
            "longer prove that the gate is what stops it being selected"
        )
        screened = mc.screen_candidates(candidates, allow_unpriced=False)
        assert screened.selected["model"] == "pin-paid"
        assert [r.model for r in screened.refusals] == ["pin-never-declared"]
        assert screened.refusals[0].reason == mc.REFUSAL_REASON_UNPRICED
        with pytest.raises(mc.CapabilityRoutingRefused) as caught:
            mc.screen_candidates(candidates[:1], allow_unpriced=False)
        assert caught.value.reason == mc.REFUSAL_REASON_UNPRICED
    finally:
        mc.unregister_capability("acme", "pin-paid")


def test_the_gate_allowing_unpriced_is_the_control_arm_not_a_vacuous_refusal() -> None:
    """Invariant 1 control: the gate refuses on price, not on everything."""
    candidates = [
        {"provider": "acme", "model": "pin-never-declared"},
        {"provider": "acme", "model": "pin-other-unpriced"},
    ]
    result = mc.screen_candidates(candidates, allow_unpriced=True)
    assert result.selected["model"] == "pin-never-declared"
    assert result.refusals == ()


def test_a_declared_free_model_is_free_and_stays_distinguishable_from_unpriced() -> (
    None
):
    """Invariant 1 control: without this arm, "cost is 0.0" proves nothing."""
    try:
        mc.register_capability(
            {
                "provider": "acme",
                "model": "pin-free",
                "input_cost_per_million": 0.0,
                "output_cost_per_million": 0.0,
            }
        )
        free = mc.estimate_cost("pin-free", 1_000_000, 1_000_000)
        unpriced = mc.estimate_cost("acme/pin-never-declared", 1_000_000, 1_000_000)
        assert free.price_state == mc.PRICE_FREE
        assert free.priced is True
        assert unpriced.price_state == mc.PRICE_UNPRICED
        assert unpriced.priced is False
        assert {mc.PRICE_PRICED, mc.PRICE_FREE, mc.PRICE_UNPRICED} == set(
            mc.PRICE_STATES
        )
    finally:
        mc.unregister_capability("acme", "pin-free")


# -- 2. unknown capability is never False ----------------------------------


def test_an_undeclared_capability_is_none_never_false() -> None:
    """Invariant 2: `None` means un-described; `False` means incapable."""
    capability = mc.capability_of("acme", "pin-never-declared")
    assert capability.supports_tools is None
    assert capability.known is False


def test_a_declared_false_stays_reachable_so_none_and_false_are_distinct() -> None:
    """Invariant 2 control: a registry that could not say False is useless."""
    try:
        mc.register_capability(
            {
                "provider": "acme",
                "model": "pin-no-tools",
                "supports_tools": False,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 1.0,
            }
        )
        declared = mc.capability_of("acme", "pin-no-tools")
        unknown = mc.capability_of("acme", "pin-never-declared")
        assert declared.supports_tools is False
        assert unknown.supports_tools is None
    finally:
        mc.unregister_capability("acme", "pin-no-tools")


def test_an_unknown_capability_does_not_refuse_the_world_by_default() -> None:
    """Invariant 2, directional: `False` refuses to route the entire world."""
    try:
        mc.register_capability(
            {
                "provider": "acme",
                "model": "pin-priced-ok",
                "supports_tools": None,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 1.0,
            }
        )
        candidates = [{"provider": "acme", "model": "pin-priced-ok"}]
        lenient = mc.screen_candidates(candidates, tool_driven=True, strict_tools=False)
        assert lenient.selected["model"] == "pin-priced-ok"
        assert mc.screen_candidates(candidates, tool_driven=True).refusals == ()
        with pytest.raises(mc.CapabilityRoutingRefused) as caught:
            mc.screen_candidates(candidates, tool_driven=True, strict_tools=True)
        assert caught.value.reason == mc.REFUSAL_REASON_TOOLS_UNKNOWN
    finally:
        mc.unregister_capability("acme", "pin-priced-ok")


def test_a_declared_incapable_model_is_refused_even_with_strict_tools_off() -> None:
    """Invariant 2 control: un-described is not the same as incapable."""
    try:
        mc.register_capability(
            {
                "provider": "acme",
                "model": "pin-no-tools",
                "supports_tools": False,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 1.0,
            }
        )
        with pytest.raises(mc.CapabilityRoutingRefused) as caught:
            mc.screen_candidates(
                [{"provider": "acme", "model": "pin-no-tools"}],
                tool_driven=True,
                strict_tools=False,
            )
        assert caught.value.reason == mc.REFUSAL_REASON_TOOLS
    finally:
        mc.unregister_capability("acme", "pin-no-tools")


# -- 3. spent_usd is a maximum ---------------------------------------------


def test_charges_whose_sum_exceeds_the_cap_report_the_cap_side_not_the_sum() -> None:
    """Invariant 3, directional: a sum fires the cap LATER than intended."""
    gov = bg.BudgetGovernor(cap_usd=0.10, spend_source=lambda: 0.06)
    gov.commit(0.06)
    assert abs(gov.spent_usd() - 0.06) < 1e-9
    assert 0.06 + 0.06 > 0.10 + 1e-9, "fixture no longer sums above the cap"
    later = gov.authorize_call(price_usd=0.05, price_state=mc.PRICE_PRICED)
    assert later.allowed is False, (
        "a 0.05 call fits under the cap but not under the cap plus a 0.06 "
        "reservation, so a sum-shaped spent_usd() would have allowed it"
    )


def test_the_governor_reports_its_own_charge_when_it_exceeds_the_bound() -> None:
    """Invariant 3: the maximum is symmetric, not one-sided."""
    gov = bg.BudgetGovernor(cap_usd=100.0, spend_source=lambda: 0.25)
    gov.commit(4.00)
    assert abs(gov.spent_usd() - 4.00) < 1e-9


def test_a_bound_that_raises_can_never_disable_the_cap() -> None:
    """Invariant 3: an unreadable bound is not a bound of zero."""

    def exploding_bound() -> float:
        raise RuntimeError("the bound source is unavailable")

    gov = bg.BudgetGovernor(cap_usd=100.0, spend_source=exploding_bound)
    gov.commit(2.00)
    assert abs(gov.spent_usd() - 2.00) < 1e-9


def test_no_bound_at_all_reports_the_governor_own_charges() -> None:
    """Invariant 3 control: max(own, 0) is the same number as own."""
    gov = bg.BudgetGovernor(cap_usd=100.0)
    gov.commit(3.00)
    assert abs(gov.spent_usd() - 3.00) < 1e-9


# -- 4. EffortPlan.parameters is empty unless sent -------------------------


def _effort_sweep() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for provider, model in _EFFORT_FAMILIES:
        for level in _EFFORT_LEVELS:
            plan = mc.map_effort(level, model, provider=provider)
            rows.append(
                {
                    "provider": provider,
                    "model": model,
                    "level": level,
                    "status": plan.status,
                    "sent": plan.sent,
                    "parameters": plan.parameters,
                }
            )
    disabled = mc.map_effort("high", "gpt-5", provider="openai", parameter="none")
    rows.append(
        {
            "provider": "openai",
            "model": "gpt-5",
            "level": "high",
            "status": disabled.status,
            "sent": disabled.sent,
            "parameters": disabled.parameters,
        }
    )
    synthetic = mc.synthetic_effort_plan("high", "gpt-5", "openai")
    rows.append(
        {
            "provider": "openai",
            "model": "gpt-5",
            "level": "high",
            "status": synthetic.status,
            "sent": synthetic.sent,
            "parameters": synthetic.parameters,
        }
    )
    return rows


def test_every_effort_status_but_sent_produces_no_parameters() -> None:
    """Invariant 4: a non-sent status is structurally unable to send."""
    offenders = {
        f"{row['provider']}/{row['model']}:{row['level']}": row
        for row in _effort_sweep()
        if not row["sent"] and row["parameters"]
    }
    assert offenders == {}


def test_a_sent_status_always_carries_a_parameter() -> None:
    """Invariant 4 control: the reverse lie is a lie too."""
    silent = [row for row in _effort_sweep() if row["sent"] and not row["parameters"]]
    assert silent == []


def test_every_declared_effort_status_is_actually_reachable() -> None:
    """Anti-vacuity: a status nothing can produce is untested vocabulary."""
    observed = {row["status"] for row in _effort_sweep()}
    assert set(mc.EFFORT_STATUSES) <= observed


def test_every_row_agrees_between_sent_and_its_own_status() -> None:
    """Invariant 4: `sent` is a projection of `status`, not a second opinion."""
    mismatched = [
        row
        for row in _effort_sweep()
        if row["sent"] != (row["status"] == mc.EFFORT_SENT)
    ]
    assert mismatched == []


# -- 5. an unsupported effort level is never clamped -----------------------


def test_an_unsupported_level_is_refused_and_sends_nothing() -> None:
    """Invariant 5: `max` on a three-level family must not become `high`."""
    plan = mc.map_effort("max", "gpt-5", provider="openai")
    assert plan.status == mc.EFFORT_UNSUPPORTED_LEVEL
    assert plan.parameters == {}
    assert plan.sent is False


def test_a_supported_level_beside_it_still_sends_its_real_parameter() -> None:
    """Invariant 5 control: otherwise the ladder could send nothing at all."""
    plan = mc.map_effort("high", "gpt-5", provider="openai")
    assert plan.status == mc.EFFORT_SENT
    assert plan.parameters == {"reasoning_effort": "high"}


def test_a_level_that_is_not_a_rung_is_invalid_not_clamped() -> None:
    """Invariant 5: `invalid` is a fourth outcome, distinct from unsupported."""
    assert (
        mc.map_effort("minimal", "gpt-5", provider="openai").status == mc.EFFORT_INVALID
    )
    assert (
        mc.map_effort("definitely-not-a-level", "gpt-5", provider="openai").status
        == mc.EFFORT_INVALID
    )


# -- 6. a budget refusal is sticky -----------------------------------------


def test_a_refused_call_never_retries_itself() -> None:
    """Invariant 6: a run may not dribble past a cap it has been told it hit."""
    gov = bg.BudgetGovernor(
        cap_usd=0.05, reserve_per_call_usd=0.05, rate_limit_retries=3
    )
    first = gov.authorize_call(price_usd=0.05, price_state=mc.PRICE_PRICED)
    assert first.allowed is True
    gov.commit(0.05, model="acme/pin")
    assert gov.exhausted is True
    refused = [
        gov.authorize_call(price_usd=0.005, price_state=mc.PRICE_PRICED)
        for _ in range(4)
    ]
    assert all(verdict.allowed is False for verdict in refused)
    assert all(verdict.reason for verdict in refused)
    assert refused[-1].price_state == "cap_reached"


def test_a_refusal_is_recorded_on_the_receipt_not_only_returned() -> None:
    """Invariant 6 control: a refusal nobody can see is not a refusal."""
    gov = bg.BudgetGovernor(cap_usd=0.05, reserve_per_call_usd=0.05)
    gov.authorize_call(price_usd=0.05, price_state=mc.PRICE_PRICED)
    gov.commit(0.05, model="acme/pin")
    gov.authorize_call(price_usd=0.001, price_state=mc.PRICE_PRICED)
    gov.authorize_call(price_usd=0.001, price_state=mc.PRICE_PRICED)
    report = gov.report()
    assert report["exhausted"] is True
    assert report["calls_refused"] >= 1
    denied = [row for row in report["recent"] if not row["allowed"]]
    assert len(denied) == 2
    assert all(row["reason"] for row in denied)


# -- 7. unreceipted calls are reported honestly ----------------------------


def test_a_missing_ledger_reports_available_false_with_a_reason(tmp_path: Any) -> None:
    """Invariant 7, directional: a missing ledger must never read as $0."""
    from cli import runview

    receipt = runview.cost_reconciliation(str(tmp_path), "pin-task")
    assert receipt["ledger_available"] is False
    assert receipt["reason"]
    assert receipt["reconciled"] is False


def test_a_present_but_empty_ledger_is_distinguishable_from_a_missing_one(
    tmp_path: Any,
) -> None:
    """Invariant 7 control: "checked and empty" is not "could not check"."""
    from cli import runview

    (tmp_path / "pin-task.runtime").mkdir(parents=True)
    (tmp_path / "pin-task.runtime" / "model_ledger.jsonl").write_text(
        "", encoding="utf-8"
    )
    receipt = runview.cost_reconciliation(str(tmp_path), "pin-task")
    assert receipt["ledger_available"] is True
    assert receipt["reason"] == ""
    assert receipt["ledger_calls"] == 0
    assert receipt["unreceipted_calls"] == 0


def test_a_ledger_the_conversation_cannot_see_is_counted_as_unreceipted(
    tmp_path: Any,
) -> None:
    """Invariant 7, the positive arm: the receipt names what it could not see."""
    from cli import runview

    (tmp_path / "pin-task").mkdir(parents=True)
    (tmp_path / "pin-task" / "trace.jsonl").write_text(
        json.dumps(
            {
                "kind": "model_response",
                "data": {
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "pin-task.runtime").mkdir(parents=True)
    (tmp_path / "pin-task.runtime" / "model_ledger.jsonl").write_text(
        json.dumps(
            {
                "model": "acme/pin",
                "provider": "acme",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost_usd": 0.01,
                "priced": True,
            }
        )
        + "\n"
        + json.dumps(
            {
                "model": "acme/pin",
                "provider": "acme",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost_usd": 0.02,
                "priced": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = runview.cost_reconciliation(str(tmp_path), "pin-task")
    assert receipt["ledger_calls"] == 2
    assert receipt["trace_calls"] == 1
    assert receipt["unreceipted_calls"] == 1
    assert receipt["reconciled"] is False


# -- 8. unreported is not miss ---------------------------------------------


def test_a_provider_that_reports_no_cache_field_is_unreported_not_a_miss() -> None:
    """Invariant 8: silence is not a zero, in either direction."""
    cached, creation, write, reported = pc.cache_tokens_from_usage(
        {"prompt_tokens": 40}
    )
    assert (cached, creation, write) == (0, 0, 0)
    assert reported is False
    plan = pc.plan_cache(
        [{"role": "system", "content": "x" * 6000}],
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        min_prefix_tokens=1,
    )
    assert plan.requested is True
    receipt = pc.receipt_from_usage({"prompt_tokens": 40}, plan)
    assert receipt.status == pc.CACHE_UNREPORTED
    assert receipt.decided is False


def test_an_explicit_zero_cache_field_is_a_real_miss() -> None:
    """Invariant 8 control: without this arm the two states are indistinguishable."""
    usage = {"prompt_tokens": 40, "prompt_tokens_details": {"cached_tokens": 0}}
    cached, _creation, _write, reported = pc.cache_tokens_from_usage(usage)
    assert cached == 0
    assert reported is True
    plan = pc.plan_cache(
        [{"role": "system", "content": "x" * 6000}],
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        min_prefix_tokens=1,
    )
    receipt = pc.receipt_from_usage(usage, plan)
    assert receipt.status == pc.CACHE_MISS
    assert receipt.decided is True


def test_the_cache_ledger_keeps_unreported_out_of_the_hit_rate_denominator() -> None:
    """Invariant 8, directional: a silent provider must read as no rate at all."""
    ledger = pc.PromptCacheLedger()
    plan = pc.plan_cache(
        [{"role": "system", "content": "x" * 6000}],
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        min_prefix_tokens=1,
    )
    ledger.record(plan, pc.receipt_from_usage({"prompt_tokens": 40}, plan))
    summary = ledger.summary()
    assert summary["cache_calls"] == 1
    assert summary["cache_decided_calls"] == 0
    assert summary["cache_status_counts"] == {pc.CACHE_UNREPORTED: 1}
    assert pc.CACHE_MISS not in summary["cache_status_counts"]
    assert summary["cache_hit_rate"] == 0.0


def test_a_real_miss_makes_the_denominator_one_so_the_two_summaries_differ() -> None:
    """Invariant 8 control: the discriminator is the denominator, not the rate."""
    ledger = pc.PromptCacheLedger()
    plan = pc.plan_cache(
        [{"role": "system", "content": "x" * 6000}],
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        min_prefix_tokens=1,
    )
    usage = {"prompt_tokens": 40, "prompt_tokens_details": {"cached_tokens": 0}}
    ledger.record(plan, pc.receipt_from_usage(usage, plan))
    summary = ledger.summary()
    assert summary["cache_hit_rate"] == 0.0
    assert summary["cache_decided_calls"] == 1
    assert summary["cache_status_counts"] == {pc.CACHE_MISS: 1}


# -- the docstrings themselves are load-bearing ---------------------------


def test_invariant_one_names_the_direction_it_prevents() -> None:
    """A rule that does not say which way it errs cannot be reviewed."""
    doc = mc.__doc__ or ""
    assert "spends money" in doc
    assert "cheapest" in doc
    assert "cheapest" in (inv.check_unpriced.__doc__ or "")


def test_invariant_two_names_the_direction_it_prevents() -> None:
    """Invariant 2's docstring must say what `False` would cost."""
    doc = inv.check_unknown_capability.__doc__ or ""
    assert "refuses to route the entire world" in doc
    assert "None" in doc and "False" in doc


def test_no_capability_key_is_a_shipped_harness_default() -> None:
    """A DEFAULTS entry is merged into every task and every eval arm."""
    from harness.config import DEFAULTS

    for key in (
        "capability_gate",
        "capability_allow_unpriced",
        "capability_strict_tools",
        "capability_tool_driven",
    ):
        assert key not in DEFAULTS, (
            f"{key} is in harness.config.DEFAULTS; a value there is merged into "
            "every task config and every eval arm and would switch the gate on "
            "for runs that never asked for it"
        )


def test_no_budget_governor_key_is_a_shipped_harness_default() -> None:
    """Invariant 7's counterpart: a budget default cannot arrive from DEFAULTS."""
    from harness.config import DEFAULTS

    for key in ("budget_reserve_per_call_usd", "max_completion_tokens"):
        assert key not in DEFAULTS


# -- the executable check module itself -----------------------------------


def test_the_invariant_module_reports_nine_checks_all_holding_with_evidence() -> None:
    """`runtime/invariants.py` is the receipt; this pins its own count."""
    results = inv.run_all()
    assert len(results) == 9
    for result in results:
        assert result.holds is True, (result.invariant, result.failures)
        assert result.observations, result.invariant
        assert result.prevents, result.invariant


def test_the_invariant_names_are_the_eight_published_ones_plus_the_guard() -> None:
    """A renamed invariant silently unpins every check that named it."""
    assert set(inv.INVARIANTS) == {
        inv.INV_UNPRICED,
        inv.INV_UNKNOWN_CAPABILITY,
        inv.INV_SPENT_MAX,
        inv.INV_EFFORT_EMPTY,
        inv.INV_EFFORT_NO_CLAMP,
        inv.INV_REFUSAL_STICKY,
        inv.INV_UNRECEIPTED,
        inv.INV_CACHE_UNREPORTED,
        "structural_predictor_is_still_unshipped",
    }


def test_the_cli_receipt_and_the_module_disagree_if_the_count_drifts() -> None:
    """`main()` is the operator surface; it must report the same nine."""
    results = inv.run_all()
    assert len(results) == len(inv.INVARIANTS)
    assert all(result.invariant in inv.INVARIANTS for result in results)
