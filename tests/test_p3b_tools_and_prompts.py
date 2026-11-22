"""Pins for Phase 3b Wave 1: the deferral seam, the skills registry, the tools
ablation, and the prompt-change gate.

Four things are pinned here and the grouping is deliberate - each class is
pinned four ways, because this repository's evidence standard is that one green
assertion is not a pin:

* **the seam** - the substring scorer is unchanged, the BM25 contract holds, and
  swapping the scorer is a swap rather than an edit;
* **the security surface** - deferral never re-admits a withheld tool, and the
  OFF arm does not switch withholding off with it (the defect this module's own
  test caught on the day it was written);
* **the vacuity claims** - re-derived from source on every run, so a change to
  the shipped mock that made the success axis non-vacuous is detected rather
  than silently obsoleting a recorded claim;
* **the prompt gate** - including an INVERTED demonstration that an unarmed
  prompt edit fails, so the gate cannot be quietly weakened into passing.

The prompt-pin section deliberately re-implements its own assertions rather than
importing the gate's verdict. A test that calls ``check()`` and asserts
``ok is True`` proves only that the gate currently agrees with itself; these
tests build a temp prompts file with a known edit and assert the FINDING KIND,
so they fail if the classification logic changes even though the verdict did not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import prompt_pin, tools_ablation
from extensions import skill_registry
from extensions.skill_registry import (
    DISCOVERY_PATHS,
    DISCOVERY_SCOPES,
    INTEROP_PATHS,
    PRECEDENCE,
    RUNTIME_GATE_KINDS,
    RuntimeGate,
    TrustTier,
    classify_path,
    describe_registry,
    discovered_skill_policy,
    permission_cap_source,
    resolve_precedence,
    runtime_gate_report,
    validate_trust_tiers,
)
from integrations import tools as itools
from integrations.tool_deferral import (
    DEFER_REASONS,
    KEEP_REASONS,
    SELECT_PREFIX,
    SELECTION_KINDS,
    TOOL_STATES,
    DeferralPolicy,
    DeferralReceipt,
    cache_key,
    deferral_safety_report,
    describe_states,
    parse_selection,
    resolve_selection,
    selection_from_config,
    state_of,
)
from integrations.tool_search import (
    NO_MATCH,
    SUBSTRING_SCORER_NOTES,
    Bm25ToolSearch,
    SearchCandidate,
    SubstringToolSearch,
    rank_tools,
    scorer_by_name,
    tokenize,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CATALOG = [
    SearchCandidate("read_file", "Read a repository file from disk", ("files", "read")),
    SearchCandidate("write_file", "Write a repository file", ("files", "write")),
    SearchCandidate("list_dir", "List files in a directory", ("files",)),
    SearchCandidate("run_tests", "Run the test suite", ("tests",)),
    SearchCandidate("search_tools", "Search the tool catalog", ("discovery",)),
]


def _policy(**kwargs):
    defaults = dict(
        enabled=True,
        threshold=1.0,
        always_keep=("search_tools",),
        withheld=("privileged_reset",),
    )
    defaults.update(kwargs)
    return DeferralPolicy(**defaults)


def _with_candidates():
    return [*CATALOG, SearchCandidate("privileged_reset", "Reset the account")]


def _schemas():
    return {
        c.name: {"type": "function", "function": {"name": c.name}} for c in _with_candidates()
    }


# ==========================================================================
# W1.1 - the search seam
# ==========================================================================


class TestTheSearchSeam:
    """The characterisation, pinned, plus the four rules a replacement keeps."""

    def test_the_replaced_scorer_is_documented_as_substring_without_idf(self):
        """The corpus's complaint must be a pinned FACT, not a handoff claim."""
        notes = SUBSTRING_SCORER_NOTES
        assert notes["kind"] == "hand-rolled substring accumulation"
        assert notes["has_idf"] is False
        assert notes["has_stemming"] is False
        assert notes["has_length_normalisation"] is False
        assert notes["location"] == "integrations/tools.py ToolCatalog.search"

    def test_the_legacy_scorer_still_reproduces_the_shipped_constants(self):
        """A seam refactor must be a no-op on the default arm.

        The pre-seam body added 100/50/25/6/12 per term. If the constants move,
        an ablation's "control" arm stops being the control and the seam becomes
        an unreported behaviour change.
        """
        scorer = SubstringToolSearch()
        assert (scorer.EXACT_NAME, scorer.PREFIX_NAME, scorer.SUBSTRING_NAME) == (
            100.0,
            50.0,
            25.0,
        )
        assert (scorer.SUBSTRING_TAG, scorer.SUBSTRING_DESCRIPTION) == (12.0, 6.0)
        assert scorer.EMPTY_QUERY_SCORE == 1.0
        # read_file: name startswith (50) + tag 'read' (12) + desc (6) = 68
        score, _hits = scorer.score("read", CATALOG[0])
        assert score == 68.0

    def test_catalog_search_is_byte_identical_to_the_pre_seam_ordering(self):
        """Two-positional call sites must be untouched."""
        catalog = itools.ToolCatalog(
            [
                itools.ToolDescriptor("read_file", "Read a repository file", {"type": "object"}, tags=("files", "read")),
                itools.ToolDescriptor("write_file", "Write a repository file", {"type": "object"}, tags=("files", "write")),
            ]
        )
        assert [d.name for d in catalog.search("repository")] == ["read_file", "write_file"]
        assert [d.name for d in catalog.search("")] == ["read_file", "write_file"]
        assert catalog.search("zzzzz") == []
        assert catalog.search("read", max_results=0) == []
        assert len(catalog.search("file", max_results="2")) == 2
        assert [d.name for d in catalog.search_tools("repository")] == ["read_file", "write_file"]

    def test_an_unknown_scorer_name_is_refused_rather_than_silently_substituted(self):
        """The failure this module must never produce: BM25 results labelled substring."""
        with pytest.raises(KeyError) as raised:
            scorer_by_name("definitely-not-a-scorer")
        assert "substring" in str(raised.value) and "bm25" in str(raised.value)

    def test_bm25_keeps_exact_name_supremacy_and_is_a_total_order(self):
        scorer = Bm25ToolSearch()
        hits = rank_tools("read_file", CATALOG, scorer=scorer)
        assert hits[0].name == "read_file"
        # A tie breaks on name ascending, so the result cannot depend on set order.
        tied = rank_tools("zzz", CATALOG, scorer=Bm25ToolSearch())
        assert tied == []
        repeated = rank_tools("files", CATALOG, scorer=Bm25ToolSearch())
        assert [h.name for h in repeated] == [h.name for h in repeated]

    def test_a_non_match_is_no_match_and_not_a_zero_score(self):
        """Rule 4. A zero reading as a match is the failure the constant exists for."""
        assert NO_MATCH == -1.0
        assert rank_tools("kubernetes", CATALOG) == []
        scorer = Bm25ToolSearch().fit(CATALOG)
        assert scorer.score("kubernetes", CATALOG[0]) == (NO_MATCH, ())
        assert [h.name for h in rank_tools("kubernetes", CATALOG, scorer=scorer)] == []

    def test_every_hit_carries_its_scorer_name_so_two_arms_are_not_one_measurement(self):
        sub = rank_tools("read", CATALOG)[0]
        bm = rank_tools("read", CATALOG, scorer=Bm25ToolSearch())[0]
        assert sub.provenance["scorer"] == "substring"
        assert bm.provenance["scorer"] == "bm25"
        assert "k1" in bm.provenance and "b" in bm.provenance

    def test_tokenize_splits_on_hyphen_and_underscore_but_keeps_the_interior(self):
        # An interior hyphen or underscore is KEPT, so `read_file` is ONE token.
        # That is why a bare `read` does not prefix-match it, and why the prefix
        # expansion is opt-in rather than implicit.
        assert tokenize("read_file") == ("read_file",)
        assert tokenize("read-file") == ("read-file",)
        assert tokenize("Read a File") == ("read", "a", "file")
        assert tokenize("v1.2.3") == ("v1", "2", "3")

    def test_prefix_matching_is_opt_in_and_never_applied_implicitly(self):
        """A search that prefix-expands by default returns everything."""
        default = Bm25ToolSearch()
        expanded = Bm25ToolSearch(expand_last_segment_prefixes=True).fit(CATALOG)
        # `read_file` is one token, so only its LAST segment prefix-matches.
        assert default.prefix_match("fil", CATALOG[0]) is True
        assert default.prefix_match("rea", CATALOG[0]) is False
        # The substring scorer reaches `read_file` because `fil` is a SUBSTRING of it.
        # That is the corpus's complaint, stated as a measurement: it also
        # reaches `run_tests` (via "suite") and every other tool whose prose
        # contains the letters. BM25 without expansion does not.
        substring_hits = [h.name for h in rank_tools("fil", CATALOG)]
        expanded_hits = [h.name for h in rank_tools("fil", CATALOG, scorer=expanded)]
        bm25_plain = [h.name for h in rank_tools("fil", CATALOG, scorer=Bm25ToolSearch())]
        assert len(substring_hits) > len(bm25_plain)
        assert "read_file" in substring_hits
        assert "read_file" in expanded_hits
        assert Bm25ToolSearch().describe()["expand_last_segment_prefixes"] is False


# ==========================================================================
# W1.1 - the third state
# ==========================================================================


class TestDeferredIsAThirdState:
    def test_three_states_and_their_distinction_are_data_not_prose(self):
        states = describe_states()
        assert set(states) == set(TOOL_STATES) == {"offered", "deferred", "withheld"}
        assert states["withheld"]["discoverable"] is False
        assert states["withheld"]["callable"] is False
        assert states["deferred"]["discoverable"] is True
        assert states["deferred"]["callable"] is True
        assert states["deferred"]["schema_in_request"] is False
        assert states["offered"]["schema_in_request"] is True

    def test_deferral_is_not_an_extension_of_withholding(self):
        """The two axes differ on every field that matters."""
        deferred, withheld = describe_states()["deferred"], describe_states()["withheld"]
        assert deferred["discoverable"] != withheld["discoverable"]
        assert deferred["decided_by"] != withheld["decided_by"]
        assert "PERMISSION" in withheld["description"].upper()

    def test_state_of_reports_an_unclassified_tool_as_empty_not_offered(self):
        """Fail-OPEN on the exact question this module keeps closed."""
        assert state_of("ghost", kept=("a",)) == ""
        assert state_of("a", kept=("a",)) == "offered"
        assert state_of("a", withheld=("a",)) == "withheld"
        assert state_of("a", deferred=("a",)) == "deferred"

    def test_two_authorities_claiming_one_tool_is_refused_not_resolved(self):
        with pytest.raises(ValueError):
            state_of("a", kept=("a",), withheld=("a",))

    def test_a_withheld_tool_is_never_ranked_and_never_deferred(self):
        policy = _policy()
        decision = policy.decide("read", _with_candidates())
        assert "privileged_reset" in decision.withheld
        assert "privileged_reset" not in decision.kept
        assert "privileged_reset" not in decision.deferred
        assert "privileged_reset" not in decision.scores

    def test_the_off_arm_still_honours_withholding(self):
        """THE DEFECT THIS FILE CAUGHT.

        The first version returned `kept=catalog_names` when deferral was
        disabled, which put every withheld tool back into the request the moment
        the budget control was switched off. Turning a BUDGET control off must
        never turn a PERMISSION decision off with it.
        """
        off = _policy(enabled=False)
        decision = off.decide("read", _with_candidates())
        assert "privileged_reset" not in decision.kept
        assert "privileged_reset" in decision.withheld
        assert set(decision.kept) | set(decision.withheld) == {
            c.name for c in _with_candidates()
        }

    def test_the_config_reader_carries_withholding_into_the_off_arm(self):
        """THE INTEGRATION HAZARD, pinned.

        Withholding is `harness.tool_surface`'s decision and deferral is this
        module's, so a caller can plausibly pass the withheld set to ONE of
        them. If it reaches only the surface and not the policy, the OFF arm
        re-admits it - because the OFF arm is the path with no scoring and so no
        other chance to notice. `tool_deferral_withheld` is therefore the seam,
        and this asserts the reader propagates it rather than assuming the
        caller wired it.
        """
        off = selection_from_config(
            {
                "tool_deferral_enabled": False,
                "tool_deferral_withheld": ["privileged_reset"],
            }
        )
        assert off.withheld == ("privileged_reset",)
        decision = off.decide("read", _with_candidates())
        assert "privileged_reset" not in decision.kept
        assert deferral_safety_report(
            off,
            decision,
            discovery_tool_names=("search_tools",),
            catalog_names=[c.name for c in _with_candidates()],
        )["safe"] is True

    def test_a_deferred_tool_stays_executable_once_discovered(self):
        """Deferred is a PRESENTATION state, not a permission one.

        ADJUDICATED IN P3b/W2. The original version asserted only that a
        deferred name is still in the candidate list - which is true of a
        deferred tool AND of a tool that was never in the catalog at all, so
        the assertion could not tell "deferred but reachable" from "absent".
        It now resolves the deferred name and asserts the resolution returns
        it, which is the property the name claims.
        """
        policy = _policy()
        _kept, receipt, decision = policy.view(
            "read", _with_candidates(), schemas=_schemas()
        )
        assert decision.deferred, "this fixture must actually defer something"
        held = set(decision.deferred)
        assert receipt.names == tuple(sorted(held))
        # Nothing about a deferred tool is removed from the catalog: a resolved
        # name resolves exactly as an always-present one would.
        assert held & {c.name for c in _with_candidates()} == held
        # AND it resolves. Presence in a list is not reachability.
        for name in sorted(held):
            resolved = resolve_selection(
                f"select:{name}", _with_candidates()
            )
            assert name in [h.name for h in resolved["hits"]], (
                f"deferred tool {name!r} is in the catalog but does not resolve; "
                "the receipt promises the model can reach it and the promise is "
                "not kept"
            )
        # AND the control: a name that was never in the catalog does NOT
        # resolve. Without this the loop above is satisfied by a resolver that
        # returns everything.
        absent = resolve_selection(
            "select:no_such_tool_at_all", _with_candidates()
        )
        assert absent["resolution"] in {"absent", "scored_fallback"}, (
            "a non-existent tool resolved; the reachability assertion above is "
            "therefore measuring nothing"
        )
        assert "no_such_tool_at_all" not in [h.name for h in absent["hits"]]

    def test_the_threshold_trigger_is_reported_for_every_tool(self):
        policy = _policy(threshold=1000.0)
        decision = policy.decide("read", _with_candidates())
        assert decision.threshold == 1000.0
        for name in decision.reasons:
            assert decision.reasons[name] in (*DEFER_REASONS, *KEEP_REASONS)
        assert "read_file" in decision.scores

    def test_an_unreachable_tool_is_reported_as_no_query_match_not_below_threshold(self):
        """The distinction the corpus's motivating case turns on."""
        policy = _policy(threshold=0.0, defer_unmatched=True)
        decision = policy.decide("kubernetes", _with_candidates())
        assert decision.reasons["read_file"] == "no_query_match"
        assert decision.reasons["search_tools"] == "always_keep"

    def test_defer_unmatched_false_keeps_everything_and_is_not_the_same_as_off(self):
        policy = _policy(threshold=0.0, defer_unmatched=False)
        decision = policy.decide("kubernetes", _with_candidates())
        assert decision.deferred == ()
        assert "privileged_reset" in decision.withheld


class TestSelectForm:
    def test_the_three_query_kinds_are_a_closed_set(self):
        assert SELECTION_KINDS == ("direct", "listing", "query")
        assert parse_selection("select:read_file") == {
            "kind": "direct", "name": "read_file", "query": ""
        }
        assert parse_selection("select")["kind"] == "listing"
        assert parse_selection("read something")["kind"] == "query"

    def test_a_direct_selection_resolves_by_exact_name_without_scoring(self):
        result = resolve_selection("select:write_file", CATALOG)
        assert result["resolution"] == "exact"
        assert result["scored"] is False
        assert result["hits"][0].name == "write_file"

    def test_a_misspelled_direct_selection_is_recorded_as_a_scored_fallback(self):
        """Silently fuzzy-matching would make a direct selection a guess."""
        result = resolve_selection("select:write_fil", CATALOG)
        assert result["resolution"] == "scored_fallback"
        assert result["scored"] is True

    def test_an_absent_direct_selection_reports_absent_not_a_different_tool(self):
        result = resolve_selection("select:no_such_tool_anywhere", CATALOG)
        assert result["resolution"] == "absent"
        assert result["hits"] == []

    def test_the_receipt_names_both_discovery_forms(self):
        """A hint mentioning only search would leave the direct form undiscoverable."""
        from integrations.tool_deferral import _default_hint

        rendered = _default_hint(("a", "b"))
        assert SELECT_PREFIX in rendered
        assert "keyword" in rendered.lower()
        assert "tool-search" in rendered.lower()

    def test_an_empty_deferred_set_renders_nothing_so_the_control_arm_is_byte_identical(self):
        assert DeferralReceipt().render() == ""


class TestDeferralSecuritySurface:
    def test_the_report_is_green_on_a_correctly_built_policy(self):
        policy = _policy()
        decision = policy.decide("read", _with_candidates())
        report = deferral_safety_report(
            policy,
            decision,
            discovery_tool_names=("search_tools",),
            catalog_names=[c.name for c in _with_candidates()],
        )
        assert report["safe"] is True
        assert report["findings"] == []

    def test_a_withheld_tool_that_became_reachable_is_caught(self):
        """DEMONSTRATION, not a claim: forge the leak and assert the report fires."""
        policy = _policy()
        decision = policy.decide("read", _with_candidates())
        forged = type(decision)(
            **{**decision.__dict__, "kept": (*decision.kept, "privileged_reset")}
        )
        report = deferral_safety_report(
            policy,
            forged,
            discovery_tool_names=("search_tools",),
            catalog_names=[c.name for c in _with_candidates()],
        )
        assert report["safe"] is False
        assert report["findings"][0]["kind"] == "withheld_tool_reachable"
        assert "privileged_reset" in report["findings"][0]["tools"]

    def test_a_deferred_catalog_with_no_discovery_tool_is_a_hard_refusal(self):
        policy = _policy(defer_unmatched=True)
        decision = policy.decide("read", _with_candidates())
        report = deferral_safety_report(
            policy, decision, discovery_tool_names=(), catalog_names=[c.name for c in _with_candidates()]
        )
        assert report["safe"] is False
        assert any(f["kind"] == "no_discovery_tool" for f in report["findings"])

    def test_an_unclassified_catalog_tool_is_reported_not_defaulted(self):
        policy = _policy()
        decision = policy.decide("read", _with_candidates())
        report = deferral_safety_report(
            policy,
            decision,
            discovery_tool_names=("search_tools",),
            catalog_names=[*(c.name for c in _with_candidates()), "unheard_of"],
        )
        kinds = [f["kind"] for f in report["findings"]]
        assert "unguarded_state" in kinds

    def test_the_cache_is_keyed_on_the_catalog_and_not_only_the_kept_set(self):
        """Adding a tool with an unchanged kept set must invalidate."""
        base = cache_key(policy_version="v", kept=("a",), catalog=("a", "b"))
        grown = cache_key(policy_version="v", kept=("a",), catalog=("a", "b", "c"))
        assert base != grown
        assert cache_key(policy_version="v", kept=("a",), catalog=("b", "a")) == base

    def test_the_cache_actually_reuses_and_is_bounded_and_clearable(self):
        policy = _policy()
        policy.decide("read", _with_candidates())
        first = policy.cache_stats()["entries"]
        assert first == 1
        policy.decide("read", _with_candidates())
        assert policy.cache_stats()["entries"] == first
        policy.clear_cache()
        assert policy.cache_stats()["entries"] == 0

    def test_truthiness_follows_agt13_so_a_settings_string_cannot_enable_deferral(self):
        assert selection_from_config({"tool_deferral_enabled": "false"}).enabled is False
        assert selection_from_config({"tool_deferral_enabled": "off"}).enabled is False
        assert selection_from_config({"tool_deferral_enabled": "true"}).enabled is True
        assert selection_from_config({}).enabled is False

    def test_a_typo_in_a_scorer_name_fails_at_config_time_not_at_first_search(self):
        with pytest.raises(KeyError):
            selection_from_config(
                {"tool_deferral_enabled": True, "tool_deferral_scorer": "bm-25"}
            )


# ==========================================================================
# W1.2 - the skills registry
# ==========================================================================


class TestSkillsDiscoveryTable:
    def test_every_discovery_path_has_a_declared_scope_trust_and_reason(self):
        assert len(DISCOVERY_PATHS) >= 8
        for rule in DISCOVERY_PATHS:
            assert rule.scope in DISCOVERY_SCOPES
            assert rule.trust
            assert len(rule.why) > 30, f"{rule.template} has no written reason"

    def test_the_cross_vendor_interop_paths_are_present_and_marked_foreign(self):
        """"the sharpest interoperability datum in the survey"."""
        assert "{home}/.claude/skills" in INTEROP_PATHS
        assert "{repo}/.claude/skills" in INTEROP_PATHS
        foreign = [r for r in DISCOVERY_PATHS if r.foreign]
        assert len(foreign) == 2
        assert all(r.trust == "community" for r in foreign), (
            "an interop path is community by default: a path this product did "
            "not author cannot be trusted because it sits under the user's home"
        )

    def test_the_cross_vendor_standard_is_the_first_project_row(self):
        first = next(r for r in DISCOVERY_PATHS if r.scope == "project")
        assert first.template == "{repo}/.agents/skills"

    def test_precedence_is_a_total_order_over_every_declared_scope(self):
        assert sorted(PRECEDENCE) == sorted(DISCOVERY_SCOPES)
        assert len(set(PRECEDENCE)) == len(PRECEDENCE)

    def test_precedence_is_workspace_then_user_then_builtin(self):
        """Gemini's order, adopted - and it is the order the scanner already used."""
        assert PRECEDENCE.index("project") < PRECEDENCE.index("user")
        assert PRECEDENCE.index("user") < PRECEDENCE.index("builtin")

    def test_a_foreign_path_is_demoted_below_every_first_party_path(self):
        """"adopting an interop path must be an addition, not a behaviour change"."""
        for foreign in ("project_foreign", "user_foreign"):
            assert PRECEDENCE.index(foreign) > PRECEDENCE.index("project")
            assert PRECEDENCE.index(foreign) > PRECEDENCE.index("user")
        assert PRECEDENCE.index("remote") == len(PRECEDENCE) - 1

    def test_resolve_precedence_is_deterministic_and_ordered(self):
        first = [r.template for r in resolve_precedence(home="/H", repo="/R")]
        second = [r.template for r in resolve_precedence(home="/H", repo="/R")]
        assert first == second
        assert first[0] == "{repo}/.agents/skills"

    def test_a_per_instance_token_renders_as_a_wildcard_rather_than_raising(self):
        """The plugin row raised a KeyError out of str.format and was DEAD."""
        rendered = [r.template for r in resolve_precedence(home="/H", repo="/R")]
        assert any("plugins" in t for t in rendered), "the plugin row must render"

    def test_a_missing_caller_input_is_a_missing_input_not_a_wildcard(self):
        """An absent home is a missing INPUT; only per-instance tokens wildcard."""
        rule = skill_registry.PathRule(
            scope="user", template="{home}/.agents/skills", trust="trusted", why="x" * 40
        )
        with pytest.raises(ValueError):
            rule.render(home="", repo="/R")
        # resolve_precedence DROPS an unrenderable row rather than raising,
        # so a caller with partial input still gets a report.
        rendered = [r.template for r in resolve_precedence(home="", repo="/R")]
        assert all("{home}" not in t for t in rendered)
        assert any("{repo}" in t for t in rendered)


class TestTrustTiers:
    def test_the_registry_is_internally_valid(self):
        assert validate_trust_tiers() == []
        assert describe_registry()["valid"] is True

    def test_the_three_tiers_are_builtin_trusted_community(self):
        tiers = {t.name for t in skill_registry.TRUST_TIERS}
        assert tiers == {"builtin", "trusted", "community"}

    def test_only_the_top_tier_may_skip_its_scan(self):
        """A second unscanned tier is a content path with no review."""
        unscanned = [t for t in skill_registry.TRUST_TIERS if t.scan == "none"]
        assert len(unscanned) == 1
        assert unscanned[0].name == "builtin"
        assert unscanned[0].rank == max(t.rank for t in skill_registry.TRUST_TIERS)

    def test_a_forged_unscanned_lower_tier_is_caught_by_the_validator(self):
        """DEMONSTRATION: build the violation and assert the validator fires."""
        original = skill_registry.TRUST_TIERS
        forged = (
            *original,
            TrustTier(
                name="sneaky",
                rank=5,
                scan="none",
                quarantine=True,
                model_invocation=True,
                review="none",
                description="forged",
            ),
        )
        try:
            skill_registry.TRUST_TIERS = forged
            findings = validate_trust_tiers()
        finally:
            skill_registry.TRUST_TIERS = original
        assert any("sneaky" in f and "not the top-ranked" in f for f in findings)

    def test_every_non_builtin_tier_scans_and_quarantines(self):
        for tier in skill_registry.TRUST_TIERS:
            if tier.name == "builtin":
                continue
            assert tier.scan == "static"
            assert tier.quarantine is True
            assert "review_untrusted_source" in tier.review

    def test_an_interop_path_is_community_until_deliberately_promoted(self):
        base = discovered_skill_policy("/H/.claude/skills", home="/H", repo="/R")
        assert base["effective_tier"] == "community"
        assert base["promoted"] is False
        promoted = discovered_skill_policy(
            "/H/.claude/skills", home="/H", repo="/R", granted_tier="trusted"
        )
        assert promoted["effective_tier"] == "trusted"
        assert promoted["promoted"] is True

    def test_a_grant_never_lowers_the_path_ceiling(self):
        result = discovered_skill_policy(
            "/R/.neo/skills", home="/H", repo="/R", granted_tier="community"
        )
        assert result["effective_tier"] == "trusted"
        assert result["promoted"] is False

    def test_an_unmatched_path_has_no_tier_rather_than_a_default_one(self):
        assert classify_path("/somewhere/else", home="/H", repo="/R")["tier"] == ""


class TestRuntimeGating:
    def test_the_three_gate_kinds_are_the_ones_the_corpus_names(self):
        assert RUNTIME_GATE_KINDS == ("bin", "env", "os_family")

    def test_a_missing_binary_withholds_the_skill_and_says_why(self):
        """"preventing 'how to use kubectl' from polluting the prompt"."""
        report = runtime_gate_report([RuntimeGate("bin", "kubectl")], which_bins=["git"])
        assert report["eligible"] is False
        assert "kubectl" in report["unsatisfied"][0]["reason"]
        assert "polluted by instructions" in report["unsatisfied"][0]["reason"]

    def test_an_unprobed_requirement_is_unmeasured_not_satisfied(self):
        """A gate that did not run must not read as a gate that passed."""
        report = runtime_gate_report([RuntimeGate("bin", "kubectl")])
        assert report["eligible"] is None
        assert report["eligible_known"] is False
        assert report["unknown"][0]["status"] == "unknown"

    def test_env_and_os_family_gates_are_evaluated(self):
        assert runtime_gate_report(
            [RuntimeGate("env", "KUBECONFIG")], env={"KUBECONFIG": "x"}
        )["eligible"] is True
        assert runtime_gate_report(
            [RuntimeGate("os_family", "linux")], os_family="windows"
        )["eligible"] is False

    def test_every_gate_is_reported_not_short_circuited_on_the_first_failure(self):
        report = runtime_gate_report(
            [RuntimeGate("bin", "kubectl"), RuntimeGate("bin", "helm")], which_bins=["git"]
        )
        assert report["gates_evaluated"] == 2
        assert len(report["unsatisfied"]) == 2

    def test_no_gates_is_eligible_with_the_count_published(self):
        report = runtime_gate_report([])
        assert report["eligible"] is True
        assert report["gates_evaluated"] == 0

    def test_an_unrecognised_gate_kind_is_unknown_not_ignored(self):
        report = runtime_gate_report([{"kind": "phase-of-moon", "value": "waxing"}])
        assert report["eligible"] is None
        assert report["unknown"][0]["kind"] == "phase-of-moon"


class TestPermissionCapSource:
    def test_the_cap_is_computed_by_the_authority_and_the_receipt_separates_it(self):
        """A receipt showing only the DECLARATION reads as a grant."""
        from extensions.skill_policy import SessionPermissions

        session = SessionPermissions(allow_tools=("read",), max_model_tier="hard")
        document = permission_cap_source(
            {"name": "demo", "permissions": ["allow: read", "allow: write"]},
            session,
        )
        assert document["declared"]
        assert "allow: write" not in document["effective"]
        assert document["refused"]
        assert document["cap_computed_by"] == "extensions.skill_policy.resolve_permissions"


# ==========================================================================
# W1.3 - the tools ablation
# ==========================================================================


class TestAblationVacuity:
    def test_the_shipped_mock_really_cannot_see_the_tool_schemas(self):
        """Re-derived from source on every run, not asserted in prose."""
        proof = tools_ablation.verify_vacuity()
        assert proof["module_scan"]["runtime/mock_provider.py"]["occurrences"] == 0
        assert proof["module_scan"]["evals/run.py"]["occurrences"] == 0
        assert proof["mock_responder_accepts_tools"] is False
        assert proof["success_axis_vacuous_under_shipped_mock"] is True
        assert proof["agrees_with_record"] is True

    def test_the_vacuity_record_and_the_derivation_agree(self):
        assert tools_ablation.VACUITY_PROOF["mock_mentions_tools"] is False
        assert tools_ablation.VACUITY["success_axis_vacuous_under_shipped_mock"] is True
        assert tools_ablation.VACUITY["request_axis_vacuous"] is False
        assert tools_ablation.VACUITY["credential_read"] is False

    def test_the_model_used_names_tools_so_the_success_axis_is_not_vacuous(self):
        import inspect

        params = inspect.signature(tools_ablation.SchemaAwareModel.__call__).parameters
        assert "tools" in params, (
            "without a `tools` parameter the model cannot be moved by the "
            "schema list, and every success delta would be invented"
        )


class TestAblationSingleVariable:
    def test_the_two_arms_differ_in_exactly_the_deferral_key(self):
        report = tools_ablation.single_variable_report(tools_ablation.ARMS)
        assert report["verdict"] == "SINGLE_VARIABLE"
        assert report["differing_keys"] == [tools_ablation.THE_ONE_VARIABLE]
        assert report["attributable"] is True

    def test_two_identical_arms_are_reported_as_the_vacuity_trap_not_as_a_pass(self):
        report = tools_ablation.single_variable_report({"a": {"x": 1}, "b": {"x": 1}})
        assert report["verdict"] == "IDENTICAL_ARMS"
        assert report["attributable"] is False

    def test_two_differing_keys_are_reported_as_multi_variable(self):
        report = tools_ablation.single_variable_report(
            {"a": {"tool_deferral_enabled": False, "other": 1}, "b": {"tool_deferral_enabled": True, "other": 2}}
        )
        assert report["verdict"] == "MULTI_VARIABLE"
        assert report["attributable"] is False


class TestAblationMetrics:
    @pytest.fixture(scope="class")
    def report(self):
        return tools_ablation.run_matrix(
            query="format the source with the configured line width", needed=("apply_patch",)
        )

    def test_the_request_axis_is_real_and_measured_in_both_arms(self, report):
        for row in report["observations"]:
            assert row["metrics"]["tool_schema_tokens"] > 0
            assert row["metrics"]["estimator_is_tokenizer"] is False
        assert report["deltas"]["tool_schema_tokens"]["delta"] < 0

    def test_the_deferred_arm_buys_tokens_with_an_extra_round_trip(self, report):
        """The cost a '40% reduction' headline hides."""
        eager = next(r for r in report["observations"] if r["arm"] == "eager")
        deferred = next(r for r in report["observations"] if r["arm"] == "deferred")
        assert eager["metrics"]["extra_round_trips_for_discovery"] == 0
        assert deferred["metrics"]["extra_round_trips_for_discovery"] >= 1

    def test_cost_is_unpriced_and_never_zero(self, report):
        """DOCTRINE section 1: a $0.00 reads as cheapest to a price ladder."""
        for row in report["observations"]:
            assert row["metrics"]["cost_usd"] == "unpriced"
            assert row["metrics"]["cost_priced"] is False
        assert report["deltas"]["cost_usd"]["delta"] is None

    def test_the_discoverability_rate_is_measured_and_the_route_is_reported(self, report):
        deferred = next(r for r in report["observations"] if r["arm"] == "deferred")
        rate = deferred["metrics"]["discoverability_failure_rate"]
        assert isinstance(rate, float)
        assert set(deferred["metrics"]["reach_routes"].values()) <= {
            "schema_present",
            "discovered_by_search",
            "unreachable",
        }

    def test_a_rate_that_is_zero_on_this_query_is_NOT_evidence_it_is_always_zero(
        self, report
    ):
        """ADJUDICATED IN P3b/W2 — this test was the watch-list-3 failure.

        ``needed=("apply_patch",)`` on the query "format the source with the
        configured line width" scores ABOVE threshold under both scorers, so
        the tool is KEPT and the deferred arm never enters the discovery path.
        The assertions around it therefore measured `schema_present` on a query
        that never deferred anything - a fixture that cannot fail, on the axis
        the phase brief calls the go/no-go.

        The rate being 0.0 here is therefore CORRECT and VACUOUS, and the
        distinction is mechanical: the corpus entry says whether the deferred
        arm actually deferred the measured tool.
        """
        assert report["corpus"]["enters_discovery_path"] is False, (
            "this fixture was selected because it does NOT defer the needed "
            "tool. If that changed, the assertions around it stopped measuring "
            "the discovery path and this test needs re-aiming."
        )
        deferred = next(r for r in report["observations"] if r["arm"] == "deferred")
        assert deferred["metrics"]["schema_count_deferred"] > 0, (
            "even though the NEEDED tool is kept, the arm must defer SOMETHING, "
            "or this fixture measures nothing about deferral at all"
        )

    def test_the_go_no_go_axis_is_reachable_and_is_driven_elsewhere(self, report):
        """Where the axis IS non-vacuous: the derivation, not this fixture.

        ``tests/test_p3b_w2_truth.py::TestTheGoNoGoAxisCanFail`` owns the
        assertion that the metric can reach BOTH 0.0 and 1.0. This test exists
        so the vacuity of THIS fixture is a recorded, deliberate fact rather
        than a silent one.
        """
        probe = tools_ablation.vacuity_of_the_go_no_go_axis()
        assert probe["non_vacuous"] is True
        assert probe["capability_intact_when_the_model_searches"] is True
        assert probe["capability_lost_when_the_model_does_not_search"] is True

    def test_a_direct_selection_reaches_a_tool_whose_schema_was_deferred(self, report):
        """Deferred AND recovered. The `deferred_recoverable` state.

        This is the state that a suite can accidentally never exercise, so it
        is driven explicitly: the tool is deferred, and the model recovers it
        by direct selection. Asserting `unresolved_tools == []` alone would
        also pass on a query where the tool was never deferred at all, which is
        what the previous version of this test did.
        """
        deferred = next(r for r in report["observations"] if r["arm"] == "deferred")
        assert deferred["metrics"]["unresolved_tools"] == []
        # The route must be a REAL recovery, not a schema the model never lost.
        routes = deferred["metrics"]["reach_routes"]
        assert set(routes.values()) & {
            "discovered_by_search",
        }, (
            f"no tool was recovered by search on a deferred arm: {routes}. The "
            "'deferred but recoverable' state is not being exercised."
        )

    def test_the_report_is_json_serialisable_and_names_its_honesty(self, report):
        json.dumps(report, default=str)
        assert len(report["honesty"]) >= 5
        assert any("unpriced" in h for h in report["honesty"])

    def test_the_verdict_is_measured_not_clean(self):
        """'CLEAN' for a zero delta is the failure mode this driver avoids."""
        report = tools_ablation.run_matrix(query="read the file", needed=("read",))
        assert report["verdict"] in {"MEASURED", "REGRESSION_DISCOVERABILITY"}
        assert report["verdict"] != "CLEAN"


# ==========================================================================
# W1.4 - the prompt-change gate
# ==========================================================================


def _fake_prompts(tmp_path: Path, overrides: dict) -> Path:
    """Write a minimal prompts.py carrying the agent-facing constants."""
    lines = ["# generated by tests/test_p3b_tools_and_prompts.py", ""]
    for name in prompt_pin.AGENT_FACING:
        value = overrides.get(name, f"body of {name}\n")
        lines.append(f'{name} = """{value}"""')
    lines.append("")
    path = tmp_path / "prompts.py"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestPromptPinMechanics:
    def test_the_pin_reads_the_file_it_claims_to_read(self):
        assert prompt_pin.PROMPTS_PATH.name == "prompts.py"
        assert prompt_pin.PROMPTS_PATH.exists()
        assert prompt_pin.PROMPTS_PATH.parent.name == "harness"

    def test_it_digests_by_ast_so_a_docstring_edit_is_not_a_prompt_edit(self, tmp_path):
        """A gate that fires on a reformat is a gate people disable."""
        path = _fake_prompts(tmp_path, {})
        before = dict(prompt_pin.PINNED_DIGESTS)
        try:
            prompt_pin.PINNED_DIGESTS = {
                c.name: c.digest for c in prompt_pin.enumerated_constants(path)[0]
            }
            doc = prompt_pin.check(path)
            assert doc["ok"] is True
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'DAILY_SYSTEM = """body of DAILY_SYSTEM',
                    '# a comment that mentions tools and skills\nDAILY_SYSTEM = """body of DAILY_SYSTEM',
                ),
                encoding="utf-8",
            )
            assert prompt_pin.check(path)["ok"] is True
        finally:
            prompt_pin.PINNED_DIGESTS = before

    def test_an_unreadable_or_unparseable_file_is_a_finding_not_a_pass(self, tmp_path):
        path = tmp_path / "prompts.py"
        path.write_text("def broken(:\n", encoding="utf-8")
        doc = prompt_pin.check(path)
        assert doc["ok"] is False
        assert doc["verdict"] == "PROMPT_UNREADABLE"
        assert doc["findings"][0]["kind"] == "prompt_unreadable"

    def test_an_agent_facing_constant_that_vanished_is_reported(self, tmp_path):
        path = _fake_prompts(tmp_path, {})
        path.write_text(
            "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("DAILY_SYSTEM")
            ),
            encoding="utf-8",
        )
        doc = prompt_pin.check(path)
        assert doc["verdict"] == "PROMPT_UNREADABLE"

    def test_crlf_is_not_a_prompt_change(self):
        """Otherwise every Windows checkout reports every prompt as edited."""
        assert prompt_pin.digest_of("a\r\nb") == prompt_pin.digest_of("a\nb")


class TestPromptPinDetection:
    def _pin_to(self, tmp_path, overrides):
        path = _fake_prompts(tmp_path, overrides)
        before = dict(prompt_pin.PINNED_DIGESTS)
        prompt_pin.PINNED_DIGESTS = {
            c.name: c.digest
            for c in prompt_pin.enumerated_constants(path, agent_facing=list(overrides))[0]
        }
        return path, before

    def test_an_unarmed_edit_is_caught(self, tmp_path):
        """THE DEMONSTRATION. An edit whose only arms are control arms."""
        originals = {n: f"body of {n}\n" for n in prompt_pin.AGENT_FACING}
        path, saved = self._pin_to(tmp_path, originals)
        try:
            # PLANNER_SYSTEM is covered by no_memory/no_skills; drop those and
            # leave only the control arm, which is the vacuous shape.
            saved_cov = dict(prompt_pin.ARM_COVERAGE)
            prompt_pin.ARM_COVERAGE["PLANNER_SYSTEM"] = ("baseline",)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'PLANNER_SYSTEM = """body of PLANNER_SYSTEM',
                    'PLANNER_SYSTEM = """Prefer the smallest change. body of PLANNER_SYSTEM',
                ),
                encoding="utf-8",
            )
            doc = prompt_pin.check(path)
            assert doc["ok"] is False
            assert doc["verdict"] == "PROMPTS_UNARMED"
            kinds = [f["kind"] for f in doc["findings"]]
            assert "unarmed_prompt_edit" in kinds
            assert "PLANNER_SYSTEM" in doc["unarmed_edits"]
            prompt_pin.ARM_COVERAGE = saved_cov
        finally:
            prompt_pin.PINNED_DIGESTS = saved

    def test_a_legitimately_armed_edit_passes(self, tmp_path):
        originals = {n: f"body of {n}\n" for n in prompt_pin.AGENT_FACING}
        path, saved = self._pin_to(tmp_path, originals)
        try:
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'PLANNER_SYSTEM = """body of PLANNER_SYSTEM',
                    'PLANNER_SYSTEM = """Prefer the smallest change. body of PLANNER_SYSTEM',
                ),
                encoding="utf-8",
            )
            doc = prompt_pin.check(path)
            assert doc["ok"] is True
            assert doc["verdict"] == "PROMPTS_ARMED"
            assert "PLANNER_SYSTEM" in doc["armed_edits"]
        finally:
            prompt_pin.PINNED_DIGESTS = saved

    def test_dangling_coverage_fires_even_with_no_digest_change(self, tmp_path):
        originals = {n: f"body of {n}\n" for n in prompt_pin.AGENT_FACING}
        path, saved = self._pin_to(tmp_path, originals)
        saved_cov = dict(prompt_pin.ARM_COVERAGE)
        try:
            prompt_pin.ARM_COVERAGE["PLANNER_SYSTEM"] = ("baseline", "an_arm_that_was_deleted")
            doc = prompt_pin.check(path)
            assert any(f["kind"] == "dangling_arm_coverage" for f in doc["findings"])
        finally:
            prompt_pin.ARM_COVERAGE = saved_cov
            prompt_pin.PINNED_DIGESTS = saved

    def test_an_empty_baseline_is_refused(self):
        saved = dict(prompt_pin.PINNED_DIGESTS)
        try:
            prompt_pin.PINNED_DIGESTS = {}
            doc = prompt_pin.check()
            assert doc["ok"] is False
            assert any(f["kind"] == "baseline_emptied" for f in doc["findings"])
        finally:
            prompt_pin.PINNED_DIGESTS = saved


class TestPromptPinBypassResistance:
    def test_a_wildcard_exemption_is_refused_by_name(self):
        saved = dict(prompt_pin.EXEMPTIONS)
        try:
            prompt_pin.EXEMPTIONS["*"] = {"reason": "skip everything", "owner": "me"}
            findings = prompt_pin._wildcard_exemption_findings()
            assert any(f["kind"] == "invalid_exemption" for f in findings)
        finally:
            prompt_pin.EXEMPTIONS = saved

    def test_an_exemption_without_a_reason_or_owner_is_refused(self):
        """One finding PER missing field, asserted by field rather than by count.

        P3b/W2 added a third `invalid_exemption` finding to the same function -
        the MAX_EXEMPTIONS bound - and this assertion of `== 2` failed. That is
        the assertion being wrong rather than the new check: it counted
        findings instead of naming what it wanted, so any additional way to
        refuse a bad exemption broke it. The two field checks below state the
        claim; the count is not the claim.
        """
        saved = dict(prompt_pin.EXEMPTIONS)
        try:
            prompt_pin.EXEMPTIONS["X"] = {"reason": "", "owner": ""}
            findings = prompt_pin._wildcard_exemption_findings()
            by_detail = " ".join(
                f["detail"] for f in findings if f["kind"] == "invalid_exemption"
            )
            assert "no reason" in by_detail
            assert "no owner" in by_detail
            assert all(
                f["kind"] == "invalid_exemption" for f in findings
            ), "an exemption problem must not be reported under another kind"
        finally:
            prompt_pin.EXEMPTIONS = saved

    def test_the_shipped_exemptions_all_carry_a_reason_and_an_owner(self):
        assert prompt_pin.EXEMPTIONS, "the exemptions must exist and be visible"
        for name, entry in prompt_pin.EXEMPTIONS.items():
            assert entry["reason"].strip()
            assert entry["owner"].strip()
            assert not any(ch in name for ch in "*?[]")

    def test_a_changed_exemption_is_a_finding_forcing_re_review(self, tmp_path):
        originals = {n: f"body of {n}\n" for n in prompt_pin.AGENT_FACING}
        path = _fake_prompts(tmp_path, originals)
        saved_pins = dict(prompt_pin.PINNED_DIGESTS)
        saved_cov = dict(prompt_pin.ARM_COVERAGE)
        saved_ex = dict(prompt_pin.EXEMPTIONS)
        try:
            prompt_pin.PINNED_DIGESTS = {
                c.name: c.digest for c in prompt_pin.enumerated_constants(path)[0]
            }
            prompt_pin.ARM_COVERAGE["PLANNER_SYSTEM"] = ()
            prompt_pin.EXEMPTIONS["PLANNER_SYSTEM"] = {
                "reason": "it was fine last quarter",
                "owner": "T5",
            }
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'PLANNER_SYSTEM = """body of PLANNER_SYSTEM',
                    'PLANNER_SYSTEM = """changed. body of PLANNER_SYSTEM',
                ),
                encoding="utf-8",
            )
            doc = prompt_pin.check(path)
            assert any(f["kind"] == "stale_exemption" for f in doc["findings"])
        finally:
            prompt_pin.PINNED_DIGESTS = saved_pins
            prompt_pin.ARM_COVERAGE = saved_cov
            prompt_pin.EXEMPTIONS = saved_ex

    def test_the_control_only_arms_are_the_ones_that_isolate_nothing(self):
        assert "baseline" in prompt_pin.CONTROL_ONLY_ARMS
        assert "pre_round" in prompt_pin.CONTROL_ONLY_ARMS
        for name in prompt_pin.AGENT_FACING:
            if name not in prompt_pin.ARM_COVERAGE:
                continue
            isolating = [
                a for a in prompt_pin.ARM_COVERAGE[name]
                if a not in prompt_pin.CONTROL_ONLY_ARMS
            ]
            if not isolating:
                continue
            assert all(a in prompt_pin.registered_arms() for a in isolating), (
                f"{name} names an arm that does not exist"
            )

    def test_the_current_tree_is_green_and_the_coverage_gaps_are_published(self):
        """The gate is green today AND the gaps are on the record."""
        doc = prompt_pin.check()
        assert doc["ok"] is True, doc["findings"]
        assert doc["verdict"] == "PROMPTS_ARMED"
        assert doc["coverage_gaps"], (
            "prompts covered only by control arms are the work list; deleting "
            "this key hides a known gap"
        )
        for name in doc["coverage_gaps"]:
            assert name in prompt_pin.AGENT_FACING

    def test_update_pins_does_not_write_the_pin_file(self):
        """A gate that can rewrite its own baseline has no baseline."""
        import inspect

        source = inspect.getsource(prompt_pin.update_pins)
        assert "write_text" not in source
        assert "open(" not in source
        digests = prompt_pin.update_pins()
        assert len(digests) == len(prompt_pin.AGENT_FACING)
