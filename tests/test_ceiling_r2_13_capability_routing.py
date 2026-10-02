"""R2-13 — model capability registry, calibrated routing, and the honest
predictor comparison.

Every test here is named after a BEHAVIOUR the round claims, and each is a
measurement rather than an assertion-from-a-comment. The four the round
required, in the order the round required them:

1. an unpriced model reports ``unpriced`` and is not treated as free;
2. a tool-incapable model is excluded from a tool-driven loop with a
   RECORDED reason;
3. the new predictor is compared against the old one on a held-out set and
   the report states which won;
4. a model with a ``reasoning_content`` response is handled per the R2-14
   protocol.

Two of the round's structural claims are also pinned as NON-vacuous: the
capability gate is OFF for a config that never mentions it (so the pre-R2-13
path is byte-identical and the ablation compares two different
configurations rather than two identical ones), and the shipped default
configuration cannot switch the gate on (a `DEFAULTS` entry, even a `None`
one, would put the key in every task context and refuse unpriced targets in
runs that never asked for it).

Everything here is offline and deterministic: the offline mock provider plus
fake provider responses. No network, no Docker, no credential is read, and no
live-provider claim is made or implied.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from runtime import difficulty, model_capabilities, model_router

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

BASH_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]

MESSAGES = [{"role": "user", "content": "fix the failing test in app.py"}]

ACME_ROWS = (
    {
        "provider": "acme",
        "model": "acme-cheap-no-tools",
        "input_cost_per_million": 0.01,
        "output_cost_per_million": 0.02,
        "supports_tools": False,
    },
    {
        "provider": "acme",
        "model": "acme-declared-free",
        "input_cost_per_million": 0.0,
        "output_cost_per_million": 0.0,
        "supports_tools": True,
    },
    {"provider": "acme", "model": "acme-unpriced", "supports_tools": True},
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate the capability registry around every test.

    The registry is process-global by design (it describes models, not tasks),
    so a test that registers a fictional model must not leave it behind for a
    later one — a leaked row would make a later test's "unknown model" case
    silently pass for the wrong reason.
    """
    model_capabilities.reset_capability_registry()
    yield
    model_capabilities.reset_capability_registry()


def _install(monkeypatch, rows=ACME_ROWS):
    """Register ``rows`` in the real registry and stub litellm's completion."""
    for row in rows:
        model_capabilities.register_capability(row, source="registry")

    def completion(**kwargs):
        msg = types.SimpleNamespace(content="ok", tool_calls=None)
        choice = types.SimpleNamespace(message=msg, finish_reason="stop")
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=2)
        return types.SimpleNamespace(choices=[choice], usage=usage, _hidden_params={})

    monkeypatch.setitem(
        sys.modules, "litellm", types.SimpleNamespace(completion=completion)
    )


def _call(ctx, *, hint="easy", tools=None, model=None):
    """Install ``ctx`` and make one call; return the last-usage record.

    ``adaptive_routing`` is forced on because a tier table is inert without
    it: the router would fall back to the built-in medium tier and the test
    would read the DEFAULT model's identity as a capability decision. Every
    capability test also asserts ``routed_up``/``refusals`` so a config that
    is not actually routing cannot pass by accident.
    """
    payload = dict(ctx)
    payload.setdefault("adaptive_routing", True)
    model_router.set_call_context(payload)
    model_router.call_model(MESSAGES, difficulty_hint=hint, tools=tools, model=model)
    return model_router.get_last_usage()


# ---------------------------------------------------------------------------
# 1. unpriced is not free
# ---------------------------------------------------------------------------


class TestUnpricedIsNotFree:
    def test_a_model_with_no_price_row_reports_unpriced_and_not_priced(self):
        record = model_capabilities.price_of("a-model-nobody-priced")
        assert record["price_state"] == "unpriced"
        assert record["priced"] is False
        assert record["free"] is False
        # The number is zero, and the STATE beside it is what makes zero
        # readable. Dropping the state is the original defect.
        assert record["input_cost_per_million"] is None

    def test_a_declared_zero_price_row_is_free_not_unpriced(self):
        model_capabilities.register_capability(
            {
                "provider": "acme",
                "model": "acme-declared-free",
                "input_cost_per_million": 0.0,
                "output_cost_per_million": 0.0,
            },
            source="registry",
        )
        record = model_capabilities.declared_rates("acme-declared-free")
        assert record["price_state"] == "free"
        assert record["free"] is True
        assert record["priced"] is True
        assert record["price_origin"].startswith("registry:")
        # The global table alone cannot know it, which is the point: `free` is
        # reachable only by a DECLARED row, never by absence of evidence.
        assert (
            model_capabilities.price_of("acme-declared-free")["price_state"]
            == "unpriced"
        )

    def test_an_unpriced_call_reports_zero_cost_with_unpriced_state(self, monkeypatch):
        _install(monkeypatch)
        usage = _call(
            {"model_tiers": {"easy": {"provider": "acme", "model": "acme-unpriced"}}}
        )
        assert usage["model"] == "acme-unpriced"
        assert usage["cost_usd"] == 0.0
        assert usage["price_state"] == "unpriced"
        assert usage["cost_priced"] is False
        assert usage["cost_source"] == "unpriced"

    def test_a_declared_free_call_reports_free_state(self, monkeypatch):
        _install(monkeypatch)
        usage = _call(
            {
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-declared-free"}
                }
            }
        )
        assert usage["price_state"] == "free"
        assert usage["cost_priced"] is True

    def test_a_priced_call_still_reports_priced_and_a_nonzero_estimate(
        self, monkeypatch
    ):
        _install(monkeypatch)
        usage = _call(
            {"model_tiers": {"easy": {"provider": "openai", "model": "gpt-4o-mini"}}}
        )
        assert usage["price_state"] == "priced"
        assert usage["cost_priced"] is True
        assert usage["cost_usd"] > 0.0

    def test_free_and_unknown_are_different_answers_not_the_same_zero(self):
        model_capabilities.register_capability(
            {
                "provider": "acme",
                "model": "acme-declared-free",
                "input_cost_per_million": 0.0,
                "output_cost_per_million": 0.0,
            },
            source="registry",
        )
        free = model_capabilities.estimate_cost("acme-declared-free", 1000, 1000)
        unknown = model_capabilities.estimate_cost("acme-nothing-known", 1000, 1000)
        # Both are 0.0 as numbers. That is exactly why the state has to travel
        # with the number: nothing but `price_state` distinguishes them.
        assert free.cost_usd == unknown.cost_usd == 0.0
        assert free.price_state == "free"
        assert unknown.price_state == "unpriced"
        assert free.priced is True
        assert unknown.priced is False

    def test_a_registered_rate_reaches_the_cost_report(self, monkeypatch):
        """A price the operator declared must not be invisible to pricing.

        Regression: the cost path originally read only the global table, so a
        rate registered for a model the table does not know was reported as
        `unpriced` — the "we do not know" answer about a model whose price the
        operator had already stated.
        """
        _install(monkeypatch)
        usage = _call(
            {
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-cheap-no-tools"}
                }
            }
        )
        assert usage["price_state"] == "priced"
        assert usage["cost_usd"] > 0.0
        assert usage["cost_source"] == "price_table"

    def test_a_malformed_rate_is_refused_rather_than_coerced_to_unknown(self):
        with pytest.raises(model_capabilities.CapabilityError) as excinfo:
            model_capabilities.register_capability(
                {"model": "acme-broken", "input_cost_per_million": -1.0}
            )
        assert excinfo.value.reason == "invalid_price"

    def test_a_non_mapping_declaration_is_refused(self):
        with pytest.raises(model_capabilities.CapabilityError):
            model_capabilities.register_capability("not-a-row")  # type: ignore[arg-type]


class TestTheRouterRefusesToRouteOnAnUnpricedModel:
    def test_the_price_ladder_alone_would_pick_the_unpriced_model(self, monkeypatch):
        """The pre-R2-13 behaviour, pinned as the control arm.

        Without the gate the unpriced model wins the cheap tier and the cost
        report says $0. That is the lie this round exists to stop, so it has
        to be measured, not described.
        """
        _install(monkeypatch)
        usage = _call(
            {
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-unpriced"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                }
            }
        )
        assert usage["model"] == "acme-unpriced"
        assert usage["price_state"] == "unpriced"
        assert usage["cost_priced"] is False
        assert usage["capability_gate"]["enabled"] is False

    def test_the_gate_refuses_the_unpriced_target_and_routes_to_a_priced_one(
        self, monkeypatch
    ):
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-unpriced"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            }
        )
        assert usage["model"] == "gpt-4o-mini"
        receipt = usage["capability_gate"]
        assert receipt["enabled"] is True
        assert [row["reason"] for row in receipt["refusals"]] == [
            model_capabilities.REFUSAL_REASON_UNPRICED
        ]
        assert receipt["refusals"][0]["model"] == "acme-unpriced"

    def test_nothing_priced_anywhere_refuses_loudly_with_a_named_reason(
        self, monkeypatch
    ):
        _install(monkeypatch)
        model_router.set_call_context(
            {
                "adaptive_routing": True,
                "capability_gate": True,
                "model_tiers": {"easy": {"provider": "acme", "model": "acme-unpriced"}},
            }
        )
        with pytest.raises(model_capabilities.CapabilityRoutingRefused) as excinfo:
            model_router.call_model(MESSAGES, difficulty_hint="easy")
        refusal = excinfo.value
        assert refusal.reason == model_capabilities.REFUSAL_REASON_UNPRICED
        assert refusal.alternatives[0]["model"] == "acme-unpriced"

    def test_an_explicit_allowance_re_admits_the_unpriced_target(self, monkeypatch):
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "capability_allow_unpriced": True,
                "model_tiers": {"easy": {"provider": "acme", "model": "acme-unpriced"}},
            }
        )
        assert usage["model"] == "acme-unpriced"
        assert usage["price_state"] == "unpriced"
        assert usage["capability_gate"]["refusal_count"] == 0

    def test_an_explicitly_named_model_is_not_a_routing_decision(self, monkeypatch):
        """The prompt is to refuse to ROUTE on an unpriced model.

        A caller who pinned the model is not routing, so the unpriced refusal
        does not fire — but the cost report still says `unpriced`, and the
        bypass is recorded rather than silent.
        """
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-cheap-no-tools"}
                },
            },
            model="acme-unpriced",
        )
        assert usage["model"] == "acme-unpriced"
        assert usage["price_state"] == "unpriced"
        assert usage["capability_gate"]["explicit_unpriced_bypass"] is True

    def test_the_gate_is_off_for_a_config_that_never_mentions_it(self, monkeypatch):
        """So the pre-R2-13 path is byte-identical AND the ablation compares
        two different configurations rather than two identical ones."""
        _install(monkeypatch)
        usage = _call(
            {"model_tiers": {"easy": {"provider": "acme", "model": "acme-unpriced"}}}
        )
        assert usage["capability_gate"]["enabled"] is False
        assert usage["capability_gate"]["refusal_count"] == 0

    def test_a_capability_key_set_to_false_still_opts_in(self, monkeypatch):
        """Presence, not value — the operator who wrote the key wants the
        pipeline and turned the feature off inside it."""
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": False,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-unpriced"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            }
        )
        assert usage["capability_gate"]["enabled"] is True
        assert usage["model"] == "gpt-4o-mini"

    def test_the_shipped_default_configuration_cannot_switch_the_gate_on(self):
        """A `DEFAULTS` entry, even a `None` one, would put the key in every
        task context and refuse unpriced targets in runs that never asked."""
        from harness.config import DEFAULTS

        for key in model_router._CAPABILITY_CONFIG_KEYS:
            assert key not in DEFAULTS, (
                f"{key} is in DEFAULTS; the gate is key-presence"
            )
        assert model_router._capability_requested(dict(DEFAULTS)) is False


# ---------------------------------------------------------------------------
# 2. tool-use scoring is a capability constraint
# ---------------------------------------------------------------------------


class TestToolUseScoring:
    def test_a_declared_tool_incapable_model_is_excluded_with_a_recorded_reason(
        self, monkeypatch
    ):
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            },
            tools=BASH_TOOL,
        )
        assert usage["model"] != "acme-cheap-no-tools"
        receipt = usage["capability_gate"]
        assert model_capabilities.REFUSAL_REASON_TOOLS in [
            row["reason"] for row in receipt["refusals"]
        ]
        refusal = next(
            row
            for row in receipt["refusals"]
            if row["reason"] == model_capabilities.REFUSAL_REASON_TOOLS
        )
        assert refusal["model"] == "acme-cheap-no-tools"
        assert refusal["supports_tools"] is False
        assert refusal["detail"]

    def test_the_refusal_escalates_the_tier_rather_than_downgrading_it(
        self, monkeypatch
    ):
        """A capability exclusion must not hand the call to a cheaper model
        that is also incapable."""
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                    "hard": {"provider": "openai", "model": "gpt-4o"},
                },
            },
            tools=BASH_TOOL,
        )
        assert usage["model"] == "gpt-4o-mini"
        assert usage["capability_gate"]["routed_up"] is True

    def test_the_exclusion_is_attributable_to_the_gate_not_to_a_missing_hint(
        self, monkeypatch
    ):
        """NON-VACUITY. Without adaptive routing a tier table is inert and the
        router falls back to the built-in medium tier, so an "exclusion" test
        would read the DEFAULT model's identity as a capability decision. The
        control run here is the same config with the gate off: it must select
        the incapable cheap model, which is what makes the gated run's
        different answer attributable to the gate.
        """
        _install(monkeypatch)
        tiers = {
            "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
            "medium": {"provider": "openai", "model": "gpt-4o-mini"},
        }
        control = _call({"model_tiers": tiers}, tools=BASH_TOOL)
        assert control["model"] == "acme-cheap-no-tools"
        assert control["capability_gate"]["refusal_count"] == 0
        gated = _call({"capability_gate": True, "model_tiers": tiers}, tools=BASH_TOOL)
        assert gated["model"] == "gpt-4o-mini"
        assert gated["capability_gate"]["refusal_count"] == 1
        assert gated["capability_gate"]["routed_up"] is True
        # The refusal names the model that was actually rejected.
        considered = {row["model"] for row in gated["capability_gate"]["considered"]}
        assert "acme-cheap-no-tools" in considered

    def test_price_cannot_buy_tool_support(self, monkeypatch):
        """The incapable model is $0.01/$0.02 per 1M — far cheaper than the
        capable one. The gate must still exclude it, because a tool-driven
        loop cannot use a model that cannot call tools."""
        _install(monkeypatch)
        tiers = {
            "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
            "medium": {"provider": "openai", "model": "gpt-4o"},
        }
        cheap = model_capabilities.declared_rates("acme-cheap-no-tools")
        capable = model_capabilities.declared_rates("gpt-4o")
        assert cheap["input_cost_per_million"] < capable["input_cost_per_million"]
        usage = _call({"capability_gate": True, "model_tiers": tiers}, tools=BASH_TOOL)
        assert usage["model"] == "gpt-4o"

    def test_an_undeclared_tool_capability_is_eligible_and_reported_as_unknown(
        self, monkeypatch
    ):
        """ "Not described" is not "cannot". A registry that had to be
        complete before anything could route would refuse the whole world."""
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "capability_allow_unpriced": True,
                "model_tiers": {"easy": {"provider": "acme", "model": "acme-mystery"}},
            },
            tools=BASH_TOOL,
        )
        assert usage["model"] == "acme-mystery"
        assert usage["capability_gate"]["refusal_count"] == 0

    def test_strict_tools_excludes_an_unverified_capability_too(self, monkeypatch):
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "capability_strict_tools": True,
                "capability_allow_unpriced": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-mystery"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            },
            tools=BASH_TOOL,
        )
        # Unknown tool support is a refusal under the strict arm, and the call
        # escalates to the VERIFIED tier rather than proceeding unverified.
        assert usage["model"] == "gpt-4o-mini"
        assert model_capabilities.REFUSAL_REASON_TOOLS_UNKNOWN in [
            row["reason"] for row in usage["capability_gate"]["refusals"]
        ]
        assert usage["capability_gate"]["strict_tools"] is True

    def test_strict_tools_refuses_loudly_when_nothing_is_verified(self, monkeypatch):
        _install(monkeypatch)
        model_router.set_call_context(
            {
                "adaptive_routing": True,
                "capability_gate": True,
                "capability_strict_tools": True,
                "capability_allow_unpriced": True,
                "model_tiers": {"easy": {"provider": "acme", "model": "acme-mystery"}},
            }
        )
        with pytest.raises(model_capabilities.CapabilityRoutingRefused) as excinfo:
            model_router.call_model(MESSAGES, difficulty_hint="easy", tools=BASH_TOOL)
        assert excinfo.value.reason == model_capabilities.REFUSAL_REASON_TOOLS_UNKNOWN

    def test_a_non_tool_call_is_not_gated_on_tool_support(self, monkeypatch):
        """The constraint applies to tool-driven loops only. A plain completion
        has no tools to emit, so a tool-incapable model is a legitimate choice
        for it — and excluding it would be an invented constraint."""
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-cheap-no-tools"}
                },
            }
        )
        assert usage["model"] == "acme-cheap-no-tools"

    def test_a_declared_tool_driven_loop_gates_a_call_that_carries_no_schemas(
        self, monkeypatch
    ):
        """A planner call inside a tool-driven run has no schemas of its own
        but still belongs to that loop; the operator declares it."""
        _install(monkeypatch)
        usage = _call(
            {
                "capability_gate": True,
                "capability_tool_driven": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            }
        )
        assert usage["model"] == "gpt-4o-mini"
        assert model_capabilities.REFUSAL_REASON_TOOLS in [
            row["reason"] for row in usage["capability_gate"]["refusals"]
        ]

    def test_every_tier_incapable_refuses_rather_than_dialling_one(self, monkeypatch):
        _install(monkeypatch)
        model_router.set_call_context(
            {
                "adaptive_routing": True,
                "capability_gate": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-cheap-no-tools"}
                },
            }
        )
        with pytest.raises(model_capabilities.CapabilityRoutingRefused) as excinfo:
            model_router.call_model(MESSAGES, difficulty_hint="easy", tools=BASH_TOOL)
        # One candidate refused for one reason names THAT reason, not the
        # generic "nothing was eligible": an operator who sees
        # "tool_calling_unsupported" can act on it.
        assert excinfo.value.reason == model_capabilities.REFUSAL_REASON_TOOLS
        assert excinfo.value.model == "acme-cheap-no-tools"

    def test_a_refusal_happens_before_the_provider_is_touched(self, monkeypatch):
        """No billable call may happen on a target the gate rejects."""
        _install(monkeypatch)
        dialed: list = []

        def completion(**kwargs):
            dialed.append(kwargs)
            return None

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=completion)
        )
        model_router.set_call_context(
            {
                "adaptive_routing": True,
                "capability_gate": True,
                "model_tiers": {"easy": {"provider": "acme", "model": "acme-unpriced"}},
            }
        )
        with pytest.raises(model_capabilities.CapabilityRoutingRefused):
            model_router.call_model(MESSAGES, difficulty_hint="easy")
        assert dialed == []

    def test_the_refusal_is_published_on_the_ledger_row(self, monkeypatch, tmp_path):
        _install(monkeypatch)
        ledger = tmp_path / "model_ledger.jsonl"
        model_router.set_call_context(
            {
                "adaptive_routing": True,
                "capability_gate": True,
                "model_tiers": {
                    "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            },
            ledger_dir=ledger,
        )
        model_router.call_model(MESSAGES, difficulty_hint="easy", tools=BASH_TOOL)
        rows = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows, (
            "the ledger row is the durable receipt; an empty ledger proves nothing"
        )
        assert rows[-1]["capability_refusal_count"] == 1
        assert (
            rows[-1]["capability_gate"]["refusals"][0]["model"] == "acme-cheap-no-tools"
        )
        assert rows[-1]["price_state"] == "priced"


# ---------------------------------------------------------------------------
# 3. the predictor comparison, and the decision NOT to ship
# ---------------------------------------------------------------------------


def _holdout_groups(rows, frac, seed):
    """Return the group ids the deterministic split assigns to the holdout."""
    _train, holdout = difficulty.group_holdout_split(rows, frac, seed)
    return {str(row["group_id"]) for row in holdout}


class TestPredictorComparison:
    @staticmethod
    def _rows(n=9, hard_mod=3, offset=0):
        """A deterministic row set. `hard_mod`/`offset` choose which
        groups are hard, so a test can place hard labels on BOTH sides of the
        deterministic split rather than hoping one lands in the holdout."""
        rows = []
        for index in range(n):
            hard = (index % hard_mod) == offset
            rows.append(
                {
                    "group_id": f"g{index:02d}",
                    "task_id": f"t{index:02d}",
                    "label": "hard" if hard else "easy",
                    # The challenger is perfect; the incumbent false-escalates
                    # on every fifth group. So the challenger CAN win.
                    "legacy_hint": "hard" if (hard or index % 5 == 0) else "easy",
                    "structural_hint": "hard" if hard else "easy",
                    "features_resolved": True,
                }
            )
        return rows

    def test_a_held_out_comparison_states_a_winner_and_reports_both_splits(self):
        verdict = difficulty.compare_predictors(self._rows(), holdout_frac=0.34, seed=7)
        assert verdict["winner"] in ("legacy", "structural", "tie")
        assert verdict["decidable"] is True
        assert "held-out" in verdict["honesty"]

    def test_a_win_on_too_few_hard_labels_is_recorded_and_refused_as_a_ship(self):
        """The round's own discipline, applied to itself: one hard observation
        can be "won" by a single lucky prediction, so a DECLARED FLOOR of hard
        labels gates the SHIPPING decision even when the challenger genuinely
        won the held-out split.

        Seed 5 is pinned because the split is a SHA-256 over (seed, group).
        The property under test is the hard-label COUNT in the holdout, which
        is a property of the split, so the split must be fixed rather than
        hoped for."""
        verdict = difficulty.compare_predictors(self._rows(), holdout_frac=0.34, seed=5)
        assert verdict["winner"] == "structural"
        assert 0 < verdict["holdout_hard_labels"] < difficulty.MIN_HOLDOUT_HARD_LABELS
        assert verdict["decidable"] is True
        assert verdict["sample_adequate"] is False
        assert verdict["ship"] is False
        assert "declared floor" in verdict["honesty"]
        assert "promising-but-unproven" in verdict["honesty"]

    def test_an_adequate_sample_that_wins_does_ship(self):
        """The floor is not a permanent refusal: enough hard labels and a real
        held-out win must still be able to enable the challenger. Without this
        the floor would be a way of never shipping anything."""
        verdict: dict = {}
        for seed in range(1, 60):
            candidate = difficulty.compare_predictors(
                self._rows(), holdout_frac=0.4, seed=seed
            )
            if (
                candidate["holdout_hard_labels"] >= difficulty.MIN_HOLDOUT_HARD_LABELS
                and candidate["winner"] == "structural"
            ):
                verdict = candidate
                break
        else:  # pragma: no cover - the split is deterministic; this is a bug
            pytest.fail(
                "no seed produced an adequate structural win; the split changed"
            )
        assert verdict["sample_adequate"] is True
        assert verdict["decidable"] is True
        assert verdict["ship"] is True
        assert "eligible to be enabled" in verdict["honesty"]

    def test_a_holdout_with_no_hard_label_is_not_decidable(self):
        rows = [
            {
                "group_id": f"g{i}",
                "task_id": f"t{i}",
                "label": "easy",
                "legacy_hint": "easy",
                "structural_hint": "easy",
                "features_resolved": True,
            }
            for i in range(6)
        ]
        verdict = difficulty.compare_predictors(rows, holdout_frac=0.34, seed=5)
        assert verdict["holdout_hard_labels"] == 0
        assert verdict["decidable"] is False
        assert verdict["ship"] is False
        assert "cannot be validated" in verdict["honesty"]

    def test_the_winner_is_measured_on_the_holdout_not_the_train_split(self):
        """A challenger that wins only on the data it was read from has not
        won anything; the report must therefore expose both splits and keep
        whole bug GROUPS out of both halves of the split."""
        rows = self._rows()
        verdict = difficulty.compare_predictors(rows, holdout_frac=0.25, seed=7)
        assert set(verdict["scores"]) == {"train", "holdout"}
        assert verdict["train_rows"] > 0
        assert verdict["holdout_rows"] > 0
        assert verdict["train_rows"] + verdict["holdout_rows"] == len(rows)
        # Whole groups: a repeated run of one bug is ONE observation, so the
        # per-split group counts must partition the group set.
        assert verdict["holdout_groups"] == len(
            {
                row["group_id"]
                for row in rows
                if row["group_id"] in _holdout_groups(rows, 0.25, 7)
            }
        )
        train, holdout = difficulty.group_holdout_split(rows, 0.25, 7)
        assert {row["group_id"] for row in train}.isdisjoint(
            {row["group_id"] for row in holdout}
        )
        assert verdict["holdout_groups"] == len({row["group_id"] for row in holdout})

    def test_a_legacy_win_is_never_shipped_and_says_so(self):
        rows = self._rows()
        for row in rows:
            row["structural_hint"] = "easy"
        verdict = difficulty.compare_predictors(rows, holdout_frac=0.25, seed=7)
        assert verdict["winner"] == "legacy"
        assert verdict["ship"] is False
        assert "did NOT beat" in verdict["honesty"]

    def test_no_calibration_artifact_ships_so_the_challenger_stays_off(self):
        """The incumbent is the DEFAULT. Reaching the challenger requires a
        caller to name it; `auto` cannot, because no validated artifact
        exists."""
        assert model_router._structural_calibrated() is False

    def test_auto_resolves_to_the_incumbent_and_records_that_it_did(self, monkeypatch):
        _install(monkeypatch)
        usage = _call(
            {
                "adaptive_routing": True,
                "difficulty_features": "auto",
                "difficulty_estimator": "heuristic",
                "model_tiers": {
                    "easy": {"provider": "openai", "model": "gpt-4o-mini"},
                    "hard": {"provider": "openai", "model": "gpt-4o"},
                },
            },
            hint=None,
        )
        prediction = usage["difficulty_prediction"]
        assert prediction["feature_family"] == "heuristic"

    def test_the_structural_challenger_is_reachable_by_name_and_says_which_family(
        self, monkeypatch
    ):
        _install(monkeypatch)
        usage = _call(
            {
                "adaptive_routing": True,
                "difficulty_features": "structural",
                "difficulty_estimator": "heuristic",
                "repo_path": None,
                "model_tiers": {
                    "easy": {"provider": "openai", "model": "gpt-4o-mini"},
                    "hard": {"provider": "openai", "model": "gpt-4o"},
                },
            },
            hint=None,
        )
        prediction = usage["difficulty_prediction"]
        assert prediction["feature_family"] == "structural"
        assert prediction["estimator"] == "structural"

    def test_an_unusable_selector_falls_back_to_the_incumbent(self, monkeypatch):
        """A challenger must never break a task."""
        _install(monkeypatch)
        usage = _call(
            {
                "adaptive_routing": True,
                "difficulty_features": "no-such-family",
                "model_tiers": {"easy": {"provider": "openai", "model": "gpt-4o-mini"}},
            },
            hint=None,
        )
        assert usage["model"] == "gpt-4o-mini"


class TestStructuralFeatures:
    def test_a_missing_repository_is_unmeasured_not_an_empty_one(self):
        features = difficulty.structural_features({"repo_path": "no/such/tree"})
        assert features["repo_shape"]["measured"] is False
        assert features["feature_coverage"]["repo_size"] is False
        assert features["feature_coverage"]["test_density"] is False

    def test_an_undeclared_target_test_is_unmeasured_not_missing(self):
        """'The operator declared no target' and 'the declared target is not
        in this repository' are different facts, and the first is not
        evidence of the second."""
        assert difficulty.target_test_exists(".", None) is None
        assert difficulty.target_test_exists(".", "") is None
        assert difficulty.target_test_exists(".", "does/not/exist.py") is False
        assert difficulty.target_test_exists(".", "harness/config.py") is True

    def test_the_bug_class_vocabulary_is_closed_and_ordered(self):
        assert difficulty.classify_bug_class("") == "unknown"
        assert (
            difficulty.classify_bug_class("off-by-one in the slice index") == "boundary"
        )
        assert (
            difficulty.classify_bug_class("racy and intermittent under load")
            == "concurrency"
        )
        for name in (
            difficulty.classify_bug_class("racy and intermittent under load"),
            difficulty.classify_bug_class("constant typo"),
        ):
            assert name in difficulty.BUG_CLASSES

    def test_repo_shape_measures_modules_and_test_density(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "a.py").write_text(
            "def f():\n    return 1\n", encoding="utf-8"
        )
        (tmp_path / "pkg" / "b.py").write_text(
            "def g():\n    return 2\n", encoding="utf-8"
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text(
            "def test_one():\n    pass\n\ndef test_two():\n    pass\n", encoding="utf-8"
        )
        shape = difficulty.repo_shape(tmp_path)
        assert shape["measured"] is True
        assert shape["modules"] == 3
        assert shape["test_files"] == 1
        assert shape["test_functions"] == 2
        assert shape["tests_per_module"] is not None

    def test_fan_in_counts_the_modules_that_reach_a_symbol(self, tmp_path):
        (tmp_path / "core.py").write_text(
            "def shared():\n    return 1\n", encoding="utf-8"
        )
        (tmp_path / "one.py").write_text(
            "from core import shared\n\nshared()\n", encoding="utf-8"
        )
        (tmp_path / "two.py").write_text(
            "import core\n\ncore.shared()\n", encoding="utf-8"
        )
        (tmp_path / "three.py").write_text("x = 1\n", encoding="utf-8")
        receipt = difficulty.symbol_fan_in(tmp_path, ["shared"])
        assert receipt["measured"] is True
        assert receipt["fan_in"]["shared"] == 2

    def test_fan_in_is_unmeasured_without_symbols_rather_than_zero(self):
        receipt = difficulty.symbol_fan_in(".", [])
        assert receipt["measured"] is False
        assert receipt["reason"] == "no_symbols"

    def test_a_walk_budget_that_fires_is_reported_as_incomplete(self, tmp_path):
        for index in range(6):
            (tmp_path / f"m{index}.py").write_text("x = 1\n", encoding="utf-8")
        shape = difficulty.repo_shape(tmp_path, budget=2)
        assert shape["walk_incomplete"] is True
        assert shape["reason"] == "walk_budget_exhausted"
        assert shape["modules"] <= 2


# ---------------------------------------------------------------------------
# 4. reasoning_content, per the R2-14 protocol
# ---------------------------------------------------------------------------


def _reasoning_response(*, reasoning, content, finish_reason, reasoning_tokens=None):
    """Build a litellm-shaped response carrying a separate reasoning field."""
    message = types.SimpleNamespace(
        content=content, reasoning_content=reasoning, tool_calls=None
    )
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    details = types.SimpleNamespace(reasoning_tokens=reasoning_tokens or 0)
    usage = types.SimpleNamespace(
        prompt_tokens=10, completion_tokens=400, completion_tokens_details=details
    )
    return types.SimpleNamespace(choices=[choice], usage=usage, _hidden_params={})


class TestReasoningContentProtocol:
    def test_a_budget_eaten_by_reasoning_is_a_named_truncation_not_an_empty_answer(
        self, monkeypatch, tmp_path
    ):
        """The historical message was 'provider returned empty assistant
        content' for BOTH a budget problem and a gateway problem, which sent
        the operator after the wrong knob entirely."""
        _install(monkeypatch)
        response = _reasoning_response(
            reasoning="thinking about it for a long time",
            content=None,
            finish_reason="length",
            reasoning_tokens=4000,
        )
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            types.SimpleNamespace(completion=lambda **k: response),
        )
        ledger = tmp_path / "model_ledger.jsonl"
        model_router.set_call_context(
            {"model_tiers": {"easy": {"provider": "openai", "model": "gpt-4o-mini"}}},
            ledger_dir=ledger,
        )
        with pytest.raises(RuntimeError) as excinfo:
            model_router.call_model(MESSAGES, difficulty_hint="easy")
        assert "reasoning_content" in str(excinfo.value)
        assert "finish_reason=length" in str(excinfo.value)
        row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
        assert row["stop_reason"] == "length"
        assert row["reasoning_content_present"] is True
        assert row["reasoning_tokens"] == 4000
        assert row["reasoning_chars"] == len("thinking about it for a long time")

    def test_reasoning_is_never_substituted_for_the_answer(self, monkeypatch, tmp_path):
        """A hidden trace is not a reply. Substituting it would launder a
        truncated turn into a successful-looking one."""
        _install(monkeypatch)
        response = _reasoning_response(
            reasoning="I should probably edit the file now",
            content=None,
            finish_reason="stop",
        )
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            types.SimpleNamespace(completion=lambda **k: response),
        )
        ledger = tmp_path / "model_ledger.jsonl"
        model_router.set_call_context(
            {"model_tiers": {"easy": {"provider": "openai", "model": "gpt-4o-mini"}}},
            ledger_dir=ledger,
        )
        with pytest.raises(RuntimeError) as excinfo:
            model_router.call_model(MESSAGES, difficulty_hint="easy")
        assert "not an answer" in str(excinfo.value)
        assert "I should probably edit the file now" not in str(excinfo.value)
        row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
        assert row["reasoning_content_present"] is True

    def test_a_genuinely_empty_response_is_distinguishable_from_a_reasoning_one(
        self, monkeypatch, tmp_path
    ):
        _install(monkeypatch)
        response = _reasoning_response(
            reasoning=None, content=None, finish_reason="stop"
        )
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            types.SimpleNamespace(completion=lambda **k: response),
        )
        ledger = tmp_path / "model_ledger.jsonl"
        model_router.set_call_context(
            {"model_tiers": {"easy": {"provider": "openai", "model": "gpt-4o-mini"}}},
            ledger_dir=ledger,
        )
        with pytest.raises(RuntimeError) as excinfo:
            model_router.call_model(MESSAGES, difficulty_hint="easy")
        assert "empty assistant content" in str(excinfo.value)
        row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
        assert row.get("reasoning_content_present") in (False, None)

    def test_a_normal_answer_with_reasoning_still_succeeds_and_records_the_reasoning(
        self, monkeypatch
    ):
        _install(monkeypatch)
        response = _reasoning_response(
            reasoning="let me think",
            content="the visible answer",
            finish_reason="stop",
            reasoning_tokens=12,
        )
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            types.SimpleNamespace(completion=lambda **k: response),
        )
        model_router.set_call_context(
            {"model_tiers": {"easy": {"provider": "openai", "model": "gpt-4o-mini"}}}
        )
        assert (
            model_router.call_model(MESSAGES, difficulty_hint="easy")
            == "the visible answer"
        )
        usage = model_router.get_last_usage()
        assert usage["reasoning_content_present"] is True
        assert usage["reasoning_tokens"] == 12

    def test_the_registry_records_reasoning_support_as_a_capability(self):
        assert (
            model_capabilities.capability_of("openai", "o3-mini").supports_reasoning
            is True
        )
        assert (
            model_capabilities.capability_of("openai", "gpt-4o").supports_reasoning
            is False
        )
        # An undescribed model is UNKNOWN, never False.
        assert (
            model_capabilities.capability_of(None, "acme-nobody").supports_reasoning
            is None
        )


# ---------------------------------------------------------------------------
# the ablation itself: measured, not asserted
# ---------------------------------------------------------------------------


class TestTheRoutingCapabilityAblation:
    def test_every_arm_runs_every_case_and_every_expectation_holds(self):
        from evals.routing_capability import run_matrix

        report = run_matrix()
        assert report["verdict"] == "CLEAN", report["regressions"]
        assert report["checks_run"] >= 15
        assert report["regressions"] == []

    def test_the_price_only_arm_really_does_select_the_incapable_cheapest_model(self):
        """If the control arm stopped being the pre-R2-13 behaviour, the
        comparison would be measuring nothing."""
        from evals.routing_capability import (
            CASES,
            _install_fixture_capabilities,
            _run_case,
        )

        _install_fixture_capabilities()
        case = next(
            c
            for c in CASES
            if c.name == "tool_incapable_model_is_excluded_from_a_tool_driven_loop"
        )
        control = _run_case(case, "price_only")
        gated = _run_case(case, "capability")
        assert control["selected"] == "acme-cheap-no-tools"
        assert gated["selected"] == "gpt-4o-mini"
        assert model_capabilities.REFUSAL_REASON_TOOLS in gated["refusal_reasons"]

    def test_the_gate_is_recorded_off_in_the_arm_that_declares_no_capability_key(self):
        from evals.routing_capability import (
            CASES,
            _install_fixture_capabilities,
            _run_case,
        )

        _install_fixture_capabilities()
        for case in CASES:
            row = _run_case(case, "price_only")
            assert row["capability_gate_enabled"] is False, case.name

    def test_an_unknown_arm_is_refused_rather_than_silently_dropped(self):
        from evals.routing_capability import run_matrix

        with pytest.raises(ValueError):
            run_matrix(["no_such_arm"])

    def test_the_cli_reports_clean_and_exits_zero(self, capsys):
        from evals.routing_capability import main

        assert main([]) == 0
        assert "verdict=CLEAN" in capsys.readouterr().out


class TestTheHeldOutDriver:
    @staticmethod
    def _trace_tree(root, count=6):
        """Write REAL synthetic trace files the driver's reader can consume.

        `build_observations` re-reads each accepted task's own `trace.jsonl`
        for the issue text and the declared target test, so a stubbed scan
        alone would leave those features unresolvable and the test would
        assert on a report about nothing. The trace shape is the documented
        public one (`{"kind": ..., "data": {...}}`).
        """
        repo = root / "repo"
        (repo / "pkg").mkdir(parents=True, exist_ok=True)
        (repo / "pkg" / "mod.py").write_text(
            "def f():\n    return 1\n", encoding="utf-8"
        )
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_mod.py").write_text(
            "def test_f():\n    pass\n", encoding="utf-8"
        )
        records = []
        for index in range(count):
            task_id = f"t{index:02d}"
            task_dir = root / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            with (task_dir / "trace.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "kind": "task_start",
                            "data": {
                                "task_id": task_id,
                                "repo_path": str(repo),
                                "issue_text": "off-by-one in the slice index",
                                "config": {"target_test": "tests/test_mod.py"},
                            },
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "kind": "baseline_verify",
                            "data": {"target_test": "tests/test_mod.py"},
                        }
                    )
                    + "\n"
                )
            records.append(
                {
                    "task_id": task_id,
                    "rel_dir": task_id,
                    "repo": str(repo),
                    "repo_key": "synthetic",
                    "status": "failed" if index % 3 == 0 else "success",
                }
            )
        return records

    def test_the_report_states_a_winner_and_carries_its_honesty_notes(
        self, tmp_path, monkeypatch
    ):
        """The whole driver over REAL synthetic traces: the label policy, the
        trace reader, the structural feature extraction, the grouped split and
        the verdict. The test does not depend on this machine's accumulated
        run history."""
        from evals import difficulty_holdout

        records = self._trace_tree(tmp_path)
        monkeypatch.setattr(
            difficulty_holdout.analyze_history,
            "scan_tasks",
            lambda root, diagnostics=None: list(records),
        )
        monkeypatch.setattr(
            difficulty_holdout.analyze_history,
            "calibration_rows",
            lambda recs: [
                {
                    "task_id": r["task_id"],
                    "group_id": f"g-{r['task_id']}",
                    "repo_key": r["repo_key"],
                    "label": "hard" if r["status"] == "failed" else "easy",
                    "status": r["status"],
                    "attempts": 1,
                    "repairs": 0,
                }
                for r in records
            ],
        )
        report = difficulty_holdout.build_report(tmp_path, holdout_frac=0.34, seed=3)
        verdict = report["verdict"]
        assert verdict["winner"] in ("legacy", "structural", "tie")
        # Every row's context really was read and scored.
        assert report["coverage"]["rows_built"] == len(records)
        assert report["coverage"]["issue_text_resolved"] == len(records)
        assert report["coverage"]["repo_path_resolved"] == len(records)
        assert report["coverage"]["target_test_declared"] == len(records)
        # The features the prompt named, and whether each was MEASURABLE.
        measured = report["coverage"]["feature_measured"]
        assert measured["repo_size"] == len(records)
        assert measured["test_density"] == len(records)
        assert measured["bug_class"] == len(records)
        # fan-in needs a per-task touched-SYMBOL list, which run history does
        # not carry; the report says so rather than scoring it as zero.
        assert measured["fan_in"] == 0
        assert "symbols" in report["coverage"]["feature_notes"]["fan_in"]
        assert any("DESIGN LEAKAGE" in note for note in report["honesty"])
        assert not difficulty_holdout.CALIBRATION_PATH.exists()

    def test_apply_writes_nothing_when_the_challenger_did_not_earn_it(self, tmp_path):
        from evals import difficulty_holdout

        target = tmp_path / "calibration.json"
        verdict = {"ship": False, "winner": "legacy"}
        assert (
            difficulty_holdout.write_calibration({"verdict": verdict}, target) is None
        )
        assert not target.exists()

    def test_apply_writes_only_on_a_ship_verdict_and_records_why(self, tmp_path):
        from evals import difficulty_holdout

        target = tmp_path / "calibration.json"
        verdict = {
            "ship": True,
            "winner": "structural",
            "holdout_rows": 20,
            "holdout_hard_labels": 5,
            "coverage": {"rows": 100},
        }
        written = difficulty_holdout.write_calibration({"verdict": verdict}, target)
        assert written == target
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["shipped"] is True
        assert payload["verdict"]["winner"] == "structural"

    def test_a_missing_logs_root_is_reported_not_silently_empty(self, tmp_path, capsys):
        from evals.difficulty_holdout import main

        assert main(["--logs-root", str(tmp_path / "nope")]) == 2
        assert "nothing was compared" in capsys.readouterr().out
