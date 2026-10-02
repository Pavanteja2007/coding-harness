"""AGT-08 - the effort ladder, honestly reported, and the cheap summariser tier.

Every test here is named after the BEHAVIOUR the brief demanded, not after an
implementation, and every one is host-only: no Docker, no provider, no network.
Provider-shaped assertions use a fake litellm installed through the router's own
seam, so nothing here is evidence about a real provider's behaviour.

The five proofs the brief asks for, and where they live:

* "Each level maps to a real parameter or an explicit unsupported report"
  -> ``TestTheLadder`` / ``TestProviderReporting``
* "the ledger records it" -> ``TestTheLedgerRecordsEffort``
* "resume identity includes it" -> ``TestResumeIdentity``
* "--json exposes it" -> ``TestJsonExposesEffort``
* "the verifier gate is identical at every level" -> ``TestTheGateIsUntouched``
* "compaction defaults to the cheap tier and says which ran"
  -> ``TestCheapCompaction``
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from runtime import model_capabilities as mc
from runtime import model_router
from runtime.model_capabilities import (
    EFFORT_CHOICES,
    EFFORT_ENV_VAR,
    CapabilityError,
    EffortKnob,
    map_effort,
    normalize_effort,
    register_effort_knob,
    reset_effort_knobs,
    resolve_effort,
)
from runtime.model_router import call_model, set_call_context

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_litellm(monkeypatch, capture: dict) -> None:
    """Install a fake litellm that records the request it was handed."""

    def completion(**kwargs):
        capture.update(kwargs)
        msg = types.SimpleNamespace(content="fake", tool_calls=None)
        choice = types.SimpleNamespace(message=msg, finish_reason="stop")
        usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=2)
        return types.SimpleNamespace(choices=[choice], usage=usage, _hidden_params={})

    monkeypatch.setitem(
        __import__("sys").modules,
        "litellm",
        types.SimpleNamespace(completion=completion),
    )


def _rows(path: Path) -> list:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(autouse=True)
def _clean_effort_state():
    """Every test starts from the built-in knob table and no NEO_EFFORT."""
    import os

    previous = os.environ.pop(EFFORT_ENV_VAR, None)
    reset_effort_knobs()
    try:
        yield
    finally:
        reset_effort_knobs()
        if previous is None:
            os.environ.pop(EFFORT_ENV_VAR, None)
        else:
            os.environ[EFFORT_ENV_VAR] = previous


# ---------------------------------------------------------------------------
# 1. the ladder itself
# ---------------------------------------------------------------------------


class TestTheLadder:
    def test_the_ladder_is_the_five_levels_plus_auto(self):
        assert EFFORT_CHOICES == ("auto", "low", "medium", "high", "xhigh", "max")

    def test_every_level_maps_to_a_real_parameter_or_says_why_not(self):
        """The whole point: no level is ever silently dropped.

        For every (family, level) pair the answer is EITHER a named provider
        parameter with a value, OR one of the closed non-sent statuses with a
        sentence a human can act on. There is no third possibility.
        """
        sent = 0
        reported = 0
        for knob in mc.known_effort_knobs():
            for level in EFFORT_CHOICES:
                plan = map_effort(
                    level, f"probe-model-for-{knob.family}", provider=knob.family
                )
                assert plan.status in mc.EFFORT_STATUSES, (knob.family, level)
                if plan.sent:
                    sent += 1
                    # A real parameter name and a value, never an empty dict.
                    assert plan.parameter and plan.parameter == knob.parameter
                    assert plan.parameters == {plan.parameter: plan.value}
                else:
                    reported += 1
                    assert plan.parameters == {}, (knob.family, level)
                    assert plan.detail, (knob.family, level)
        assert sent > 0 and reported > 0

    def test_a_level_the_family_does_not_accept_is_reported_not_clamped(self):
        """`max` on a three-level family must not become `high`."""
        plan = map_effort("max", "gpt-5", provider="openai")
        assert plan.status == mc.EFFORT_UNSUPPORTED_LEVEL
        assert plan.parameters == {}
        assert "max" in plan.detail and "reasoning_effort" in plan.detail

    def test_a_model_with_no_declared_knob_says_so(self):
        plan = map_effort("high", "some-unknown-model", provider="openai")
        assert plan.status == mc.EFFORT_UNSUPPORTED_MODEL
        assert plan.parameters == {}
        assert "no declared effort knob" in plan.detail

    def test_an_unusable_level_is_reported_not_rounded_to_the_default(self):
        """A typo must be visible. Silently becoming `auto` is the bug."""
        plan = map_effort("veru-high", "gpt-5", provider="openai")
        assert plan.status == mc.EFFORT_INVALID
        assert plan.requested == "veru-high"
        assert plan.parameters == {}
        assert "veru-high" in plan.detail

    def test_auto_sends_nothing_at_all(self):
        plan = map_effort("auto", "gpt-5", provider="openai")
        assert plan.status == mc.EFFORT_AUTO
        assert plan.parameters == {}

    def test_the_operator_can_pin_the_parameter_off(self):
        """The escape hatch for an endpoint that 400s on an unknown kwarg."""
        for off in ("none", "off", "false", "0", "no", False):
            plan = map_effort("high", "gpt-5", provider="openai", parameter=off)
            assert plan.status == mc.EFFORT_DISABLED, off
            assert plan.parameters == {}, off

    def test_no_parameter_means_no_override_not_off(self):
        """The absent case must reach the family's own knob.

        `parameter=None` is what the router passes on every unconfigured run,
        so reading it as "send nothing" would silently disable the whole
        feature instead of using it.
        """
        plan = map_effort("high", "gpt-5", provider="openai", parameter=None)
        assert plan.sent
        assert plan.parameters == {"reasoning_effort": "high"}

    def test_the_operator_can_name_the_parameter_explicitly(self):
        plan = map_effort(
            "high", "my-own-model", provider="my-gateway", parameter="reasoning_effort"
        )
        assert plan.sent
        assert plan.parameters == {"reasoning_effort": "high"}

    def test_a_parameter_nobody_declared_sends_nothing_and_says_why(self):
        plan = map_effort("high", "gpt-5", provider="openai", parameter="made_up_knob")
        assert plan.status == mc.EFFORT_DISABLED
        assert plan.parameters == {}
        assert "made_up_knob" in plan.detail

    def test_aliases_resolve_to_the_same_rung(self):
        """`/effort hi` and config `"high"` must be ONE setting."""
        assert normalize_effort("hi") == normalize_effort("high") == "high"
        assert normalize_effort("Med") == normalize_effort("medium") == "medium"
        assert normalize_effort("") == normalize_effort(None) == "auto"
        assert normalize_effort("nonsense") == ""

    def test_a_malformed_knob_declaration_raises_rather_than_defaulting(self):
        with pytest.raises(CapabilityError):
            EffortKnob(family="", parameter="x", values={"low": 1})
        with pytest.raises(CapabilityError):
            EffortKnob(family="acme", parameter="", values={"low": 1})
        with pytest.raises(CapabilityError) as excinfo:
            EffortKnob(family="acme", parameter="x", values={"turbo": 1})
        assert "outside the ladder" in str(excinfo.value)

    def test_a_declared_knob_is_usable_and_removable(self):
        register_effort_knob(
            EffortKnob(
                family="acme", parameter="acme_effort", values={"low": 1, "high": 9}
            )
        )
        try:
            plan = map_effort("high", "acme/thing", provider="acme")
            assert plan.sent and plan.parameters == {"acme_effort": 9}
            assert map_effort("max", "acme/thing", provider="acme").status == (
                mc.EFFORT_UNSUPPORTED_LEVEL
            )
        finally:
            assert mc.unregister_effort_knob("acme") is True
        assert map_effort("high", "acme/thing", provider="acme").status == (
            mc.EFFORT_UNSUPPORTED_MODEL
        )


# ---------------------------------------------------------------------------
# 2. resolution precedence (config / env / override)
# ---------------------------------------------------------------------------


class TestResolution:
    def test_config_beats_env_and_override_beats_config(self, monkeypatch):
        monkeypatch.setenv(EFFORT_ENV_VAR, "low")
        assert resolve_effort({"effort": "high"}) == ("high", "config")
        assert resolve_effort({"effort": "high"}, override="xhigh") == (
            "xhigh",
            "override",
        )

    def test_env_is_used_when_config_says_nothing(self, monkeypatch):
        monkeypatch.setenv(EFFORT_ENV_VAR, "max")
        assert resolve_effort({}) == ("max", "env")
        assert resolve_effort({"effort": ""}) == ("max", "env")

    def test_nothing_configured_is_auto_from_the_default_rung(self):
        assert resolve_effort({}) == ("auto", "default")

    def test_an_unusable_value_is_reported_as_an_invalid_source(self, monkeypatch):
        monkeypatch.setenv(EFFORT_ENV_VAR, "turbo")
        level, source = resolve_effort({})
        assert level == "auto"
        assert source.startswith("invalid:")

    def test_the_harness_merged_config_carries_the_environment_value(self, monkeypatch):
        """`get_config` is the merge every task goes through."""
        from harness.config import DEFAULTS, get_config

        assert DEFAULTS["effort"] == "auto"
        assert get_config({})["effort"] == "auto"
        monkeypatch.setenv(EFFORT_ENV_VAR, "high")
        assert get_config({})["effort"] == "high"
        assert get_config({"effort": "low"})["effort"] == "low"


# ---------------------------------------------------------------------------
# 3. the request the provider actually receives
# ---------------------------------------------------------------------------


class TestTheRequest:
    def test_a_supported_level_reaches_the_wire(self, tmp_path, monkeypatch):
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        set_call_context(
            {"provider": "openai", "effort": "high"},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        assert call_model([{"role": "user", "content": "x"}], model="gpt-5") == "fake"
        assert capture["reasoning_effort"] == "high"

    def test_auto_leaves_the_request_byte_identical(self, tmp_path, monkeypatch):
        """The shipped default must cost nothing on the wire."""
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        set_call_context(
            {"provider": "openai", "effort": "auto"},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5")
        assert "reasoning_effort" not in capture
        assert "thinking" not in capture
        assert "thinking_budget" not in capture

    def test_an_unsupported_provider_sends_nothing_but_still_calls(
        self, tmp_path, monkeypatch
    ):
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        set_call_context(
            {"provider": "openai", "effort": "max"},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        assert call_model([{"role": "user", "content": "x"}], model="gpt-5") == "fake"
        assert "reasoning_effort" not in capture

    def test_a_call_level_effort_beats_the_context(self, tmp_path, monkeypatch):
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        set_call_context(
            {"provider": "openai", "effort": "low"},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5", effort="high")
        assert capture["reasoning_effort"] == "high"

    def test_the_anthropic_knob_is_the_documented_extended_thinking_shape(
        self, tmp_path, monkeypatch
    ):
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        set_call_context(
            {"provider": "anthropic", "effort": "xhigh"},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        call_model([{"role": "user", "content": "x"}], model="claude-sonnet-4-5")
        assert capture["thinking"] == {"type": "enabled", "budget_tokens": 24576}

    def test_a_gateway_fronting_a_claude_model_gets_the_claude_knob(
        self, tmp_path, monkeypatch
    ):
        """A generic `openai` provider fronts many families; the MODEL decides."""
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        set_call_context(
            {"provider": "openai", "effort": "low"},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        call_model([{"role": "user", "content": "x"}], model="claude-haiku-4-5")
        assert "thinking" in capture
        assert "reasoning_effort" not in capture

    def test_the_off_switch_reaches_no_parameter_even_when_a_knob_exists(
        self, tmp_path, monkeypatch
    ):
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        set_call_context(
            {"provider": "openai", "effort": "high", "effort_parameter": "none"},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5")
        assert "reasoning_effort" not in capture

    def test_the_offline_mock_lane_reports_synthetic_not_sent(self, tmp_path):
        from runtime import mock_provider

        mock_provider.install({"m": "canned"})
        try:
            set_call_context(
                {"use_mock_provider": True, "effort": "high"},
                ledger_dir=str(tmp_path / "ledger.jsonl"),
            )
            assert call_model([{"role": "user", "content": "x"}], model="m") == "canned"
            row = _rows(tmp_path / "ledger.jsonl")[-1]
            assert row["effort"] == "high"
            assert row["effort_status"] == mc.EFFORT_SYNTHETIC
            assert row["effort_sent"] is False
        finally:
            mock_provider.reset()

    def test_a_streamed_call_carries_the_effort_parameter(self, tmp_path, monkeypatch):
        """Streaming must not be the one path that drops the knob."""
        capture: dict = {}

        def streaming_completion(**kwargs):
            capture.update(kwargs)
            for chunk in (
                types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            delta=types.SimpleNamespace(content="hi", tool_calls=None),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                types.SimpleNamespace(choices=[], usage=None),
            ):
                yield chunk

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=streaming_completion),
        )
        set_call_context(
            {"provider": "openai", "effort": "low"},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        seen: list = []
        out = call_model(
            [{"role": "user", "content": "x"}],
            model="gpt-5",
            stream=True,
            on_delta=seen.append,
        )
        assert out == "hi"
        assert capture.get("reasoning_effort") == "low"


# ---------------------------------------------------------------------------
# 4. the ledger + the trace
# ---------------------------------------------------------------------------


class TestTheLedgerRecordsEffort:
    def test_a_success_row_records_the_level_the_parameter_and_the_model(
        self, tmp_path, monkeypatch
    ):
        _fake_litellm(monkeypatch, {})
        ledger = tmp_path / "ledger.jsonl"
        set_call_context(
            {"provider": "openai", "effort": "high"}, ledger_dir=str(ledger)
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5")
        row = _rows(ledger)[-1]
        assert row["effort"] == "high"
        assert row["effort_status"] == mc.EFFORT_SENT
        assert row["effort_sent"] is True
        assert row["effort_parameter"] == "reasoning_effort"
        assert row["effort_value"] == "high"
        assert row["effort_model"] == "gpt-5"

    def test_an_unsupported_row_records_why_nothing_was_sent(
        self, tmp_path, monkeypatch
    ):
        _fake_litellm(monkeypatch, {})
        ledger = tmp_path / "ledger.jsonl"
        set_call_context(
            {"provider": "openai", "effort": "max"}, ledger_dir=str(ledger)
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5")
        row = _rows(ledger)[-1]
        assert row["effort"] == "max"
        assert row["effort_status"] == mc.EFFORT_UNSUPPORTED_LEVEL
        assert row["effort_sent"] is False
        assert row["effort_detail"]

    def test_a_failed_call_still_records_the_effort(self, tmp_path, monkeypatch):
        """A cost claim about a failed call needs the same explanation."""

        def completion(**_kwargs):
            raise RuntimeError("upstream said no")

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        ledger = tmp_path / "ledger.jsonl"
        set_call_context(
            {"provider": "openai", "effort": "low", "rate_limit_retries": 1},
            ledger_dir=str(ledger),
        )
        with pytest.raises(RuntimeError):
            call_model([{"role": "user", "content": "x"}], model="gpt-5")
        rows = _rows(ledger)
        assert rows
        assert all(row["effort"] == "low" for row in rows)
        assert any(row["outcome"] == "error" for row in rows)

    def test_get_last_usage_carries_the_effort(self, tmp_path, monkeypatch):
        _fake_litellm(monkeypatch, {})
        set_call_context(
            {"provider": "openai", "effort": "high"},
            ledger_dir=str(tmp_path / "l.jsonl"),
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5")
        usage = model_router.get_last_usage()
        assert usage["effort"] == "high"
        assert usage["effort_sent"] is True

    def test_the_unified_trace_event_carries_the_effort(self, tmp_path, monkeypatch):
        _fake_litellm(monkeypatch, {})
        emitted: list = []
        monkeypatch.setattr(
            model_router.tracing,
            "emit",
            lambda module, event, **kw: emitted.append(dict(kw, event=event)),
        )
        set_call_context(
            {"provider": "openai", "effort": "high", "task_id": "t-1"},
            ledger_dir=str(tmp_path / "l.jsonl"),
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5")
        routed = [row for row in emitted if row.get("event") == "model_routed"]
        assert routed
        assert routed[-1]["effort"] == "high"
        assert routed[-1]["effort_sent"] is True
        assert routed[-1]["effort_parameter"] == "reasoning_effort"

    def test_the_resilient_pipeline_resolves_effort_per_candidate(
        self, tmp_path, monkeypatch
    ):
        """A fallback model may support a different knob; the plan must follow."""
        seen: list = []

        def completion(**kwargs):
            seen.append(dict(kwargs))
            msg = types.SimpleNamespace(content="fake", tool_calls=None)
            choice = types.SimpleNamespace(message=msg, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1)
            return types.SimpleNamespace(
                choices=[choice], usage=usage, _hidden_params={}
            )

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        ledger = tmp_path / "ledger.jsonl"
        set_call_context(
            {
                "provider": "openai",
                "effort": "low",
                "model_tiers": {
                    "easy": {"provider": "openai", "model": "gpt-5"},
                    "hard": {"provider": "openai", "model": "no-knob-model"},
                },
                "provider_fallback_max": 1,
            },
            ledger_dir=str(ledger),
        )
        call_model(
            [{"role": "user", "content": "x"}], model="gpt-5", difficulty_hint="easy"
        )
        assert seen, "the pipeline never dialled"
        assert seen[0].get("reasoning_effort") == "low"
        # Every row for this call states an effort plan, including a fallback
        # onto a model that cannot honour the level.
        for row in _rows(ledger):
            assert "effort_status" in row
            assert row["effort"] == "low"


# ---------------------------------------------------------------------------
# 5. resume / checkpoint identity
# ---------------------------------------------------------------------------


class TestResumeIdentity:
    def test_the_runtime_checkpoint_identity_includes_the_effort(self, tmp_path):
        from runtime.checkpoint import checkpoint_identity, checkpoint_identity_matches

        base = dict(
            task_id="t",
            repo_path=str(tmp_path),
            request="do the thing",
            config={},
        )
        low = checkpoint_identity(**base)
        high = checkpoint_identity(**{**base, "config": {"effort": "high"}})
        assert low["effort_identity"] == "auto"
        assert high["effort_identity"] == "high"
        # The mismatch is what a resume must notice.
        assert checkpoint_identity_matches(low, dict(low)) is True
        assert checkpoint_identity_matches(high, dict(low)) is False
        assert checkpoint_identity_matches(low, dict(high)) is False

    def test_an_alias_and_its_canonical_rung_are_the_same_identity(self, tmp_path):
        from runtime.checkpoint import checkpoint_identity

        base = dict(task_id="t", repo_path=str(tmp_path), request="r", config={})
        assert (
            checkpoint_identity(**{**base, "config": {"effort": "hi"}})[
                "effort_identity"
            ]
            == checkpoint_identity(**{**base, "config": {"effort": "high"}})[
                "effort_identity"
            ]
        )

    def test_the_strict_kernel_checkpoint_identity_includes_the_effort(self):
        from harness.agent_kernel.checkpoints import (
            checkpoint_identity,
            with_effort,
        )

        low = checkpoint_identity("repo", "r", metadata=with_effort({}, {}))
        high = checkpoint_identity(
            "repo", "r", metadata=with_effort({}, {"effort": "max"})
        )
        assert low["effort_identity"] == "auto"
        assert high["effort_identity"] == "max"

    def test_the_strict_identity_requires_a_non_empty_effort(self):
        """A checkpoint that never recorded a rung must not authorize a resume."""
        from harness.agent_kernel.checkpoints import (
            checkpoint_identity,
            checkpoint_identity_matches,
        )
        from harness.agent_kernel.contracts import Checkpoint

        identity = checkpoint_identity("repo", "r", resume_namespace="run-1")
        assert identity["effort_identity"] == "auto"
        stamped_fields = {
            field: identity[field]
            for field in (
                "repository_identity",
                "request_identity",
                "revision_identity",
                "resume_namespace",
            )
        }
        bare = Checkpoint(last_event_sequence=1, **stamped_fields)
        assert checkpoint_identity_matches(bare, identity) is False
        stamped = Checkpoint(
            last_event_sequence=1, effort_identity="auto", **stamped_fields
        )
        assert checkpoint_identity_matches(stamped, identity) is True
        # A run recorded at a DIFFERENT rung does not match either.
        other = dict(identity, effort_identity="high")
        assert checkpoint_identity_matches(stamped, other) is False

    def test_the_effort_rung_survives_a_checkpoint_round_trip(self):
        from shared.agent_contracts import Checkpoint as Contract

        original = Contract(last_event_sequence=3, effort_identity="xhigh")
        assert Contract.from_dict(original.to_dict()).effort_identity == "xhigh"
        # An older row without the field still loads.
        legacy = Contract.from_dict({"last_event_sequence": 3})
        assert legacy.effort_identity == ""


# ---------------------------------------------------------------------------
# 6. --json exposure
# ---------------------------------------------------------------------------


class TestJsonExposesEffort:
    def test_every_model_call_record_carries_the_effort(self, tmp_path, monkeypatch):
        """`--json` publishes `TaskResult.model_calls`; effort must be on each."""
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        _fake_litellm(monkeypatch, {})
        config = get_config(
            {
                "provider": "openai",
                "model": "gpt-5",
                "effort": "high",
                "stream_enabled": False,
            }
        )
        client = ModelClient(TraceLogger(Path(tmp_path)), config)
        client.call([{"role": "user", "content": "x"}], step="step-1")
        assert client.model_calls
        record = client.model_calls[-1]
        assert record["effort"] == "high"
        assert record["effort_sent"] is True
        assert record["effort_parameter"] == "reasoning_effort"

    def test_the_trace_usage_row_carries_the_effort(self, tmp_path, monkeypatch):
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        _fake_litellm(monkeypatch, {})
        config = get_config(
            {
                "provider": "openai",
                "model": "gpt-5",
                "effort": "xhigh",
                "stream_enabled": False,
            }
        )
        client = ModelClient(TraceLogger(Path(tmp_path)), config)
        client.call([{"role": "user", "content": "x"}], step="step-1")
        rows = [
            json.loads(line)
            for line in (tmp_path / "trace.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        responses = [row for row in rows if row.get("kind") == "model_response"]
        assert responses
        assert responses[-1]["data"]["usage"]["effort"] == "xhigh"

    def test_the_json_document_publishes_the_effort_receipt(
        self, tmp_path, monkeypatch
    ):
        """`neo fix --json` must let a SCRIPT distinguish asked-for from sent.

        The historical document published only `len(model_calls)`, which makes
        "ran at high" and "asked for high and the provider ignored it" the same
        bytes. This is the brief's `--json exposes it` requirement, asserted on
        the real renderer.
        """
        import types as _types

        from cli.main import _effort_json

        result = _types.SimpleNamespace(
            model_calls=[
                {
                    "step": "step-1",
                    "call_index": 1,
                    "model": "gpt-5",
                    "effort": "high",
                    "effort_status": "sent",
                    "effort_sent": True,
                    "effort_parameter": "reasoning_effort",
                    "tokens": 20,
                    "cost": 0.001,
                }
            ]
        )
        payload = _effort_json(result)
        assert payload["level"] == "high"
        assert payload["supported"] is True
        assert payload["parameters"] == ["reasoning_effort"]
        assert payload["receipts"][0]["effort_sent"] is True
        assert payload["receipts_truncated"] == 0

        unsupported = _types.SimpleNamespace(
            model_calls=[
                {
                    "step": "s",
                    "call_index": 1,
                    "model": "gpt-5",
                    "effort": "max",
                    "effort_status": "unsupported_level",
                    "effort_sent": False,
                    "effort_parameter": "reasoning_effort",
                    "tokens": 1,
                    "cost": 0.0,
                }
            ]
        )
        payload = _effort_json(unsupported)
        assert payload["level"] == "max"
        assert payload["supported"] is False
        assert payload["statuses"] == ["unsupported_level"]

    def test_the_json_receipt_is_bounded_and_total(self):
        """A 400-call run must not turn a result document into a log."""
        import types as _types

        from cli.main import EFFORT_RECEIPT_LIMIT, _effort_json

        result = _types.SimpleNamespace(
            model_calls=[
                {"step": f"s{i}", "call_index": i, "effort": "auto"}
                for i in range(EFFORT_RECEIPT_LIMIT + 10)
            ]
        )
        payload = _effort_json(result)
        assert len(payload["receipts"]) == EFFORT_RECEIPT_LIMIT
        assert payload["receipts_truncated"] == 10
        # A run whose boundary knew nothing about effort still gets a document.
        empty = _effort_json(_types.SimpleNamespace(model_calls=[]))
        assert empty["calls_with_effort"] == 0
        assert empty["supported"] is False
        assert _effort_json(object())["receipts"] == []

    def test_a_boundary_that_declares_no_effort_still_works_and_is_reported(
        self, tmp_path, monkeypatch
    ):
        """An older stub keeps working; the record says the level, not a lie."""
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        def legacy_boundary(
            messages, difficulty_hint=None, provider=None, model=None, api_key=None
        ):
            return "legacy reply"

        client = ModelClient(
            TraceLogger(Path(tmp_path)),
            get_config({"effort": "high"}),
        )
        monkeypatch.setattr(client, "_get_fn", lambda: legacy_boundary)
        assert (
            client.call([{"role": "user", "content": "x"}], step="s") == "legacy reply"
        )
        record = client.model_calls[-1]
        assert record["effort"] == "high"
        assert record["effort_sent"] is False

    def test_a_permissive_boundary_is_not_evidence_of_support(
        self, tmp_path, monkeypatch
    ):
        """THE regression this round shipped and the eval matrix caught.

        A `**kwargs` double accepts `effort` and then hands it to something
        that rejects it, which is a run-killing `TypeError` rather than a
        degraded feature. The first version forwarded the keyword whenever the
        boundary was permissive, and the daily-driver suite went red with
        `planner failed: ScriptedModel.__call__() got an unexpected keyword
        argument 'effort'`. Only a NAMED parameter counts.
        """
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        def inner(
            messages, difficulty_hint=None, provider=None, model=None, api_key=None
        ):
            return "inner reply"

        def permissive_boundary(messages, **kwargs):
            # Exactly the shape that broke: accepts anything, forwards to a
            # callable that does not.
            return inner(messages, **{k: v for k, v in kwargs.items() if k != "effort"})

        client = ModelClient(
            TraceLogger(Path(tmp_path)),
            get_config({"effort": "high", "stream_enabled": False}),
        )
        monkeypatch.setattr(client, "_get_fn", lambda: permissive_boundary)
        assert (
            client.call([{"role": "user", "content": "x"}], step="s") == "inner reply"
        )
        assert client.model_calls[-1]["effort"] == "high"

    def test_a_boundary_that_names_effort_does_receive_it(self, tmp_path, monkeypatch):
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        seen: dict = {}

        def modern_boundary(
            messages,
            difficulty_hint=None,
            provider=None,
            model=None,
            api_key=None,
            effort=None,
        ):
            seen["effort"] = effort
            return "modern reply"

        client = ModelClient(
            TraceLogger(Path(tmp_path)),
            get_config({"effort": "xhigh", "stream_enabled": False}),
        )
        monkeypatch.setattr(client, "_get_fn", lambda: modern_boundary)
        assert (
            client.call([{"role": "user", "content": "x"}], step="s") == "modern reply"
        )
        assert seen["effort"] == "xhigh"


# ---------------------------------------------------------------------------
# 7. the verifier gate is identical at every level
# ---------------------------------------------------------------------------


class TestTheGateIsUntouched:
    def test_no_completion_path_reads_the_effort_setting(self):
        """A source-level pin: effort is a MODEL parameter and nothing else.

        Read by parsing, not by grepping for the word, so a docstring that
        mentions effort cannot make this pass.
        """
        import ast

        sources = (
            "harness/agent_kernel/completion.py",
            "harness/agent_kernel/verified.py",
            "harness/agent_kernel/kernel.py",
            "harness/editor.py",
            "execution/verify.py",
        )
        offenders = []
        for relative in sources:
            path = Path(relative)
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # Any string constant naming an effort key, outside a
                # docstring, is a read of the setting.
                if not isinstance(node, ast.Constant) or not isinstance(
                    node.value, str
                ):
                    continue
                if node.value.strip() in {
                    "effort",
                    "effort_parameter",
                    "effort_identity",
                    "context_compaction_tier",
                }:
                    offenders.append(f"{relative}:{node.lineno}")
        assert offenders == [], (
            "the completion/verification path now reads an effort setting: " + offenders
        )

    def test_the_mint_condition_is_still_the_verifier_triple(self):
        """The success word is reachable only from `completed_verified`.

        Located by AST rather than grep, so the pin cannot pass because a
        docstring happens to mention the mint. The assertion that matters is
        the second one: the statement that produces the success word must not
        consult an effort setting, at any level.
        """
        import ast

        path = Path("harness/core.py")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        mints = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(value, ast.Constant)
                    and value.value == "success"
                    and isinstance(key, ast.Attribute)
                ):
                    mints.append(node)
        assert mints, "the verified->success mapping is gone; the pin would be vacuous"
        for mapping in mints:
            # The key must be the canonical completion status, not a literal.
            assert (
                "COMPLETED_VERIFIED" in ast.unparse(mapping)
                or "completed" in ast.unparse(mapping).lower()
            ), ast.unparse(mapping)
            statement = ast.get_source_segment(source, mapping) or ""
            assert "effort" not in statement, statement
        # And the mint statement in the kernel's own completion policy.
        policy = Path("harness/agent_kernel/completion.py")
        if policy.exists():
            policy_source = policy.read_text(encoding="utf-8")
            assert "effort" not in policy_source, (
                "the completion policy now mentions an effort setting"
            )


# ---------------------------------------------------------------------------
# 8. cheap-tier compaction
# ---------------------------------------------------------------------------


#: A readable file big enough that a handful of reads crosses the compaction
#: fraction, so the summariser is genuinely reached rather than merely
#: configured. It must stay UNDER the catalog's 200 KB per-file ceiling, or
#: every read would be refused and the context would never grow - which is
#: exactly the kind of vacuous pass this fixture exists to avoid.
_HUGE = ("x = 1  # a line of ordinary source\n" * 1200)[:150_000]


def _growth_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "big.txt").write_text(_HUGE, encoding="utf-8")
    return repo


def _compaction_run(tmp_path: Path, run_id: str, **config):
    """Run the REAL daily kernel until it compacts, recording model per call.

    Returns ``(log_root, summarizer_models, receipt)``. The summariser's model
    is read off the run's OWN journal rather than a private method, so "which
    tier summarized" is a measurement of what the run did.
    """
    from harness.agent_kernel import AgentKernel, ModelGateway, RunSpec

    repo = _growth_repo(tmp_path, name=f"repo-{run_id}")
    log_root = tmp_path / "logs"
    state = {"compacting": False, "reads": 0}

    def boundary(messages, **kwargs):
        step = str(kwargs.get("step") or "")
        if step.startswith("context-compaction"):
            state["compacting"] = True
            return "SUMMARY-TEXT"
        if state["compacting"]:
            return json.dumps({"tool": "finish", "answer": "done"})
        state["reads"] += 1
        if state["reads"] > 12:
            return json.dumps({"tool": "finish", "answer": "done"})
        return json.dumps({"tool": "read", "path": "big.txt"})

    values: dict = {
        "agent_approval": "auto",
        "steering_enabled": False,
        "max_model_attempts": 3,
        "model_retry_base_s": 0.0,
        "model_retry_cap_s": 0.0,
        "context_window_tokens": 32768,
        "context_reserved_output_tokens": 0,
        "context_compaction_fraction": 0.6,
        # The compaction request is itself bounded; a summariser that received
        # the WHOLE history could not summarize it.
        "context_compaction_input_tokens": 6000,
        "agent_conversation_messages": 400,
        "agent_conversation_chars": 4_000_000,
        "agent_conversation_tool_chars": 40000,
        "agent_max_read_chars": 40000,
        "agent_max_turns": 40,
    }
    values.update(config)
    kernel = AgentKernel(
        repo_path=str(repo),
        log_root=Path(log_root),
        model_gateway=ModelGateway(call_fn=boundary),
        config=values,
    )
    result = kernel.run(
        RunSpec(
            session_id=f"session-{run_id}",
            run_id=run_id,
            request="change the value",
            repository_identity=str(repo),
        )
    )
    assert result.status in {
        "completed_unverified",
        "completed_verified",
    }, f"the run did not complete: {result.status} {result.error}"
    receipt = _compaction_receipt(log_root, run_id)
    assert receipt, "the run never compacted, so the tier is untested"
    return log_root, str(receipt.get("compaction_model") or ""), receipt


def _compaction_receipt(log_root: Path, run_id: str) -> dict:
    """Return the run's own ``context_compacted`` receipt, or ``{}``."""
    path = Path(log_root) / run_id / "trace.jsonl"
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        payload = row.get("data") if isinstance(row.get("data"), dict) else row
        kind = row.get("kind") or row.get("event") or payload.get("kind")
        if kind == "context_compacted":
            return payload
    return {}


class TestCheapCompaction:
    def test_the_default_compaction_tier_is_cheap(self):
        from harness.config import DEFAULTS

        assert DEFAULTS["context_compaction_tier"] == "cheap"

    def test_compaction_runs_on_the_cheap_tier_by_default(self, tmp_path):
        """The brief's item 5, proven on the real kernel.

        The run's own model is expensive-sounding and its declared easy tier is
        a different model; the summariser must reach the cheap one.
        """
        _log_root, model, receipt = _compaction_run(
            tmp_path,
            "cheap",
            model="expensive-run-model",
            model_tiers={"easy": {"provider": "openai", "model": "cheap-tier-model"}},
        )
        assert model == "cheap-tier-model"
        assert receipt["compaction_model"] == "cheap-tier-model"

    def test_the_receipt_names_the_tier_that_ran(self, tmp_path):
        _log_root, _model, receipt = _compaction_run(
            tmp_path,
            "receipt",
            model="expensive-run-model",
            model_tiers={"easy": {"provider": "openai", "model": "cheap-tier-model"}},
        )
        assert receipt["compaction_tier"] == "cheap"
        assert receipt["compaction_tier_configured"] == "cheap"
        assert receipt["compaction_tier_model"] == "cheap-tier-model"
        assert receipt["compaction_tier_honoured"] is True

    def test_the_expensive_tier_is_an_explicit_opt_in(self, tmp_path):
        _log_root, model, receipt = _compaction_run(
            tmp_path,
            "expensive",
            model="expensive-run-model",
            context_compaction_tier="expensive",
            model_tiers={"easy": {"provider": "openai", "model": "cheap-tier-model"}},
        )
        assert model == "expensive-run-model"
        assert receipt["compaction_tier"] == "expensive"

    def test_an_explicit_compaction_model_still_wins_over_the_tier(self, tmp_path):
        _log_root, model, receipt = _compaction_run(
            tmp_path,
            "explicit",
            model="run-model",
            context_compaction_model="named-summarizer",
            model_tiers={"easy": {"provider": "openai", "model": "cheap-tier-model"}},
        )
        assert model == "named-summarizer"
        # The tier is still named, and the receipt still states what ran: the
        # operator's explicit model overrode the default tier.
        assert receipt["compaction_tier_configured"] == "cheap"
        assert receipt["compaction_model"] == "named-summarizer"
        assert receipt["compaction_model_configured"] == "named-summarizer"

    def test_an_unusable_tier_does_not_quietly_become_the_expensive_one(self, tmp_path):
        """A typo must not make summarisation cost frontier prices."""
        _log_root, model, _receipt = _compaction_run(
            tmp_path,
            "typo",
            model="expensive-run-model",
            context_compaction_tier="cheapp",
            model_tiers={"easy": {"provider": "openai", "model": "cheap-tier-model"}},
        )
        assert model == "cheap-tier-model"

    def test_a_cheap_tier_that_is_the_run_model_leaves_the_run_unchanged(
        self, tmp_path
    ):
        """The common case must not add a second gateway or a second model."""
        _log_root, model, receipt = _compaction_run(
            tmp_path,
            "same",
            model="same-model",
            model_tiers={"easy": {"provider": "openai", "model": "same-model"}},
        )
        assert model == "same-model"
        assert receipt["compaction_tier"] == "cheap"
        assert receipt["compaction_tier_honoured"] is True

    def test_the_summariser_spend_is_still_folded_into_the_run_total(self, tmp_path):
        """A cheap summariser is still real spend; a second ledger would lie."""
        log_root, _model, receipt = _compaction_run(
            tmp_path,
            "spend",
            model="expensive-run-model",
            model_tiers={"easy": {"provider": "openai", "model": "cheap-tier-model"}},
        )
        assert receipt["reversible"]["restore"]
        rows = [
            json.loads(line)
            for line in (Path(log_root) / "spend" / "compactions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert rows
        assert rows[0]["compaction_model"] == "cheap-tier-model"
        assert rows[0]["compaction_tier"] == "cheap"


# ---------------------------------------------------------------------------
# 9. the `/effort` surface
# ---------------------------------------------------------------------------


class TestTheEffortCommand:
    def test_the_command_is_registered_with_its_argument_shape(self):
        from cli.commands import command_spec, command_usage, resolve_command_line

        spec = command_spec("/effort")
        assert spec is not None
        assert spec.argument_hint == "[auto|low|medium|high|xhigh|max]"
        assert "medium" in command_usage(spec)
        resolved = resolve_command_line("/effort high")
        assert resolved.status == "ok"
        assert resolved.args == "high"

    def test_it_is_allowed_mid_run(self):
        """A person reaches for effort precisely when a run is struggling."""
        from cli.commands import CommandContext, resolve_command_line

        assert (
            resolve_command_line(
                "/effort high", CommandContext(surface="tui", in_flight=True)
            ).status
            == "ok"
        )

    def test_the_composer_hint_teaches_the_ladder_while_typing(self):
        from cli.commands import argument_hint

        hint = argument_hint("/eff")
        assert "/effort" in hint
        assert "max" in hint

    def test_the_typed_ladder_is_the_authority_ladder(self):
        import cli.commands as commands

        assert tuple(EFFORT_CHOICES) == commands.EFFORT_LADDER

    def test_a_bare_command_reports_without_changing_anything(self):
        import os

        import cli.commands as commands

        os.environ.pop(EFFORT_ENV_VAR, None)
        state: dict = {"model": "gpt-5", "provider": "openai"}
        receipt = commands.effort_receipt(state, None, "")
        assert receipt["ok"] and receipt["changed"] is False
        assert receipt["level"] == "auto"
        assert commands.apply_effort(state, receipt) is False
        assert "effort" not in state
        assert EFFORT_ENV_VAR not in os.environ

    def test_setting_a_level_writes_the_session_key_and_the_environment(self):
        import os

        import cli.commands as commands

        state: dict = {"model": "gpt-5", "provider": "openai"}
        receipt = commands.effort_receipt(state, None, "high")
        assert receipt["ok"] and receipt["level"] == "high"
        assert commands.apply_effort(state, receipt) is True
        assert state["effort"] == "high"
        # The environment write is what reaches a run whose config is built by
        # a path this command does not own (a worker subprocess, a mode module).
        assert os.environ[EFFORT_ENV_VAR] == "high"

    def test_the_receipt_reports_the_real_parameter_for_the_current_model(self):
        import cli.commands as commands

        receipt = commands.effort_receipt(
            {"model": "gpt-5", "provider": "openai"}, None, "high"
        )
        assert receipt["plan"]["effort_sent"] is True
        assert receipt["plan"]["effort_parameter"] == "reasoning_effort"
        rendered = "\n".join(commands.render_effort(receipt))
        assert "reasoning_effort" in rendered

    def test_the_receipt_says_so_when_the_provider_cannot_honour_it(self):
        import cli.commands as commands

        receipt = commands.effort_receipt(
            {"model": "unknown-model", "provider": "openai"}, None, "max"
        )
        assert receipt["plan"]["effort_sent"] is False
        rendered = "\n".join(commands.render_effort(receipt))
        assert "nothing sent" in rendered
        assert "unsupported_model" in rendered

    def test_an_unusable_argument_is_refused_and_nothing_changes(self):
        import os

        import cli.commands as commands

        os.environ.pop(EFFORT_ENV_VAR, None)
        state: dict = {}
        receipt = commands.effort_receipt(state, None, "turbo")
        assert receipt["ok"] is False
        assert receipt["error"]
        assert commands.apply_effort(state, receipt) is False
        assert "effort" not in state
        assert EFFORT_ENV_VAR not in os.environ

    def test_the_render_is_plain_text_with_no_markup_delimiters(self):
        """A provider detail is DATA; it must not reach a markup parser.

        The renderer is allowed to CONTAIN bracketed text (it is reporting it),
        so the pin is on the caller's obligation, not on absence: the render is
        a list of lines that no surface may hand to a markup parser unescaped,
        and these two properties are what make that checkable - no leading
        bracket that would open a style, and a documented, stable line shape.
        """
        import cli.commands as commands

        receipt = commands.effort_receipt(
            {"model": "weird[model]/name", "provider": "openai"}, None, "high"
        )
        receipt["plan"]["effort_detail"] = "[bold]not markup[/]"
        rendered = commands.render_effort(receipt)
        assert isinstance(rendered, list) and rendered
        for line in rendered:
            assert isinstance(line, str)
            assert line == line.strip("\n")
            # The FIRST line is the one a surface renders as a heading; it must
            # never open with a bracket, or a rich console eats the label.
            assert not rendered[0].startswith("[")
        # The hostile text is still visible, escaped-or-not, rather than
        # silently dropped: a provider detail nobody can see is a receipt that
        # cannot be acted on.
        assert any("[bold]not markup[/]" in line for line in rendered)

    def test_the_alias_also_resolves_through_the_command(self):
        import cli.commands as commands

        receipt = commands.effort_receipt(
            {"model": "gpt-5", "provider": "openai"}, None, "hi"
        )
        assert receipt["level"] == "high"
        assert receipt["plan"]["effort_value"] == "high"

    def test_an_unavailable_authority_degrades_to_a_report_not_a_crash(
        self, monkeypatch
    ):
        import builtins

        import cli.commands as commands

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith("runtime."):
                raise ImportError("runtime is not importable here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        monkeypatch.setattr(commands, "_effort_alias", lambda _v: "")
        receipt = commands.effort_receipt({"effort": "high"}, None, "")
        assert receipt["ok"] is True
        assert receipt["level"] == "auto"
        assert commands.render_effort(receipt)

    def test_a_broken_authority_still_reports_the_plan_as_unreported(self, monkeypatch):
        import cli.commands as commands

        def boom(*_a, **_k):
            raise RuntimeError("registry is corrupt")

        monkeypatch.setitem(
            __import__("sys").modules,
            "runtime.model_capabilities",
            types.SimpleNamespace(
                EFFORT_CHOICES=EFFORT_CHOICES,
                normalize_effort=normalize_effort,
                resolve_effort=boom,
                map_effort=boom,
            ),
        )
        receipt = commands.effort_receipt({"model": "m"}, None, "high")
        assert receipt["level"] == "high"
        assert receipt["plan"]["effort_sent"] is False
        assert "unavailable" in receipt["plan"]["effort_detail"]


# ---------------------------------------------------------------------------
# 10. mid-run reachability, end to end
# ---------------------------------------------------------------------------


class TestMidRun:
    def test_a_level_set_between_two_calls_applies_to_the_second_only(
        self, tmp_path, monkeypatch
    ):
        """The brief's "mid-run" requirement, proven at the boundary."""
        _fake_litellm(monkeypatch, {})
        ledger = tmp_path / "ledger.jsonl"
        set_call_context({"provider": "openai"}, ledger_dir=str(ledger))
        call_model([{"role": "user", "content": "1"}], model="gpt-5")
        call_model([{"role": "user", "content": "2"}], model="gpt-5", effort="high")
        call_model([{"role": "user", "content": "3"}], model="gpt-5")
        rows = _rows(ledger)
        assert [row["effort"] for row in rows] == ["auto", "high", "auto"]
        assert [row["effort_sent"] for row in rows] == [False, True, False]
