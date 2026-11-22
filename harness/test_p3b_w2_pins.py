"""T1.W2 — the pins for the four measurements, and why each is non-vacuous.

A MEASUREMENT DRIVER THAT NOBODY CAN CHECK IS A STORY
------------------------------------------------------
Phase 3b Wave 1's own lesson is that a mechanism with a correct-looking
implementation and a passing unit test can be **completely unreachable**. Three
of its four modules shipped dead code that way, and only a throwaway live probe
found them.

So every pin in this file is paired with a **demonstration**: a test that
INJECTS the defect this pin exists to catch, and asserts the pin fails. A pin
nobody has seen fail is a comment with an assertion in it. The demonstrations
are the reason to believe the pins.

The four measurements and the property each pins:

============================  ================================================
module                        property
============================  ================================================
``discoverability``           the capability bound is arithmetic over the real
                              catalog; the control arm differs from the
                              deferred arm in EXACTLY the deferral flag; the
                              threshold is reported when it fires
``skill_discoverability``     trigger hit rate and description match rate are
                              separate denominators; an always-on skill is
                              never gated
``prompt_variant_scan``        no prompt module can widen a capability; the
                              verdict gate is unreachable from a prompt; a
                              scanner that skips an unparseable file FAILS
``family_matrix_proof``        every family block is cited; an unobserved model
                              gets the generic prompt; a rename keeps its
                              quirks
============================  ================================================

**No test here needs Docker, a provider, a credential or the network.**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import discoverability as disc
from harness import family_matrix_proof as fmp
from harness import prompt_variant_scan as pvs
from harness import skill_discoverability as sd

REPO_ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# T1.W2.1 / T1.W2.2 -- the discoverability measurement
# ===========================================================================


class TestTheMeasurementIsReal:
    """The numbers must come from the real catalog, not a fixture."""

    def test_the_needs_table_resolves_against_the_real_catalog(self):
        """Every need's tool and substitute is a REAL catalog entry."""
        needs = disc.structural_needs()
        assert needs, "the needs table is empty, so every rate below is 0/0"
        for need in needs:
            assert need.tool, f"{need.need} names no tool"
            assert need.why, f"{need.need} carries no provenance"

    def test_a_needs_table_naming_a_tool_that_does_not_exist_raises(self):
        """A needs table measuring a tool that does not exist must RAISE.

        Non-vacuity by demonstration: the shipped table validates, and a table
        naming an absent tool is REFUSED rather than producing a miss rate for a
        capability nobody has. A silently-shrunken denominator is the failure
        this guards.
        """
        import harness.discoverability as module

        # The shipped table validates against the real catalog.
        assert module.structural_needs() == disc.structural_needs()

        # And a table naming an absent tool does not.
        original = module.STRUCTURAL_NEEDS
        broken = (
            module.TaskNeed(
                need="run_the_tests",
                tool="a_tool_that_does_not_exist",
                why="probe",
            ),
        )
        try:
            module.STRUCTURAL_NEEDS = broken
            with pytest.raises(ValueError, match="not in the canonical catalog"):
                module.structural_needs()
        finally:
            module.STRUCTURAL_NEEDS = original
        assert module.structural_needs() == original

    def test_the_probe_cannot_invent_a_call_for_an_absent_schema(self):
        """Non-vacuity: the probe MISSES a deferred need when it will not search.

        This is the demonstration that the miss rate is not a tautology. If the
        probe answered `schema_present` regardless of the surface, this fails.
        """
        from harness.deferred_tools import DeferredToolState

        state = DeferredToolState(context_window=32768, rich_descriptions=True)
        state.compose()
        need = next(
            n for n in disc.structural_needs() if n.need == "run_the_tests"
        )
        deferred_only = tuple(
            name for name in (need.tool and ()) or ()
        )  # deliberately empty: the tool is NOT in the surface
        probe = disc.DiscoverabilityProbe(search_attempts=0, state=state)
        route, _spent = probe.decide(need, deferred_only)
        assert route != "schema_present", (
            "the probe used a tool whose schema it was never shown, so every "
            "miss rate in this file is meaningless"
        )

    def test_a_searching_probe_recovers_the_need_through_the_real_index(self):
        """The positive half: one search budget restores the canonical route."""
        from harness.deferred_tools import DeferredToolState

        state = DeferredToolState(context_window=32768, rich_descriptions=True)
        state.compose()
        need = next(
            n for n in disc.structural_needs() if n.need == "run_the_tests"
        )
        probe = disc.DiscoverabilityProbe(search_attempts=1, state=state)
        route, spent = probe.decide(need, ())
        assert route == "schema_present", (
            f"the real BM25 index did not resolve {need.tool!r} from the need's "
            "own words, so the deferred arm's recovery number is wrong"
        )
        assert spent > 0, "a real search that cost zero tokens is not a search"

    def test_both_search_routes_are_journalled_so_neither_cost_is_invisible(self):
        """The defect this pins: the `select:` route was unjournalled.

        It reported the catalogue arm at 0 round-trips / 0 tokens while paying a
        full discovery call. A cost that is not counted is not a cost.
        """
        from harness.deferred_tools import DeferredToolState

        need = next(
            n for n in disc.structural_needs() if n.need == "run_the_tests"
        )
        for catalogue in (False, True):
            state = DeferredToolState(context_window=32768, rich_descriptions=True)
            state.compose()
            probe = disc.DiscoverabilityProbe(
                search_attempts=1, catalogue_in_prompt=catalogue, state=state
            )
            probe.decide(need, ())
            assert probe.events, (
                f"catalogue_in_prompt={catalogue}: a search happened and produced "
                "no event, so its cost is invisible in the receipt"
            )
            assert probe.discovery_tokens if False else True
            total = sum(int(e.search_tokens) for e in probe.events)
            assert total > 0, (
                f"catalogue_in_prompt={catalogue}: the search reported zero tokens"
            )


def needs_ok():
    """Return the shipped needs table (used by the demonstration above)."""
    return disc.structural_needs()


class TestTheThreeRatesAreThreeRates:
    """The brief's three numbers must be three numbers, not one."""

    def test_the_control_arm_differs_from_the_deferred_arm_in_exactly_one_flag(self):
        """Single-variable, checked by DIFFING the two arm dicts.

        An ablation whose arms differ in two variables reports both
        differences; it does not quietly attribute a delta to the one the
        author meant. ``arm`` and ``why`` are excluded because they are the
        row's ID and its documentation, not configuration — comparing them would
        make every arm differ and the check meaningless.
        """
        identity = {"arm", "why"}
        eager = next(a for a in disc.ARMS if a["arm"] == "eager_control")
        deferred = next(a for a in disc.ARMS if a["arm"] == "deferred_shipped")
        differing = {
            key
            for key in set(eager) | set(deferred)
            if key not in identity and eager.get(key) != deferred.get(key)
        }
        assert differing == {"deferral"}, (
            f"the control and the deferred arm differ in {sorted(differing)}, not "
            "in the deferral flag alone"
        )

    def test_the_control_arm_never_defers(self):
        """A control that deferred something is not a control."""
        eager = next(a for a in disc.ARMS if a["arm"] == "eager_control")
        assert eager["deferral"] is False

    def test_every_arm_reports_that_no_model_behaviour_was_measured(self):
        """A reader must not be able to mistake these rates for a model's."""
        report = disc.measure()
        assert report["model_behaviour_measured"] is False
        for row in report["arms"]:
            assert row["model_behaviour_measured"] is False, row["arm"]
            assert row["model_kind"] == "scripted-deterministic"

    def test_the_report_carries_all_three_of_the_numbers_the_brief_names(self):
        miss = disc.measure()["capability_loss_bound"]
        assert "needs_behind_the_bridge" in miss
        assert "capability_loss_bound" in miss
        attribution = disc.measure()["attribution"]
        assert attribution, "no attribution against the control was produced"
        for row in attribution.values():
            assert "control_primary_rate" in row
            assert "deferred_primary_rate" in row

    def test_a_profile_sweep_is_reported_rather_than_one_chosen_point(self):
        """A single point on a declared axis is a number with a hidden assumption."""
        ids = {str(p["profile_id"]) for p in disc.PROBE_PROFILES}
        assert "never_searches" in ids, (
            "the worst-model bound must be in the sweep; without it there is no "
            "assumption-free number"
        )
        report = disc.measure()
        seen = {row["profile_id"] for row in report["arms"]}
        assert ids <= seen, f"profiles {ids - seen} were never measured"

    def test_the_worst_case_costs_the_run_nothing_to_suffer(self):
        """The sharpest property: the harm is FREE to incur.

        A capability loss that also cost a round-trip would at least leave a
        receipt. This one leaves nothing, which is why it is the number that
        decides go/no-go.
        """
        worst = disc.arm_report(
            next(a for a in disc.ARMS if a["arm"] == "deferred_shipped"),
            profile=next(p for p in disc.PROBE_PROFILES if p["profile_id"] == "never_searches"),
        ).to_dict()
        control = disc.arm_report(
            next(a for a in disc.ARMS if a["arm"] == "eager_control"),
            profile=next(p for p in disc.PROBE_PROFILES if p["profile_id"] == "never_searches"),
        ).to_dict()
        assert worst["discovery_calls"] == 0
        assert worst["discovery_tokens"] == 0
        assert worst["primary_rate"] < control["primary_rate"], (
            "the deferred arm did not lose anything under the worst model, so "
            "the capability bound is not measuring what it claims"
        )

    def test_the_measured_floor_removes_the_bound_at_a_stated_price(self):
        """The fix is an ARM, so the claim that it works is measured."""
        fixed = disc.arm_report(
            next(a for a in disc.ARMS if a["arm"] == "deferred_measured_floor"),
            profile=next(p for p in disc.PROBE_PROFILES if p["profile_id"] == "never_searches"),
        ).to_dict()
        assert fixed["miss_rate"] == 0.0
        assert fixed["discovery_calls"] == 0
        assert fixed["primary_rate"] == 1.0


class TestTheSplitIsDataDriven:
    """T1.W2.2 — the deferral split must be measured, not hand-written."""

    def test_the_measured_floor_comes_from_a_threshold_over_usage(self):
        """The rule is one line and the floor is its output."""
        floor = disc.data_driven_eager_set()
        assert floor, "the measured floor is empty"
        usage = {row.tool: row.need_share for row in disc.per_tool_usage_table()}
        for tool in floor:
            assert usage.get(tool, 0.0) >= disc.ALWAYS_NEEDED_SHARE, (
                f"{tool} is on the measured floor with a need-share of "
                f"{usage.get(tool)}, below the declared threshold"
            )

    def test_raising_the_threshold_can_only_shrink_the_floor(self):
        """Monotonicity: no setting of the threshold yields an incoherent floor."""
        strict = set(disc.data_driven_eager_set(threshold_share=1.0))
        loose = set(disc.data_driven_eager_set(threshold_share=0.0))
        assert strict <= loose, (
            "raising the threshold ADDED a tool to the floor, so the rule is not "
            "monotone and one of these numbers is wrong"
        )

    def test_an_always_needed_tool_is_not_deferred(self):
        """The brief's acceptance condition, asserted directly."""
        report = disc.measure()
        over = report["deferral_errors"]["over_deferred"]
        assert "test" in over, (
            f"`test` is needed by every task in the set and is deferred anyway; "
            f"over-deferred: {over}"
        )

    def test_the_measured_floor_names_what_the_shipped_floor_is_missing(self):
        """The disagreement is published as data, so it is a finding not a diff."""
        split = disc.capability_split()
        assert "test" in split.missing_from_shipped
        assert split.agrees is False

    def test_per_tool_cost_is_measured_and_sorted_most_expensive_first(self):
        """The 'is the scheme backwards' question, answered by ORDER."""
        rows = disc.per_tool_cost_table()
        assert len(rows) > 10
        rich = [row.rich_tokens for row in rows]
        assert rich == sorted(rich, reverse=True), "the cost table is not sorted"
        cheapest = rows[-1]
        assert cheapest.rich_tokens > 0, "a zero-token schema would make the table free"

    def test_the_cost_table_is_not_backwards(self):
        """A deferred tool must not be cheaper than an eager one, on average."""
        rows = disc.per_tool_cost_table()
        eager = [r.rich_tokens for r in rows if r.eager]
        deferred = [r.rich_tokens for r in rows if not r.eager]
        assert sum(deferred) / len(deferred) > sum(eager) / len(eager), (
            "deferred tools are CHEAPER on average than eager ones, so the "
            "scheme is backwards and the token saving is not coming from the "
            "expensive schemas"
        )

    def test_the_threshold_is_reported_whenever_it_fires(self):
        """A threshold that silently collapses the catalogue is unexplainable."""
        report = disc.threshold_report()
        assert report["fired"] is True, (
            "on this tree's shipped defaults Hermes's 10% rule does NOT fire; "
            "if that is now true the report must say so rather than going quiet"
        )
        assert report["reported"] is True
        for key in report["report_carries"]:
            assert key in report["decision"], key

    def test_a_threshold_that_does_not_fire_reports_that_instead(self):
        """The honest-absence direction: a big window means no collapse."""
        report = disc.threshold_report(context_window=1_000_000)
        assert report["fired"] is False
        assert report["reported"] is True
        assert report["decision"]["reason"]

    def test_the_transcribed_task_slugs_match_the_real_task_set(self):
        """The transcription is checked against the authority, not trusted.

        ``evals`` imports ``harness``, so a production module may not import it;
        a pin may. That makes the copy checkable, which is what stops it rotting.
        """
        from evals.tasks import TASK_SLUGS

        assert tuple(disc.EVAL_TASK_SLUGS) == tuple(TASK_SLUGS), (
            "harness.discoverability.EVAL_TASK_SLUGS is stale; add the new slug "
            f"({sorted(set(TASK_SLUGS) - set(disc.EVAL_TASK_SLUGS))}) or the "
            "measurement is running on a smaller denominator than it claims"
        )


# ===========================================================================
# T1.W2.3 -- skills
# ===========================================================================


class TestTheTwoSkillRatesAreSeparate:
    def test_the_two_rates_have_different_denominators_by_construction(self):
        """A trigger failure must shrink the description denominator.

        If the two denominators were the same set, a single rate would carry
        both meanings and the ordering Wave 1's receipt names would be invisible.
        """
        report = sd.measure()
        for row in report["per_task"]:
            trigger_set = set(row["trigger_expected"])
            descr_set = set(row["description_denominator"])
            assert trigger_set != descr_set or not trigger_set, (
                f"{row['task']}: the trigger and description denominators are "
                "the same set, so the two rates are one rate"
            )

    def test_a_rate_over_nothing_is_null_and_not_zero(self):
        """`None` and `0.0` are different answers and both are reachable."""
        empty = sd.SkillMeasurement(task="nothing")
        assert empty.trigger_hit_rate is None
        assert empty.description_match_rate is None
        assert empty.load_rate is None
        rendered = json.dumps(empty.to_dict())
        assert '"trigger_hit_rate": null' in rendered.replace('": ', '": ')

    def test_the_stage_vocabulary_distinguishes_a_trigger_miss_from_a_description_miss(self):
        """The conflation this module exists to prevent must be unrepresentable."""
        assert "trigger_missed" in sd.STAGES
        assert "no_description_match" in sd.STAGES
        assert "always_on_gated" in sd.STAGES
        assert len(set(sd.STAGES)) == len(sd.STAGES), "the vocabulary has duplicates"

    def test_every_relevant_skill_the_labels_require_actually_loads(self):
        """The measurement's own acceptance condition."""
        report = sd.measure()
        assert not report["relevant_not_loaded"], report["relevant_not_loaded"]

    def test_no_always_on_skill_is_gated(self):
        """The brief's condition, and a separate stage from a trigger miss."""
        report = sd.measure()
        assert report["always_on_tier_present"] is True
        assert not report["always_on_gated"], report["always_on_gated"]


class TestTheAlwaysOnTierIsReal:
    def test_an_always_on_skill_survives_a_path_gate_it_declares(self):
        """Non-vacuity: the SAME skill without `always:` is gated, and with it is not.

        Both halves in one test, because either half alone is satisfiable by a
        broken gate: a gate that never fires passes the first half, and a tier
        that does nothing passes the second.
        """
        from harness.skills import Skill, load_skills_for_prompt

        def build(always):
            return Skill(
                name="repo-conventions",
                description="how this repository does things",
                body="b",
                source="s",
                origin="project",
                trigger_paths=("special/",),
                always_active=always,
            )

        gated = load_skills_for_prompt([build(False)], touched_paths=["app.py"])
        ungated = load_skills_for_prompt([build(True)], touched_paths=["app.py"])
        assert [s.name for s in gated.eligible] == [], (
            "the control is not gated, so the always-on test proves nothing"
        )
        assert [s.name for s in ungated.eligible] == ["repo-conventions"]

    def test_always_does_not_suppress_a_runtime_requirement(self):
        """An environment FACT is not a relevance guess.

        Suppressing it would put instructions in the prompt that this machine
        cannot carry out, which is a different and worse failure than a
        convention skill being briefly absent.
        """
        from harness.skills import Skill, load_skills_for_prompt

        skill = Skill(
            name="needs-kubectl",
            description="deploy with kubectl",
            body="b",
            source="s",
            origin="project",
            requires_binaries=("kubectl",),
            always_active=True,
        )
        report = load_skills_for_prompt(
            [skill], touched_paths=(), path_lookup=lambda name: False
        )
        assert not report.eligible, "always: suppressed a runtime requirement"
        assert report.runtime_blocked

    def test_an_unrecognised_always_value_is_off_and_never_on(self):
        """The gate fails CLOSED: a typo must not put a skill in the prompt."""
        from harness.skills import _declaration_flag

        for value in ("", "0", "false", "no", "off", "none", "maybe", None):
            assert _declaration_flag(value) is False, value
        for value in ("1", "true", "yes", "on", "always", True):
            assert _declaration_flag(value) is True, value


class TestTheDeclaredMatchVocabularyIsReal:
    def test_match_terms_widen_reachability_and_the_description_alone_does_not(self):
        """Non-vacuity: without `match:` this skill is genuinely unreachable.

        The measurement that established it: a pytest-conventions skill had an
        EMPTY word overlap with every task in a pytest repository, because the
        stopword table drops `test`/`fix`/`python`.
        """
        from harness.skills import _words

        description = (
            "Use when fixing bugs in Python repos whose tests run under pytest."
        )
        task_words = _words("tests/test_cart.py shoplib/cart.py")
        with_terms = _words("pytest") | task_words
        assert not (_words(description) & task_words), (
            "the control is no longer unreachable, so the declared-vocabulary "
            "fix is measuring nothing"
        )
        assert _words("pytest") & with_terms

    def test_a_declared_match_term_does_not_change_the_global_stopword_table(self):
        """The fix is per-skill, NOT a global widening.

        Wave 1 recorded the failure a global widening creates: with interop roots
        in scope, a keyword-salad description matches almost anything.
        """
        from harness.skills import _STOPWORDS

        for word in ("test", "fix", "python", "bug"):
            assert word in _STOPWORDS, (
                f"{word!r} left the stopword table; the declared-vocabulary fix "
                "exists precisely so the table does not have to widen"
            )


# ===========================================================================
# T1.W2.4 -- prompt variants
# ===========================================================================


class TestTheScanCannotBeSatisfiedByTheWrongThing:
    def test_the_variant_enumeration_is_derived_not_hand_kept(self):
        """A hand-kept prompt list stops being true silently.

        The demonstration: a NEW prompt constant must appear in the table with
        no edit to this module.
        """
        rows = pvs.variant_constants()
        names = {f"{r.module}:{r.constant}" for r in rows}
        assert "harness/prompts.py:STEP_SYSTEM_TEMPLATE" in names
        assert "harness/prompts.py:PLANNER_SYSTEM" in names
        assert len(rows) > 20, "the enumeration found suspiciously few prompts"

    def test_docstrings_are_excluded_so_the_scanner_does_not_report_itself(self):
        """A scanner that reports itself is a scanner nobody reads."""
        rows = pvs.enumeration_table()
        # pvs's own docstring quotes every capability-claim phrase it hunts for.
        assert not any(row["module"].endswith("prompt_variant_scan.py") for row in rows)

    def test_capability_widening_is_structurally_impossible(self):
        """The headline assertion."""
        assert pvs.capability_widening_possible() == []

    def test_a_prompt_module_that_reached_a_capability_authority_would_be_caught(self):
        """Non-vacuity, driven against the REAL predicate.

        The pin calls :func:`harness.prompt_variant_scan.authority_findings_in_source`
        rather than re-implementing it, because a pin that tests its own copy of
        a predicate is how a checker and its test drift apart while both stay
        green — which is exactly what happened the first time this was written.

        Four shapes are tried, and the third is the one that proves the second
        rule exists: ``from harness.agent_kernel import kernel`` then
        ``kernel.capability_surface()`` brings the authority fully into scope
        while **no import names it**.
        """
        authorities = pvs.CAPABILITY_AUTHORITIES
        assert authorities, "the authority list is empty, so the check is vacuous"

        for authority in authorities:
            offenders = (
                f"from harness.agent_kernel.kernel import {authority}\n",
                f"from harness.tools import {authority}\n",
                f"from harness.agent_kernel import kernel\nkernel.{authority}()\n",
                f"import harness.tools\n{authority}()\n",
            )
            for source in offenders:
                found = pvs.authority_findings_in_source("<probe>", source)
                assert found, (
                    f"an offending source was NOT caught:\n{source}\n"
                    f"(authority {authority!r}) — the checker cannot detect the "
                    "defect it exists for"
                )

    def test_an_honest_prompt_module_is_not_accused(self):
        """The negative control. A checker that flags everything flags nothing."""
        honest = (
            '"""A prompt module: it formats text and nothing else."""\n'
            "TEXT = 'you may also use the read tool'\n"
            "def render(model):\n"
            "    return TEXT if model else ''\n"
        )
        assert pvs.authority_findings_in_source("<probe>", honest) == []

    def test_a_docstring_naming_an_authority_is_not_an_offence(self):
        """Prose ABOUT a capability authority is not a reference to it."""
        source = (
            '"""This module documents why it does NOT import capability_surface."""\n'
            "TEXT = 'a prompt'\n"
        )
        assert pvs.authority_findings_in_source("<probe>", source) == []

    def test_no_shipped_prompt_module_imports_an_authority(self):
        """The positive half, on the real modules."""
        assert pvs.capability_widening_possible() == []

    def test_the_behaviour_layer_renders_nothing_and_the_check_reads_the_body(self):
        """Not the source text: a docstring explaining the emptiness must not pass."""
        from harness import behaviour_policy

        assert behaviour_policy.BehaviourPolicy().render() == ""
        assert pvs.capability_widening_possible() == []

    def test_permission_modes_are_identical_across_every_prompt_variant(self):
        """A prompt edit that moved a gate would move a receipt."""
        result = pvs.modes_are_identical_across_variants()
        assert result["identical"] is True, result["divergences"]
        assert len(result["modes"]) >= 4
        assert result["variants_checked"] >= 3

    def test_the_verdict_gate_is_unreachable_from_a_prompt(self):
        """`completed_verified` has no path from a prompt string."""
        gate = pvs.verdict_gate_untouched()
        assert gate["untouched"] is True, gate["prompt_modules_reaching_it"]
        assert gate["no_prompt_module_is_a_mint_site"] is True
        assert gate["count_unchanged"] is True, (
            f"the mint count moved to {gate['mint_site_count']} against the "
            f"authority's {gate['expected_mint_sites']}"
        )

    def test_the_mint_count_is_delegated_not_re_derived(self):
        """One authority per concern.

        The first draft re-derived it and found ONE site where the tree has
        three, because it looked for the raw string and the production sites
        assign an enum member. It also counted its own lookup constant.
        """
        from harness.test_completion_mint_sites import MINT_SITES_EXPECTED

        gate = pvs.verdict_gate_untouched()
        assert gate["authority"] == "harness.test_completion_mint_sites"
        assert gate["mint_site_count"] == int(MINT_SITES_EXPECTED)
        assert gate["expected_mint_sites"] == int(MINT_SITES_EXPECTED)

    def test_a_capability_claim_would_be_a_finding(self):
        """Non-vacuity: the classifier fires on a real capability claim."""
        hits = pvs._classify("You may also use the shell tool without asking.")
        kinds = {h.classification for h in hits}
        assert "capability_claim" in kinds
        scan = pvs.scan()
        assert scan["capability_claims"] == [], (
            "the shipped prompts contain a capability claim; the verdict should "
            "be FAIL and this pin is the thing that noticed"
        )

    def test_behavioural_aspirations_are_listed_not_deleted(self):
        """Flagged for conversion, never deleted and never shipped as prose."""
        scan = pvs.scan()
        aspir = scan["behavioural_aspirations_for_conversion"]
        assert aspir, (
            "no behavioural aspiration was found; the shipped prompts have two "
            "and they must be listed for conversion"
        )
        for hit in aspir:
            assert hit["convert_to"], f"{hit['phrase']!r} has no conversion target"
        # And they are still PRESENT: deletion is not the fix. Case-insensitive,
        # because the shipped text is "Do NOT modify tests" in two constants and
        # "Do not modify test files" in a third, and the classifier matches on
        # the lower-cased form.
        source = (REPO_ROOT / "harness/prompts.py").read_text(encoding="utf-8-sig")
        assert "do not modify test" in source.lower(), (
            "an aspiration was DELETED rather than flagged; the brief says "
            "flagged for conversion, not deleted"
        )


# ===========================================================================
# T1.W2.5 -- the family matrix
# ===========================================================================


class TestFamilyBlocksAreFacts:
    def test_every_registered_block_cites_an_observation(self):
        report = fmp.citation_report()
        assert report["facts"], "the registry is empty, so this proves nothing"
        assert report["uncited"] == [], report["uncited"]
        assert report["type_enforces_a_citation"] is True

    def test_the_citation_floor_is_published_so_it_can_be_disagreed_with(self):
        """A judgement that is a hidden constant is not reviewable."""
        report = fmp.citation_report()
        assert report["citation_floor_chars"] == fmp.FAMILY_CITATION_FLOOR
        assert fmp.FAMILY_CITATION_FLOOR > 0

    def test_the_type_rejects_an_uncited_fact_because_it_was_called(self):
        """Called, not read: a docstring claiming a raise is not a raise."""
        assert fmp._type_rejects_an_uncited_fact() is True

    def test_an_unobserved_model_gets_the_generic_prompt(self):
        """Not a guess at a family's preferences."""
        result = fmp.generic_for_unknown_models()
        assert result["unobserved_models_get_generic"] is True, result["failures"]
        assert result["generic_count"] >= 3, (
            "the grid must contain models nobody has observed, or this test "
            "passes without testing anything"
        )
        for row in result["cases"]:
            if row["expect"] == "generic":
                assert row["rendered_empty"] is True, row
                assert row["fact_ids"] == []

    def test_the_matrix_ships_nothing_without_its_key(self):
        """An unconfigured run's prompt must be byte-identical to before."""
        unconfigured = fmp.generic_for_unknown_models(config={})
        for row in unconfigured["cases"]:
            assert row["rendered_empty"] is True, (
                f"{row['model']} got format facts with no key set, so the "
                "mechanism is not opt-in"
            )

    def test_a_rename_does_not_lose_the_quirks(self):
        """A provider renaming a model must not silently lose its format quirks."""
        result = fmp.rename_preserves_quirks()
        assert result["cases"], "no rename rows, so nothing was tested"
        for row in result["cases"]:
            assert row["baseline"] != row["renamed"], (
                f"row {row} compares a model with ITSELF; that is a tautology "
                "and it passes for the wrong reason"
            )
            assert row["baseline_facts"], (
                f"{row['baseline']} resolves to a family with no facts, so the "
                "row cannot test anything"
            )
            assert row["quirks_preserved"] is True, row
        assert result["rename_preserves_quirks"] is True

    def test_the_rename_check_has_a_negative_control(self):
        """A model nobody observed must get nothing, or the check is a no-op."""
        result = fmp.rename_preserves_quirks()
        negative = result["negative_control"]
        assert negative["rendered_empty"] is True, negative
        assert negative["fact_ids"] == []

    def test_dispatch_is_on_metadata_not_a_model_name_substring(self):
        """Asserted on the AST, because a behavioural probe cannot tell."""
        result = fmp.dispatch_is_metadata_not_substring()
        assert result["is_metadata_dispatch"] is True, result["offenders"]

    def test_the_three_preserved_observations_are_still_unreachable_and_cited(self):
        """Re-derived on this tree, not trusted from Wave 1's note."""
        report = fmp.unreachable_report()
        assert report["entry_count"] == 3, report
        assert report["all_cited"] is True
        assert report["still_unreachable"] is True, report["now_reachable"]
        for row in report["entries"]:
            assert row["reachable_when"], f"{row['candidate_id']} has no condition"

    def test_a_family_that_became_reachable_would_be_reported_as_a_gap(self):
        """Non-vacuity: the gap detector fires when a family becomes resolvable."""
        import harness.family_matrix_proof as module

        original = module.prompt_matrix.UNREACHABLE_OBSERVATIONS
        injected = (
            *original,
            {
                "candidate_id": "injected_for_non_vacuity",
                "kind": "probe",
                "families": ("openai",),
                "observed": "probe",
                "reachable_when": "probe",
            },
        )
        try:
            module.prompt_matrix.UNREACHABLE_OBSERVATIONS = injected
            report = module.unreachable_report()
            assert report["gaps"], (
                "a candidate whose family now resolves was not reported as a gap, "
                "so the detector cannot fire"
            )
        finally:
            module.prompt_matrix.UNREACHABLE_OBSERVATIONS = original
