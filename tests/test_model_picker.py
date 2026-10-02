"""The model picker and the effort VARIANT - the seven required proofs.

One class per required proof, one test per behaviour, named after the
behaviour rather than the function. Every test is host-only: no Docker, no
provider, no network. The provider-shaped assertions install a fake litellm
through the router's own seam, so what is measured is the REQUEST the provider
would have received - not a comment about it.

The list of proofs, in the order the brief names them:

1. ``TestThePickerFiltersAndSelects``          - the picker filters and selects
2. ``TestTheVariantIsChainedNotASecondScreen`` - ``variant.cycle`` walks the
   provider's own vocabulary
3. ``TestEffortMapsToARealParameterOrSaysWhy`` - a real parameter, or a plain
   refusal
4. ``TestAnUnrecognisedModelIdIsRefused``      - refused, current model kept
5. ``TestTheLedgerRecordsTheEffortOnEveryRow`` - ledger + trace, every row
6. ``TestJsonExposesTheEffort``                - ``--json`` publishes it
7. ``TestTheVerifierGateIsIdenticalAtEveryLevel`` - identical at low/medium/high

Plus the honesty pins that are not in the brief but that the brief's own rules
demand: markup safety (a render failure must never delete a message), the
anti-clutter rule, the closed rejection vocabulary, "never store a key", and
the fact that the picker is not a wizard.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest

import runtime.model_capabilities as mc
from cli import models as M
from harness.agent_loop_step import LoopEnvironment, ToolOutcome
from harness.trace import TraceLogger
from runtime.model_router import call_model, set_call_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_LITELLM_KEYS = (
    "reasoning_effort",
    "thinking",
    "thinking_budget",
)


def _fake_litellm(monkeypatch, capture: dict) -> None:
    """Install a fake litellm that records the request it was handed."""

    def completion(**kwargs):
        capture.update(kwargs)
        msg = types.SimpleNamespace(content="fake", tool_calls=None)
        choice = types.SimpleNamespace(message=msg, finish_reason="stop")
        usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=2)
        return types.SimpleNamespace(choices=[choice], usage=usage, _hidden_params={})

    monkeypatch.setitem(
        sys.modules, "litellm", types.SimpleNamespace(completion=completion)
    )


def _rows(path: Path) -> list:
    """Read a JSONL file, skipping a torn tail."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@pytest.fixture(autouse=True)
def _authoritative_knobs(monkeypatch):
    """Every test starts from the built-in knob table and no ambient effort."""
    monkeypatch.delenv(mc.EFFORT_ENV_VAR, raising=False)
    mc.reset_effort_knobs()
    yield
    mc.reset_effort_knobs()


def _repo_path(relative: str) -> Path:
    """Resolve a repository-relative path from this test file's location."""
    return Path(__file__).resolve().parents[1] / relative


def _catalog(favorites=(), recent=(), **overrides):
    """A small, fully offline catalog for the picker tests.

    ``favorites``/``recent`` are passed to the catalog builder directly rather
    than through the settings mapping, so a test states which source it means.
    """
    settings = {
        "model": "gpt-4o",
        "provider": "openai",
        "model_tiers": {
            "easy": {"provider": "openai", "model": "gpt-4o-mini"},
            "hard": {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"},
        },
    }
    settings.update(overrides)
    return M.model_catalog(
        settings,
        favorites=tuple(favorites),
        recent=tuple(recent),
        include_registry=False,
    )


def _many_catalog(count: int = 20):
    """A catalog large enough to overflow the visible bound."""
    return [
        M.ModelEntry(
            model=f"model-{index:02d}", provider="openai", sources=("registry",)
        )
        for index in range(count)
    ]


def _row_of(picker, model_id: str) -> int:
    """The row index of a model id. Selecting BY ID, never by a fixed index.

    Provider blocks are ordered deterministically, so a hard-coded index is a
    pin on the sort order rather than on the behaviour under test.
    """
    for row in picker.matches():
        if row.entry.id == model_id:
            return row.index
    raise AssertionError(f"{model_id} is not in the picker: {picker.matches()}")


# ---------------------------------------------------------------------------
# 1. The picker filters and selects
# ---------------------------------------------------------------------------


class TestThePickerFiltersAndSelects:
    def test_a_query_narrows_the_list_and_the_selection_is_the_first_row(self):
        catalog = _catalog(favorites=("openai/gpt-4o-mini",), recent=("openai/gpt-4o",))
        picker = M.ModelPicker(
            catalog, current_model="gpt-4o", current_provider="openai"
        )
        assert len(picker.matches()) >= 2

        picker.set_query("haiku")
        # 'haiku' matches nothing in this catalog, so the list is EMPTY rather
        # than unfiltered: a filter that shows everything is not a filter.
        assert picker.matches() == []
        assert picker.key("enter") == "no_results"
        assert picker.selected_entry() is None

    def test_a_query_ranks_inside_a_group_and_never_across_groups(self):
        catalog = _catalog(
            favorites=("anthropic/claude-3-5-sonnet-20241022",),
            recent=("openai/gpt-4o-mini",),
        )
        picker = M.ModelPicker(
            catalog, current_model="gpt-4o", current_provider="openai"
        )
        picker.set_query("gpt")
        groups = [row.group for row in picker.matches()]
        # The favourite is out of the 'gpt' query; the recent/provider rows are
        # in. Group ORDER is still favourites, recent, provider.
        assert groups == sorted(groups, key=lambda name: M.PICKER_GROUPS.index(name))
        assert "favorites" not in groups
        assert set(groups) <= {"recent", "provider"}

    def test_at_most_eight_rows_are_visible_and_the_omission_is_stated(self):
        picker = M.ModelPicker(_many_catalog(40))
        assert len(picker.visible()) == M.MAX_VISIBLE_MODELS == 8
        assert picker.hidden_count() == 32
        rendered = "\n".join(picker.lines())
        assert "+32 more not shown" in rendered

    def test_a_short_catalog_reports_nothing_hidden(self):
        picker = M.ModelPicker(_many_catalog(3))
        assert len(picker.visible()) == 3
        assert picker.hidden_count() == 0
        assert "more not shown" not in "\n".join(picker.lines())

    def test_a_group_of_two_or_fewer_rows_renders_no_header(self):
        """The anti-clutter rule, applied to chrome and not to models.

        A header is a row of chrome spent on a small number of facts. The two
        MODELS are still listed; only the label is withheld. Providers are
        judged separately, because a provider block is the group a person sees.
        """
        catalog = [
            M.ModelEntry("gpt-4o", "openai", ("settings",)),
            M.ModelEntry("gpt-4o-mini", "openai", ("settings",)),
            M.ModelEntry("a", "anthropic", ("settings",)),
            M.ModelEntry("b", "anthropic", ("settings",)),
            M.ModelEntry("c", "anthropic", ("settings",)),
        ]
        picker = M.ModelPicker(catalog)
        sections = {section.provider: section for section in picker.sections()}
        assert sections["openai"].size == 2
        assert sections["openai"].header == ""  # two rows earn no header
        assert sections["anthropic"].size == 3
        assert sections["anthropic"].header == "anthropic"
        assert len(picker.visible()) == 5  # and every model is still there

    def test_a_group_of_three_renders_its_provider_as_the_header(self):
        catalog = [
            M.ModelEntry("a", "anthropic", ("settings",)),
            M.ModelEntry("b", "anthropic", ("settings",)),
            M.ModelEntry("c", "anthropic", ("settings",)),
        ]
        picker = M.ModelPicker(catalog)
        sections = picker.sections()
        assert [section.header for section in sections] == ["anthropic"]
        assert sections[0].size == 3

    def test_favourites_and_recent_come_before_the_provider_groups(self):
        catalog = _catalog(
            favorites=("anthropic/claude-3-5-sonnet-20241022",),
            recent=("openai/gpt-4o-mini",),
        )
        picker = M.ModelPicker(catalog)
        assert [section.group for section in picker.sections()] == [
            "favorites",
            "recent",
            "provider",
        ]

    def test_choosing_a_row_sets_the_model_immediately_with_no_confirm_step(self):
        catalog = _catalog(favorites=("openai/gpt-4o-mini",))
        picker = M.ModelPicker(
            catalog, current_model="gpt-4o", current_provider="openai"
        )
        assert picker.matches()[0].entry.model == "gpt-4o-mini"

        selection = picker.choose()

        assert selection.ok is True
        assert selection.model == "gpt-4o-mini"
        assert picker.current_model == "gpt-4o-mini"  # in force on return
        assert picker.variant() is not None

    def test_the_picker_is_keyboard_driven_and_every_key_returns_a_known_action(self):
        picker = M.ModelPicker(_many_catalog(5))
        seen = []
        for key in ("j", "j", "k", "up", "down", "home", "end", "pageup", "pagedown"):
            seen.append(picker.key(key))
        assert set(seen) <= set(M.PICKER_ACTIONS)
        assert "move" in seen
        assert picker.key("enter") == "select"
        assert picker.key("escape") == "dismiss"
        assert picker.active is False

    def test_an_inactive_picker_ignores_every_key_including_printables(self):
        picker = M.ModelPicker(_many_catalog(5))
        picker.close()
        for key in ("j", "a", "enter", M.VARIANT_CYCLE_KEYBIND):
            assert picker.key(key) == "inactive"
        assert picker.query == ""
        assert picker.cursor == 0

    def test_a_free_text_field_accepts_a_model_id_the_catalog_never_saw(self):
        picker = M.ModelPicker(
            _catalog(), current_model="gpt-4o", current_provider="openai"
        )
        selection = picker.submit_text("z-ai/glm-5.3-free")
        assert selection.ok is True
        assert selection.model == "glm-5.3-free"
        assert selection.provider == "z-ai"
        assert picker.current_model == "glm-5.3-free"

    def test_the_picker_does_no_io_and_cannot_block(self):
        """A picker is a list, not a wizard: nothing here can block a session.

        Asserted on the source, because "it has no await" is a property of the
        code rather than of a call, and a future edit that adds a network
        lookup behind a key press would otherwise be invisible to a behavioural
        test.
        """
        source = Path(M.__file__).read_text(encoding="utf-8")
        for banned in ("await ", "async def", "input(", "time.sleep", "requests"):
            assert banned not in source, f"the picker must not contain {banned!r}"


# ---------------------------------------------------------------------------
# 2. The variant is chained immediately after model selection
# ---------------------------------------------------------------------------


class TestTheVariantIsChainedNotASecondScreen:
    def test_the_keybind_is_the_one_the_authority_declares(self):
        assert M.VARIANT_CYCLE_KEYBIND == mc.variant_keybind() == "variant.cycle"
        assert M.EFFORT_CYCLE_KEY == M.VARIANT_CYCLE_KEYBIND
        assert M.VARIANT_CYCLE_KEYBIND in M.PICKER_KEYS

    def test_choosing_a_model_returns_its_effort_variant_on_the_same_value(self):
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-5", "openai")],
            current_model="gpt-5",
            current_provider="openai",
        )
        selection = picker.choose()
        # No second screen, no second return, no dialog in between: the variant
        # rides the selection itself.
        assert selection.variant is not None
        assert selection.variant.vocabulary == (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        )
        assert selection.to_dict()["variant"]["level"] == selection.variant.level

    def test_variant_cycle_walks_the_models_own_vocabulary_and_wraps(self):
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-5", "openai")],
            current_model="gpt-5",
            current_provider="openai",
            current_effort="none",
        )
        seen = []
        for _ in range(len(("none", "minimal", "low", "medium", "high", "xhigh")) + 1):
            picker.key(M.VARIANT_CYCLE_KEYBIND)
            seen.append(picker.current_effort)
        assert seen == ["minimal", "low", "medium", "high", "xhigh", "none", "minimal"]

    def test_variant_cycle_on_anthropic_walks_only_its_own_two_rungs(self):
        picker = M.ModelPicker(
            [M.ModelEntry("claude-sonnet-4", "anthropic")],
            current_model="claude-sonnet-4",
            current_provider="anthropic",
            current_effort="high",
        )
        picker.key(M.VARIANT_CYCLE_KEYBIND)
        assert picker.current_effort == "max"
        picker.key(M.VARIANT_CYCLE_KEYBIND)
        assert picker.current_effort == "high"

    def test_variant_cycle_on_google_walks_low_and_high(self):
        picker = M.ModelPicker(
            [M.ModelEntry("gemini-2.5-pro", "google")],
            current_model="gemini-2.5-pro",
            current_provider="google",
            current_effort="low",
        )
        assert picker.cycle().level == "high"
        assert picker.cycle().level == "low"

    def test_a_model_with_no_declared_knob_is_stable_rather_than_surprising(self):
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-4o", "openai")],
            current_model="gpt-4o",
            current_provider="openai",
        )
        variant = picker.cycle()
        assert variant.vocabulary == ("auto",)
        assert variant.level == "auto"
        assert variant.honoured is False
        assert picker.cycle().level == "auto"

    def test_cycling_a_model_with_no_knob_sends_nothing_at_all(self):
        """The knob-less case reaches the wire as an unchanged request."""
        plan = mc.map_picker_effort("high", "gpt-4o", provider="openai")
        assert plan.status == mc.EFFORT_UNSUPPORTED_MODEL
        assert plan.parameters == {}
        variant = M.effort_variant("high", "gpt-4o", "openai")
        assert variant.honoured is False
        assert variant.parameter == ""
        assert "no effort parameter" not in "\n".join(variant.lines())

    def test_changing_model_carries_the_effort_and_names_what_happened(self):
        """A carried level is kept, mapped, or dropped - and each is stated.

        `low` is the level that separates the three answers: it is an OpenAI
        choice, a Google choice, and NOT an Anthropic choice - but Anthropic's
        `thinking` knob will still send a real budget for it.
        """
        catalog = [
            M.ModelEntry("gpt-5", "openai"),
            M.ModelEntry("claude-sonnet-4", "anthropic"),
            M.ModelEntry("gemini-2.5-pro", "google"),
        ]
        picker = M.ModelPicker(
            catalog,
            current_model="gpt-5",
            current_provider="openai",
            current_effort="low",
        )
        # exact: `low` is one of Google's two choices, so it is kept as-is.
        picker.move_to(_row_of(picker, "google/gemini-2.5-pro"))
        exact = picker.choose()
        assert exact.model == "gemini-2.5-pro"
        assert exact.carry["carried"] == mc.EFFORT_VARIANT_EXACT
        assert exact.carry["level"] == "low"
        assert exact.carry["sent"] is True
        assert exact.carry["parameter"] == "thinking_budget"

        # mapped: `low` is NOT one of Anthropic's two choices, but its
        # `thinking` knob WILL send a real budget for it - so the level is kept
        # and the receipt names the parameter, instead of discarding a level the
        # provider would have honoured.
        picker.move_to(_row_of(picker, "anthropic/claude-sonnet-4"))
        mapped = picker.choose()
        assert mapped.model == "claude-sonnet-4"
        assert mapped.carry["carried"] == mc.EFFORT_VARIANT_MAPPED
        assert mapped.carry["level"] == "low"
        assert mapped.carry["sent"] is True
        assert mapped.carry["parameter"] == "thinking"
        assert mapped.carry["value"] == {"type": "enabled", "budget_tokens": 1024}
        assert "thinking" in mapped.carry["detail"]

    def test_a_level_the_new_model_cannot_honour_is_dropped_with_a_reason(self):
        """`minimal` is a real OpenAI choice; Anthropic has no such rung.

        The reachable path is the one a person takes: pick `minimal` on
        OpenAI, then switch to Claude. Anthropic cannot honour it, so the
        receipt says so and names the level it gave up rather than pretending.
        """
        picker = M.ModelPicker(
            [
                M.ModelEntry("gpt-5", "openai"),
                M.ModelEntry("claude-sonnet-4", "anthropic"),
            ],
            current_model="gpt-5",
            current_provider="openai",
            current_effort="minimal",
        )
        picker.move_to(_row_of(picker, "anthropic/claude-sonnet-4"))
        dropped = picker.choose()
        assert dropped.carry["carried"] == mc.EFFORT_VARIANT_DROPPED
        assert dropped.carry["previous"] == "minimal"
        assert dropped.carry["level"] == "high"  # Anthropic's first choice
        assert "minimal" in dropped.carry["detail"]
        assert "rather than clamped" in dropped.carry["detail"]
        # The FALLBACK level is itself honoured, and the receipt says so rather
        # than leaving the reader to guess whether anything was sent.
        assert dropped.carry["sent"] is True
        assert dropped.carry["parameter"] == "thinking"
        assert dropped.variant.honoured is True


# ---------------------------------------------------------------------------
# 3. Effort maps to a real parameter, or says plainly that it cannot
# ---------------------------------------------------------------------------


class TestEffortMapsToARealParameterOrSaysWhy:
    @pytest.mark.parametrize(
        "model,provider,level,parameter,value",
        [
            ("gpt-5", "openai", "low", "reasoning_effort", "low"),
            ("gpt-5", "openai", "medium", "reasoning_effort", "medium"),
            ("gpt-5", "openai", "high", "reasoning_effort", "high"),
            (
                "claude-sonnet-4",
                "anthropic",
                "high",
                "thinking",
                {"type": "enabled", "budget_tokens": 16384},
            ),
            ("gemini-2.5-pro", "google", "low", "thinking_budget", 1024),
            ("gemini-2.5-pro", "google", "high", "thinking_budget", 16384),
        ],
    )
    def test_a_honoured_level_names_the_parameter_and_the_value(
        self, model, provider, level, parameter, value
    ):
        variant = M.effort_variant(level, model, provider)
        assert variant.honoured is True
        assert variant.parameter == parameter
        assert variant.value == value
        assert variant.status == mc.EFFORT_SENT

    @pytest.mark.parametrize("level", ["xhigh", "minimal", "max"])
    def test_a_level_this_family_cannot_honour_says_so_and_sends_nothing(self, level):
        variant = M.effort_variant(level, "gpt-5", "openai")
        assert variant.honoured is False
        assert variant.status == mc.EFFORT_UNSUPPORTED_LEVEL
        assert variant.value is None
        assert "reasoning_effort" in variant.detail
        assert "rather than clamped" in variant.detail
        assert "sends:" not in "\n".join(variant.lines())

    def test_none_means_send_nothing_and_is_not_reported_as_a_parameter(self):
        variant = M.effort_variant("none", "gpt-5", "openai")
        assert variant.honoured is False
        assert variant.status == mc.EFFORT_AUTO
        assert variant.parameter == ""
        rendered = "\n".join(variant.lines())
        assert "nothing sent: auto" in rendered
        assert "sends:" not in rendered

    def test_the_receipt_names_the_parameter_even_when_it_was_not_sent(self):
        """A surface can then say WHICH parameter was declined, not just 'no'."""
        variant = M.effort_variant("xhigh", "gpt-5", "openai")
        assert variant.parameter == "reasoning_effort"
        assert "this provider sends reasoning_effort for other levels" in "\n".join(
            variant.lines()
        )

    def test_a_model_with_no_knob_reports_unsupported_model_not_a_parameter(self):
        variant = M.effort_variant("high", "some-unknown-model", "openai")
        assert variant.honoured is False
        assert variant.status == mc.EFFORT_UNSUPPORTED_MODEL
        assert "no declared effort knob" in variant.detail

    def test_the_chosen_level_and_the_honoured_level_are_separate_fields(self):
        """The field that may not lie is `honoured`, and it is not `level`."""
        receipt = M.effort_variant("xhigh", "gpt-5", "openai").to_dict()
        assert receipt["level"] == "xhigh"
        assert receipt["honoured"] is False
        assert receipt["in_vocabulary"] is True
        assert receipt["effective_effort"] == "auto"

    def test_an_unhonoured_choice_records_auto_in_a_run_config(self):
        """A picker must never write a level the router would call a typo.

        `minimal` is an OpenAI CHOICE that is not a wire rung at all, so
        `resolve_effort` would report `invalid:minimal` - a typo the user never
        typed, produced by this product's own picker.
        """
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-5", "openai")],
            current_model="gpt-5",
            current_provider="openai",
            current_effort="minimal",
        )
        assert picker.config_effort() == "auto"
        assert mc.resolve_effort({"effort": picker.config_effort()}) == (
            "auto",
            "config",
        )
        # The control: what writing the CHOICE would have produced.
        assert mc.resolve_effort({"effort": "minimal"})[1] == "invalid:minimal"

    def test_a_honoured_choice_records_the_wire_rung_in_a_run_config(self):
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-5", "openai")],
            current_model="gpt-5",
            current_provider="openai",
            current_effort="high",
        )
        assert picker.config_effort() == "high"
        assert mc.resolve_effort({"effort": picker.config_effort()}) == (
            "high",
            "config",
        )

    def test_the_real_parameter_reaches_the_provider_request(
        self, tmp_path, monkeypatch
    ):
        """The end of the chain: what the provider would actually receive.

        Driven through the REAL router with a fake litellm, so the assertion is
        about the request kwargs, not about a function's return value.
        """
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-5", "openai")],
            current_model="gpt-5",
            current_provider="openai",
            current_effort="high",
        )
        set_call_context(
            {"provider": "openai", "effort": picker.config_effort()},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        assert call_model([{"role": "user", "content": "x"}], model="gpt-5") == "fake"
        assert capture["reasoning_effort"] == "high"

    def test_an_unhonoured_choice_puts_nothing_on_the_request(
        self, tmp_path, monkeypatch
    ):
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-5", "openai")],
            current_model="gpt-5",
            current_provider="openai",
            current_effort="xhigh",
        )
        set_call_context(
            {"provider": "openai", "effort": picker.config_effort()},
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5")
        for key in FAKE_LITELLM_KEYS:
            assert key not in capture, f"{key} must not be sent for an unhonoured level"


# ---------------------------------------------------------------------------
# 4. An unrecognised model id is refused, and the current model is kept
# ---------------------------------------------------------------------------


class TestAnUnrecognisedModelIdIsRefused:
    @pytest.mark.parametrize(
        "typed,reason",
        [
            ("", "empty"),
            ("   ", "empty"),
            ("gpt 4o", "unsafe"),
            ("../../etc/passwd", "unsafe"),
            ("nul\x00byte", "unsafe"),
            ("http://example.com/model", "unsafe"),
            ("openai/", "incomplete"),
            ("/gpt-4o", "incomplete"),
            ("gpt-4o-mnii", "typo"),
        ],
    )
    def test_every_refusal_comes_from_the_closed_vocabulary(self, typed, reason):
        catalog = M.model_catalog(
            {"model": "gpt-4o-mini", "provider": "openai"}, include_registry=False
        )
        verdict = M.model_id_verdict(typed, catalog, current_model="gpt-4o")
        assert verdict.accepted is False
        assert verdict.reason == reason
        assert verdict.reason in M.MODEL_ID_REJECTIONS
        assert verdict.message

    def test_a_refusal_keeps_the_current_model_on_every_path(self):
        catalog = M.model_catalog(
            {"model": "gpt-4o-mini", "provider": "openai"}, include_registry=False
        )
        for typed in ("", "gpt 4o", "../x", "openai/", "gpt-4o-mnii"):
            selection = M.select_model(
                typed,
                catalog,
                current_model="gpt-4o",
                current_provider="openai",
            )
            assert selection.ok is False
            assert selection.model == "gpt-4o"  # never cleared
            assert selection.kept_current is True

    def test_a_refusal_leaves_the_picker_on_the_model_it_had(self):
        picker = M.ModelPicker(
            M.model_catalog(
                {"model": "gpt-4o-mini", "provider": "openai"}, include_registry=False
            ),
            current_model="gpt-4o",
            current_provider="openai",
        )
        selection = picker.submit_text("gpt-4o-mnii")
        assert selection.ok is False
        assert picker.current_model == "gpt-4o"
        assert picker.query == "gpt-4o-mnii"  # the text stays so it can be fixed
        assert selection.variant is not None  # the effort in force is unchanged

    def test_a_refusal_never_raises_for_a_hostile_value(self):
        for typed in (None, 0, [], {}, object(), "\n\t", "a" * 5000):
            selection = M.select_model(typed, [], current_model="gpt-4o")
            assert selection.ok in (True, False)
            if not selection.ok:
                assert selection.model == "gpt-4o"

    def test_a_typo_offers_the_models_that_were_meant(self):
        catalog = M.model_catalog(
            {"model": "gpt-4o-mini", "provider": "openai"}, include_registry=False
        )
        verdict = M.model_id_verdict("gpt-4o-mnii", catalog, current_model="gpt-4o")
        assert verdict.reason == "typo"
        assert "openai/gpt-4o-mini" in verdict.suggestions
        assert "did you mean" in "\n".join(
            M.ModelSelection(
                ok=False,
                model="gpt-4o",
                reason=verdict.reason,
                message=verdict.message,
                suggestions=verdict.suggestions,
            ).lines()
        )

    def test_a_session_with_no_model_is_told_so_rather_than_left_guessing(self):
        """'Never leave the user with no model' also covers starting with none."""
        selection = M.select_model("gpt 4o", [], current_model="")
        assert selection.ok is False
        assert selection.reason == "unsafe"
        assert selection.kept_current is False
        assert "unset" in selection.message

    def test_a_model_exactly_in_the_catalog_is_accepted_as_itself(self):
        catalog = M.model_catalog(
            {"model": "gpt-4o", "provider": "openai"}, include_registry=False
        )
        verdict = M.model_id_verdict("openai/gpt-4o", catalog, current_model="")
        assert verdict.accepted is True
        assert verdict.model == "gpt-4o"
        assert verdict.provider == "openai"

    def test_the_accepted_row_says_it_was_not_in_the_catalog(self):
        verdict = M.model_id_verdict("z-ai/glm-9", [], current_model="gpt-4o")
        assert verdict.accepted is True
        assert "not in this install's catalog" in verdict.message


# ---------------------------------------------------------------------------
# 5. The ledger records the effort on every row
# ---------------------------------------------------------------------------


class _NamedEffortBoundary:
    """A boundary that EXPLICITLY names `effort` (the ModelClient rule)."""

    def __init__(self, model: str = "gpt-5"):
        self.model = model
        self.seen: list = []
        self._last: dict = {}

    def __call__(
        self,
        messages,
        *,
        difficulty_hint=None,
        provider=None,
        model=None,
        api_key=None,
        effort=None,
    ):
        self.seen.append(effort)
        self._last = {
            "model": model or self.model,
            "provider": provider or "openai",
            "tokens": 7,
            "cost_usd": 0.001,
            "effort": effort or "auto",
            "effort_status": "sent" if effort not in (None, "auto") else "auto",
            "effort_sent": effort not in (None, "auto"),
            "effort_parameter": "reasoning_effort"
            if effort not in (None, "auto")
            else "",
        }
        return "fake reply"

    def get_last_usage(self):
        return dict(self._last)


class TestTheLedgerRecordsTheEffortOnEveryRow:
    def test_every_model_call_record_carries_the_level(self, tmp_path):
        from harness.deps import set_call_model
        from harness.model_client import ModelClient

        boundary = _NamedEffortBoundary()
        set_call_model(boundary)
        try:
            client = ModelClient(
                TraceLogger(tmp_path), {"model": "gpt-5", "effort": "high"}
            )
            client.call([{"role": "user", "content": "one"}], step="s1")
            client.call([{"role": "user", "content": "two"}], step="s2", effort="low")
        finally:
            set_call_model(None)
        assert [row["effort"] for row in client.model_calls] == ["high", "low"]
        assert all(row["effort_status"] == "sent" for row in client.model_calls)
        assert all(
            row["effort_parameter"] == "reasoning_effort" for row in client.model_calls
        )
        assert boundary.seen == ["high", "low"]

    def test_every_trace_row_of_a_call_carries_the_effort(self, tmp_path):
        from harness.deps import set_call_model
        from harness.model_client import ModelClient

        set_call_model(_NamedEffortBoundary())
        try:
            client = ModelClient(
                TraceLogger(tmp_path), {"model": "gpt-5", "effort": "high"}
            )
            client.call([{"role": "user", "content": "one"}], step="s1")
        finally:
            set_call_model(None)
        responses = [
            row
            for row in _rows(tmp_path / "trace.jsonl")
            if row.get("kind") == "model_response"
        ]
        assert responses, "the model_response row must exist"
        for row in responses:
            usage = row["data"]["usage"]
            assert usage["effort"] == "high"
            assert usage["effort_status"] == "sent"
            assert usage["effort_sent"] is True
            assert usage["effort_parameter"] == "reasoning_effort"

    def test_every_router_ledger_row_carries_the_level(self, tmp_path, monkeypatch):
        """The runtime's own per-call ledger, read back off disk."""
        capture: dict = {}
        _fake_litellm(monkeypatch, capture)
        ledger = tmp_path / "model_ledger.jsonl"
        set_call_context(
            {"provider": "openai", "effort": "high"}, ledger_dir=str(ledger)
        )
        call_model([{"role": "user", "content": "x"}], model="gpt-5")
        call_model([{"role": "user", "content": "y"}], model="gpt-5", effort="low")
        rows = _rows(ledger)
        assert len(rows) == 2
        for row in rows:
            assert row["effort"] in ("high", "low")
            assert row["effort_status"] == "sent"
            assert row["effort_sent"] is True
            assert row["effort_parameter"] == "reasoning_effort"

    def test_a_failed_attempt_is_still_receipted_for_its_effort(
        self, tmp_path, monkeypatch
    ):
        """A cost claim about a FAILED call needs the same explanation."""

        def exploding(**kwargs):
            raise RuntimeError("upstream said no")

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=exploding)
        )
        ledger = tmp_path / "model_ledger.jsonl"
        set_call_context(
            {"provider": "openai", "effort": "high"}, ledger_dir=str(ledger)
        )
        with pytest.raises(RuntimeError):
            call_model([{"role": "user", "content": "x"}], model="gpt-5")
        rows = [row for row in _rows(ledger) if "effort" in row]
        assert rows, "a failed attempt must still be receipted"
        assert rows[0]["effort"] == "high"

    def test_the_effort_is_part_of_the_resume_identity(self):
        """Resuming a high-effort run at low effort is a lie about the run."""
        from runtime import checkpoint as rt_checkpoint

        high = rt_checkpoint.effort_identity({"effort": "high"})
        low = rt_checkpoint.effort_identity({"effort": "low"})
        assert high and low and high != low
        # One identity for two spellings of the same rung, so an alias is not a
        # different run.
        assert high == rt_checkpoint.effort_identity({"effort": "hi"})

    def test_the_picker_records_a_rung_that_resolves_to_that_identity(self):
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-5", "openai")],
            current_model="gpt-5",
            current_provider="openai",
            current_effort="high",
        )
        from runtime import checkpoint as rt_checkpoint

        assert rt_checkpoint.effort_identity(
            {"effort": picker.config_effort()}
        ) == rt_checkpoint.effort_identity({"effort": "high"})


# ---------------------------------------------------------------------------
# 6. `--json` exposes the effort
# ---------------------------------------------------------------------------


class TestJsonExposesTheEffort:
    def _result(self, calls):
        from cli.main import _effort_json

        return _effort_json(types.SimpleNamespace(model_calls=calls))

    def test_the_json_document_publishes_the_level_and_the_parameter(self):
        picker = M.ModelPicker(
            [M.ModelEntry("gpt-5", "openai")],
            current_model="gpt-5",
            current_provider="openai",
            current_effort="high",
        )
        document = self._result(
            [
                {
                    "step": "agent-turn-1",
                    "call_index": 1,
                    "model": "gpt-5",
                    "effort": picker.config_effort(),
                    "effort_status": "sent",
                    "effort_sent": True,
                    "effort_parameter": "reasoning_effort",
                    "tokens": 10,
                    "cost": 0.002,
                }
            ]
        )
        assert document["level"] == "high"
        assert document["parameters"] == ["reasoning_effort"]
        assert document["supported"] is True
        assert document["receipts"][0]["effort_parameter"] == "reasoning_effort"

    def test_the_json_document_can_say_asked_for_high_and_got_nothing(self):
        """The whole point of the receipt: two different runs, two documents."""
        asked = self._result(
            [
                {
                    "step": "s1",
                    "call_index": 1,
                    "model": "gpt-4o",
                    "effort": "auto",
                    "effort_status": "unsupported_model",
                    "effort_sent": False,
                    "effort_parameter": "",
                    "tokens": 10,
                    "cost": 0.002,
                }
            ]
        )
        assert asked["supported"] is False
        assert asked["statuses"] == ["unsupported_model"]
        assert asked["parameters"] == []

    def test_a_boundary_that_knew_nothing_about_effort_reports_not_supported(self):
        document = self._result(
            [{"step": "s1", "call_index": 1, "model": "x", "tokens": 1}]
        )
        assert document["supported"] is False
        assert document["statuses"] == []
        assert document["parameters"] == []
        assert document["receipts"][0]["effort"] is None
        assert "level" in document  # never a MISSING key


# ---------------------------------------------------------------------------
# 7. The verifier gate is identical at every effort level
# ---------------------------------------------------------------------------


class _ScriptedEnv(LoopEnvironment):
    """A `LoopEnvironment` whose only two capabilities are scripted.

    Subclasses the real base class so every OTHER call resolves to the shipped
    safe default. A hand-rolled stand-in that happened to be missing a method
    would fail for the wrong reason - and the missing `steering` method is
    exactly what a first draft of this test got wrong.
    """

    def __init__(self, evidence: dict):
        self.evidence = dict(evidence)
        self.asked = 0

    def ask(self, messages, *, step):
        self.asked += 1
        return '{"tool": "done", "answer": "done"}'

    def invoke(self, name, args, *, turn):
        if name == "verify":
            return ToolOutcome(ok=True, output="", detail=dict(self.evidence))
        return ToolOutcome(ok=True, output="")


def _run_step(effort: str, evidence: dict) -> str:
    from harness.agent_loop_step import step, terminal_status_of

    config = {
        "agent_strategy": "daily",
        "effort": effort,
        "target_test": "tests/test_x.py::test_y",
        "max_turns": 3,
    }
    events = step(
        [{"role": "user", "content": "fix it"}], _ScriptedEnv(evidence), config
    )
    return terminal_status_of(events)


CLEAN = {"target_passed": True, "regression_passed": True, "flaky": False}
DIRTY = {"target_passed": True, "regression_passed": False, "flaky": False}


class TestTheVerifierGateIsIdenticalAtEveryLevel:
    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    def test_a_clean_verifier_run_is_verified_at_every_level(self, level):
        assert _run_step(level, CLEAN) == "completed_verified"

    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    def test_a_dirty_verifier_run_is_never_verified_at_every_level(self, level):
        """The control. Without it the test above could pass because every
        level mints `completed_verified` unconditionally - which is the exact
        failure the gate exists to prevent."""
        assert _run_step(level, DIRTY) == "failed"

    def test_every_level_produces_the_same_status_for_the_same_evidence(self):
        clean = {
            _run_step(level, CLEAN)
            for level in ("low", "medium", "high", "xhigh", "auto")
        }
        dirty = {
            _run_step(level, DIRTY)
            for level in ("low", "medium", "high", "xhigh", "auto")
        }
        assert clean == {"completed_verified"}
        assert dirty == {"failed"}

    def test_a_run_with_no_declared_verifier_is_unverified_at_every_level(self):
        from harness.agent_loop_step import step, terminal_status_of

        statuses = set()
        for level in ("low", "medium", "high"):
            events = step(
                [{"role": "user", "content": "fix it"}],
                _ScriptedEnv(CLEAN),
                {"effort": level, "max_turns": 3},
            )
            statuses.add(terminal_status_of(events))
        assert statuses == {"completed_unverified"}

    def test_no_completion_path_reads_the_effort_setting(self):
        """A source pin, because a behavioural test only covers what it ran.

        Docstrings are stripped first: a module is allowed to NAME the setting
        in prose. What it may not do is reference it in code.
        """
        import io
        import tokenize

        for relative in (
            "harness/agent_loop_step.py",
            "harness/agent_kernel/completion.py",
        ):
            text = _repo_path(relative).read_text(encoding="utf-8")
            code_tokens = []
            readline = io.StringIO(text).readline
            for token in tokenize.generate_tokens(readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                code_tokens.append(token.string)
            code = " ".join(code_tokens)
            for banned in ("map_effort", "picker_variant", "EFFORT_", "NEO_EFFORT"):
                assert banned not in code, f"{relative} must not read {banned}"

    def test_the_mint_is_still_the_verifier_triple(self):
        from harness.agent_loop_step import _evidence_is_clean

        assert _evidence_is_clean(CLEAN) is True
        assert _evidence_is_clean(DIRTY) is False
        # An effort setting is not evidence and cannot make it clean.
        assert _evidence_is_clean({**CLEAN, "effort": "high"}) is True
        assert _evidence_is_clean({**CLEAN, "error": "boom"}) is False


# ---------------------------------------------------------------------------
# Markup safety - a render failure must NEVER delete a message
# ---------------------------------------------------------------------------


HOSTILE_ID = "[bold red]evil[/bold red]"


class TestMarkupSafety:
    def test_a_hostile_model_id_is_refused_before_it_can_reach_a_renderer(self):
        selection = M.select_model(HOSTILE_ID, [], current_model="gpt-4o")
        assert selection.ok is False
        assert selection.reason == "unsafe"
        assert selection.model == "gpt-4o"

    def test_a_hostile_model_id_that_reaches_a_line_stays_readable(self):
        """The regression the rule exists for.

        A string carrying `[...]` that crosses into a markup parser is consumed
        as a TAG, so a provider detail quoting `[/]` takes the sentence around
        it with it. The proof is RENDERED output, not a substring test: the
        hostile text must still be visible after a real rich Console has parsed
        it, and the label before it must still be there.
        """
        from rich.console import Console

        entry = M.ModelEntry(model="evil", provider=HOSTILE_ID)
        lines = [f"  {entry.id}"]

        console = Console(record=True, width=200, no_color=True, force_terminal=False)
        console.print(M.safe_lines(lines)[0])
        rendered = console.export_text()
        assert "evil" in rendered
        assert "provider" not in rendered  # nothing was eaten and nothing was lost

        console = Console(record=True, width=200, no_color=True, force_terminal=False)
        console.print(M.escape_lines(lines)[0], markup=True)
        escaped_render = console.export_text()
        assert "evil" in escaped_render
        assert HOSTILE_ID in escaped_render  # visible, not deleted

    def test_an_authored_heading_can_never_open_a_bracket(self):
        """The parser-level property: a label that opens with `[` is eaten."""
        assert M.ModelPicker(_many_catalog(4)).lines()[0] == "model picker"
        assert M.effort_variant("high", "gpt-5", "openai").lines()[0] == "effort: high"
        for receipt in (
            M.select_model("gpt 4o", [], current_model="gpt-4o"),
            M.ModelPicker(_many_catalog(2)).choose(),
        ):
            for line in receipt.lines():
                assert isinstance(line, str)
                assert not line.lstrip().startswith("["), line

    def test_a_receipt_line_may_contain_brackets_but_never_opens_the_line(self):
        """DATA may carry brackets; the LINE it sits on may not open with one.

        This is the distinction the rule turns on: a bracket in the middle of a
        line is escaped at the boundary, while a bracket at the start is eaten
        by the parser before the reader sees anything. The fixture is a catalog
        row whose provider came from a settings file, which is how a hostile
        string really reaches a row.
        """
        picker = M.ModelPicker([M.ModelEntry("evil", HOSTILE_ID, ("settings",))])
        lines = picker.lines()
        assert any("[" in line for line in lines)
        for line in lines:
            assert not line.lstrip().startswith("["), line

    def test_every_rendered_line_is_a_plain_string(self):
        picker = M.ModelPicker(_many_catalog(9))
        for line in picker.lines():
            assert isinstance(line, str)
        for line in M.escape_lines(picker.lines()):
            assert isinstance(line, str)

    def test_a_value_whose_str_raises_does_not_take_the_render_down(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no string for you")

            __repr__ = __str__

        assert M.plain_lines([Hostile()]) == [""]
        assert M.escape_lines([Hostile()]) == [""]

    def test_the_module_renders_no_markup_of_its_own(self):
        """A `[neo.*]`-style tag in this module would be a tag, not a label."""
        source = Path(M.__file__).read_text(encoding="utf-8")
        body = re.sub(r'"""(?:.|\n)*?"""', "", source)
        assert "[neo." not in body
        assert "[bold" not in body


# ---------------------------------------------------------------------------
# No key is ever stored
# ---------------------------------------------------------------------------


class TestNoKeyIsStored:
    def test_the_catalog_never_reads_or_keeps_a_credential(self):
        settings = {
            "model": "gpt-4o",
            "provider": "openai",
            "api_key": "sk-FAKE0123456789abcdefghijklmnop",
            "api_base": "https://user:password@example.invalid/v1",
            "base_url": "https://user:password@example.invalid/v1",
        }
        catalog = M.model_catalog(settings, include_registry=False)
        blob = json.dumps([entry.to_dict() for entry in catalog])
        assert "sk-FAKE" not in blob
        assert "password" not in blob
        assert "api_key" not in blob
        assert "api_base" not in blob
        for entry in catalog:
            assert set(entry.to_dict()) == {
                "id",
                "model",
                "provider",
                "sources",
                "hint",
                "family",
            }

    def test_discovery_never_carries_a_credential_either(self):
        found = M.discover_models(
            settings={
                "model": "gpt-4o",
                "provider": "openai",
                "api_key": "sk-FAKE0123456789abcdefghijklmnop",
            }
        )
        assert "sk-FAKE" not in json.dumps(found.to_dict())


# ---------------------------------------------------------------------------
# `neo models` - discovery
# ---------------------------------------------------------------------------


class TestModelsDiscovery:
    def test_it_lists_provider_slash_model(self):
        found = M.discover_models(
            settings={
                "model": "gpt-4o",
                "provider": "openai",
                "model_tiers": {
                    "easy": {"provider": "openai", "model": "gpt-4o-mini"},
                    "hard": {"provider": "anthropic", "model": "claude-sonnet-4"},
                },
            }
        )
        assert "openai/gpt-4o" in found.ids()
        assert "anthropic/claude-sonnet-4" in found.ids()
        for identifier in found.ids():
            assert "/" in identifier, f"{identifier!r} is not provider/model"

    def test_a_provider_filter_narrows_the_list(self):
        found = M.discover_models(
            "anthropic",
            settings={
                "model": "gpt-4o",
                "provider": "openai",
                "model_tiers": {
                    "hard": {"provider": "anthropic", "model": "claude-sonnet-4"}
                },
            },
        )
        assert found.ids()
        assert "anthropic/claude-sonnet-4" in found.ids()
        for identifier in found.ids():
            assert identifier.startswith("anthropic/"), identifier
        assert not any(identifier.startswith("openai/") for identifier in found.ids())

    def test_an_empty_install_lists_nothing_and_says_so(self, capsys):
        def empty(provider=None, *, refresh=False):
            return M.ModelDiscovery(entries=(), reason="offline: nothing configured")

        code = M.cmd_models(
            types.SimpleNamespace(provider=None, refresh=False, json=False),
            discovery=empty,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "no models are configured" in out

    def test_the_command_prints_one_provider_slash_model_per_line(self, capsys):
        def fixed(provider=None, *, refresh=False):
            return M.discover_models(
                settings={
                    "model": "gpt-4o",
                    "provider": "openai",
                    "model_tiers": {
                        "easy": {"provider": "openai", "model": "gpt-4o-mini"},
                        "hard": {"provider": "anthropic", "model": "claude-sonnet-4"},
                    },
                }
            )

        code = M.cmd_models(
            types.SimpleNamespace(provider=None, refresh=False, json=False),
            discovery=fixed,
        )
        out = capsys.readouterr().out.strip().splitlines()
        assert code == 0
        assert "openai/gpt-4o" in out
        assert "anthropic/claude-sonnet-4" in out
        for line in out:
            assert "/" in line, f"{line!r} is not provider/model"

    def test_a_registry_row_with_no_provider_still_prints_a_provider_segment(self):
        """`provider/model` is the contract, so an empty provider is stated.

        The built-in registry rows declare no provider. Printing a bare model
        name would break the one shape the command promises; printing a
        provider name this install does not have would be a different lie.
        """
        found = M.discover_models(settings={"provider": "openai", "model": "gpt-4o"})
        assert found.ids()
        for identifier in found.ids():
            head, _, tail = identifier.partition("/")
            assert head, identifier
            assert tail, identifier
        # The unprefixed registry rows borrowed the CONFIGURED provider.
        assert any(identifier.startswith("openai/") for identifier in found.ids())

    def test_a_discovery_failure_is_a_clean_exit_and_never_a_traceback(self, capsys):
        def boom(provider=None, *, refresh=False):
            raise RuntimeError("the settings chain is unreadable")

        code = M.cmd_models(
            types.SimpleNamespace(provider=None, refresh=False, json=False),
            discovery=boom,
        )
        assert code == 2
        assert "model discovery failed" in capsys.readouterr().out

    def test_without_refresh_it_does_not_claim_to_have_asked_a_provider(self):
        found = M.discover_models(settings={"model": "gpt-4o", "provider": "openai"})
        assert found.refreshed is False
        assert found.refresh_attempted is False
        assert found.reason.startswith("offline:")

    def test_refresh_without_a_credential_reports_that_it_did_not_reach_one(
        self, monkeypatch
    ):
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
            "TOKENROUTER_API_KEY",
            "AGENTROUTER_API_KEY",
        ):
            monkeypatch.delenv(name, raising=False)
        found = M.discover_models(
            settings={"model": "gpt-4o", "provider": "openai"}, refresh=True
        )
        assert found.refresh_attempted is True
        assert found.refreshed is False
        assert "no provider credential" in found.reason

    def test_refresh_uses_an_injected_probe_and_says_so(self, monkeypatch):
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.setenv(name, "sk-FAKE")
        found = M.discover_models(
            settings={"model": "gpt-4o", "provider": "openai"},
            refresh=True,
            live_probe=lambda provider: ["openai/gpt-4o", "openai/gpt-5"],
        )
        assert found.refreshed is True
        assert "openai/gpt-5" in found.ids()
        assert found.reason == "live provider model list requested"

    def test_a_failing_probe_is_reported_and_never_raises(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-FAKE")

        def boom(provider):
            raise RuntimeError("endpoint down")

        found = M.discover_models(
            settings={"model": "gpt-4o", "provider": "openai"},
            refresh=True,
            live_probe=boom,
        )
        assert found.refreshed is False
        assert "endpoint down" in found.reason
        assert found.ids()  # the local rows are still there

    def test_the_subcommand_parses_and_dispatches(self, capsys):
        """The whole command, driven through a REAL argparse parser.

        `cli/main.py` is another owner's file, so the mount is a filed request;
        the command itself is proven here, and the mount is one line.
        """
        import argparse

        parser = argparse.ArgumentParser(prog="neo")
        sub = parser.add_subparsers(dest="command")
        created = M.register_models_parser(sub)
        assert created is not None

        args = parser.parse_args(["models", "openai", "--refresh"])
        assert args.func is M.cmd_models
        assert args.provider == "openai"
        assert args.refresh is True
        assert args.json is False

        args = parser.parse_args(["models", "--json"])
        assert args.provider is None
        assert args.json is True

    def test_json_output_is_exactly_one_document(self, capsys):
        def fixed(provider=None, *, refresh=False):
            return M.discover_models(settings={"provider": "openai", "model": "gpt-4o"})

        code = M.cmd_models(
            types.SimpleNamespace(provider=None, refresh=False, json=True),
            discovery=fixed,
        )
        out = capsys.readouterr().out.strip()
        assert code == 0
        document = json.loads(out)
        assert "ids" in document and "models" in document
        assert document["refreshed"] is False

    def test_a_registration_on_a_broken_parser_returns_none_rather_than_raising(self):
        assert M.register_models_parser(None) is None
        assert M.register_models_parser(object()) is None


# ---------------------------------------------------------------------------
# The handoff a surface needs
# ---------------------------------------------------------------------------


class TestTheHandoffIsNamed:
    def test_the_picker_exposes_the_widget_id_and_the_keybind(self):
        assert M.picker_widget_id() == M.PICKER_WIDGET_ID == "neo-model-picker"
        assert M.VARIANT_CYCLE_KEYBIND

    def test_the_receipt_names_both_so_a_surface_needs_no_other_file(self):
        receipt = M.ModelPicker(_many_catalog(2)).receipt()
        assert receipt["widget_id"] == "neo-model-picker"
        assert receipt["keybind"] == "variant.cycle"
        assert receipt["variant"]["keybind"] == "variant.cycle"

    def test_the_module_does_not_import_the_tui_it_must_not_edit(self):
        source = Path(M.__file__).read_text(encoding="utf-8")
        for banned in (
            "from cli import tui",
            "from cli.tui",
            "import cli.tui",
            "import textual",
            "from textual",
        ):
            assert banned not in source, f"cli/models.py must not contain {banned!r}"
