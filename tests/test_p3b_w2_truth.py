"""P3b/W2-T5: the go/no-go axis, the six honest-absent rules, and the pin.

Read this before adding a test here. Every test in this file exists because a
measurement was WRONG or a pin was DECORATIVE in this phase, and each one is
written so it cannot pass vacuously - which in practice means each asserts BOTH
directions, because a rule that only checks the absent path is satisfied by a
function that returns a constant.
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest

import evals.prompt_pin as pin
import evals.tools_ablation as ta
import shared.p3b_truth as truth

REPO_ROOT = Path(__file__).resolve().parents[1]


# ======================================================================
# The go/no-go axis. THIS IS THE HEADLINE GATE.
# ======================================================================


class TestTheGoNoGoAxisCanFail:
    """The metric this phase names as go/no-go was STRUCTURALLY 0.0.

    Three separate reasons, each found by driving the metric rather than by
    reading the code, and each fixed. All three are pinned here because each
    one is a way the metric silently returns to 0.0.
    """

    def test_the_axis_is_not_vacuous(self):
        """The gate. Exits 2 if the metric cannot reach a non-zero value."""
        doc = ta.vacuity_of_the_go_no_go_axis()
        assert doc["non_vacuous"] is True, (
            "the discoverability axis cannot fail; a metric that cannot fail is "
            "not a go/no-go. " + doc["consequence"]
        )

    def test_the_axis_can_reach_both_zero_and_non_zero(self):
        """Both directions, not just "it moved once"."""
        doc = ta.vacuity_of_the_go_no_go_axis()
        rates = [r for r in doc["observed_rates"] if isinstance(r, (int, float))]
        assert rates, "no numeric rate was observed at all"
        assert min(rates) == 0.0, "the metric never reports a clean reach"
        assert max(rates) > 0.0, "the metric never reports a loss"

    def test_capability_is_intact_when_the_model_searches(self):
        """THE CONTROL ARM. Without this the gate above can pass while the
        deferred arm silently destroys capability."""
        doc = ta.vacuity_of_the_go_no_go_axis()
        assert doc["capability_intact_when_the_model_searches"] is True

    def test_capability_is_lost_when_the_model_never_searches(self):
        """THE CHARGED ARM. The whole point of the metric."""
        doc = ta.vacuity_of_the_go_no_go_axis()
        assert doc["capability_lost_when_the_model_does_not_search"] is True

    def test_a_model_that_never_searches_scores_one_not_zero(self):
        """The specific regression the unconditional-resolution version hid.

        The first version resolved the needed tool whether or not the model
        searched, so a model that did not look scored 0.0 - a perfect
        discoverability rate for a model that had no idea the tool existed.
        """
        doc = ta.vacuity_of_the_go_no_go_axis()
        blind_deferred = [
            m
            for row in doc["sweep"]
            if "skipped" not in row
            for m in row["models"]
            if m["arm"] == "deferred" and m["model_behaviour"] == "never_searches"
        ]
        assert blind_deferred, "no blind-model rows: the sweep is not exercising this"
        for m in blind_deferred:
            assert m["discoverability_failure_rate"] == 1.0, (
                "a model that never searched reported a rate other than 1.0 for "
                f"the tools it did not have: {m}"
            )

    def test_a_model_that_searches_costs_extra_round_trips(self):
        """Capability survives, but it is BOUGHT. Free tokens, paid latency.

        If the round trips stop being reported the token saving becomes a
        latency claim by silence, which is the specific error the metric's
        own comment warns about.
        """
        doc = ta.vacuity_of_the_go_no_go_axis()
        searching_deferred = [
            m
            for row in doc["sweep"]
            if "skipped" not in row
            for m in row["models"]
            if m["arm"] == "deferred" and m["model_behaviour"] == "searches_when_needed"
        ]
        assert searching_deferred
        for m in searching_deferred:
            assert m["extra_round_trips"] > 0, (
                "the deferred arm recovered every tool at ZERO extra round "
                "trips; discovery is not free and a zero here would be the "
                f"token win being read as a latency win: {m}"
            )

    def test_the_eager_arm_never_pays_a_round_trip(self):
        """The control for the row above, in the OTHER direction."""
        doc = ta.vacuity_of_the_go_no_go_axis()
        for row in doc["sweep"]:
            if "skipped" in row:
                continue
            for m in row["models"]:
                if m["arm"] == "eager":
                    assert m["extra_round_trips"] == 0


class TestTheDeferringCaseIsDerived:
    """A hand-written 'this query defers it' is a claim about a scorer.

    The first version of the probe named one query as the deferring case. It
    did not defer anything under either registered scorer; only the empty query
    did, and it defers 46 of 47 tools, which is the degenerate blind-agent
    policy. The case is now DERIVED, and the degenerate case is refused.
    """

    def test_a_deferring_case_exists(self):
        assert ta.derive_a_deferring_case() is not None

    def test_the_derived_case_is_not_the_degenerate_empty_query(self):
        case = ta.derive_a_deferring_case()
        assert case is not None
        assert case["deferred_fraction"] <= ta.MAX_DEFERRED_FRACTION, (
            "the derived case defers almost the entire catalog, which measures "
            "a blind agent rather than the mechanism"
        )

    def test_the_derivation_publishes_what_it_rejected_and_why(self):
        case = ta.derive_a_deferring_case()
        assert case is not None
        assert "rejected_as_degenerate" in case
        assert "selection_rule" in case

    def test_no_english_description_escapes_the_shipped_scorer(self):
        """THE FINDING, pinned. Not "my fixture is bad" - "the scorer is blind".

        The substring scorer tests `term in description` with no word boundary
        and no stopword list. Every English sentence contains "the", "a" or
        "an", so at threshold 0.0 an English-described tool is KEPT for almost
        any query and is **essentially never deferred**.

        Measured on a description with no name match at all: still 6.0. This is
        why the go/no-go derivation needs :data:`DERIVABLE_DESCRIPTION` and why
        the token saving is partly a stopword artefact rather than a
        prompt-budget win.
        """
        from integrations.tool_search import SearchCandidate, default_scorer

        scorer = default_scorer()
        unrelated = "explain what this module does and summarise the architecture"
        for label, description in (
            ("generic_placeholder", f"the {ta._MEASURED_TOOL} tool"),
            ("realistic_english", ta.MEASURED_TOOL_DESCRIPTION),
        ):
            candidate = SearchCandidate(name="zzqx_fetch", description=description)
            assert scorer.score(unrelated, candidate)[0] > -1.0, (
                f"the {label} description was NOT matched by an unrelated query. "
                "If this now fails it means the scorer learned about word "
                "boundaries - in which case DERIVABLE_DESCRIPTION is no longer "
                "needed and MEASURED_TOOL_DESCRIPTION becomes genuinely "
                "deferrable, which is the real fix this test is watching for."
            )

    def test_a_single_letter_query_term_is_enough_to_match_anything(self):
        """Why :data:`DERIVABLE_DESCRIPTION` avoids the letters a/e/i/o/u/y.

        This is the non-obvious half of the substring defect and it is the
        reason a plausible-looking fixture description still fails: the test is
        `term in description`, so the one-character query term "a" matches any
        description containing the letter a. Measured on the first attempt at a
        "stopword-free" fixture, which contained "state".
        """
        from integrations.tool_search import SearchCandidate, default_scorer

        scorer = default_scorer()
        candidate = SearchCandidate(
            name="zzqx_fetch", description="shrd consensus reached quorum"
        )
        score, fields = scorer.score("write a release note", candidate)
        assert score > -1.0, (
            "a one-letter query term no longer matches a description; the "
            "substring test has gained a word boundary"
        )
        assert "description" in fields

    def test_the_derivable_description_is_genuinely_unmatchable(self):
        """THE CONTROL for the test above, and the reason it is not a bug fix.

        This string is not a realistic tool description and is never shipped.
        Its only job is to be the one shape the substring scorer cannot match by
        accident, so the go/no-go axis becomes reachable.

        **The constraint is stronger than "no English function words".** The
        scorer tests `term in description` with no word boundary, so a
        SINGLE-LETTER query term is fatal: "write a release note" contains "a",
        so the description may not contain the letter "a". The first attempt
        ("quorum ledger shard state reconciliation") was dropped for exactly
        this reason - "state" contains an "a" - and the derivation silently fell
        back to the degenerate empty query, which would have looked like a
        passing test and measured a blind agent.
        """
        from integrations.tool_search import SearchCandidate, default_scorer

        scorer = default_scorer()
        candidate = SearchCandidate(
            name="zzqx_fetch", description=ta.DERIVABLE_DESCRIPTION
        )
        for query in ta.QUERIES.values():
            score = scorer.score(query, candidate)[0]
            assert score == -1.0 or query == "", (
                f"the derivable description is matched by {query!r} (score "
                f"{score}); the go/no-go derivation can no longer reach a "
                "deferred state and the axis re-vacuums"
            )

    def test_a_realistic_description_is_used_for_ordinary_runs(self):
        """The contrived description must not leak into the headline number.

        Only the go/no-go DERIVATION uses the derivable string. If it leaked
        into a normal `run_matrix`, the published token figure would describe a
        tool that does not exist.
        """
        report = ta.run_matrix()
        assert (
            report["corpus"]["measured_tool_description"]
            == ta.MEASURED_TOOL_DESCRIPTION
        )
        assert ta.DERIVABLE_DESCRIPTION not in str(report["observations"])

    def test_the_shipped_scorers_are_swept_not_a_hardcoded_list(self):
        from integrations.tool_search import TOOL_SEARCH_SCORERS

        assert set(ta._REGISTERED_SCORERS) == set(TOOL_SEARCH_SCORERS), (
            "the go/no-go sweep must cover every registered scorer, or a scorer "
            "added tomorrow is silently uncovered"
        )


class TestTheArmsDifferInExactlyOneVariable:
    """Wave 1's check, still the entry condition for anything being publishable."""

    def test_the_shipped_arms_are_single_variable(self):
        report = ta.single_variable_report(ta.ARMS)
        assert report["single_variable"] is True
        assert report["differing_keys"] == [ta.THE_ONE_VARIABLE]

    def test_two_identical_arms_are_reported_as_the_vacuity_trap(self):
        """The inverted control: the check must be able to FAIL."""
        report = ta.single_variable_report({"a": {"x": 1}, "b": {"x": 1}})
        assert report["verdict"] == "IDENTICAL_ARMS"
        assert report["attributable"] is False

    def test_two_differing_keys_are_multi_variable(self):
        report = ta.single_variable_report({"a": {"x": 1}, "b": {"x": 2}})
        assert report["verdict"] == "MULTI_VARIABLE"
        assert report["attributable"] is False

    def test_the_single_variable_check_runs_on_the_configs_the_run_used(self):
        """A check run on a dict the measurement did not use is not a check."""
        report = ta.run_matrix(scorer="bm25")
        assert report["single_variable"]["differing_keys"] == [ta.THE_ONE_VARIABLE]
        assert report["scorer"] == "bm25"


class TestRepetitionsAndVariance:
    """'One run per arm is noise' - and the spread is reported both ways."""

    def test_repeats_produce_a_report(self):
        doc = ta.run_repeated(repeats=2)
        assert doc["repeats"] == 2
        assert doc["per_query"]

    def test_spread_is_reported_for_every_axis_not_just_the_mean(self):
        doc = ta.run_repeated(repeats=2)
        for q in doc["per_query"]:
            for arm, axes in q["spread"].items():
                for axis, stat in axes.items():
                    assert "spread" in stat, f"{arm}/{axis} has no spread"
                    assert stat["n"] >= 1

    def test_a_deterministic_driver_reports_zero_spread_and_says_why(self):
        """spread==0 must be labelled as a property of the DRIVER.

        A tidy zero with no explanation is the presentation this phase's brief
        warns about: it reads as stability and is actually determinism.
        """
        doc = ta.run_repeated(repeats=3)
        for q in doc["per_query"]:
            for axes in q["spread"].values():
                for stat in axes.values():
                    if stat["spread"] == 0.0 and stat["mean"] is not None:
                        assert "deterministic" in stat["note"], (
                            "a zero spread must say it is the driver's "
                            "determinism, not evidence about a model"
                        )

    def test_cross_query_spread_is_reported_separately(self):
        """The variation that actually matters, and it is NOT the same axis."""
        doc = ta.run_repeated(repeats=2)
        assert "cross_query" in doc
        assert set(doc["cross_query"]) == set(ta.ARMS)
        for axes in doc["cross_query"].values():
            assert "tool_schema_tokens" in axes

    def test_zero_repeats_raises_rather_than_returning_an_empty_report(self):
        """An empty report reads as 'no data, no problem'."""
        with pytest.raises(ValueError):
            ta.run_repeated(repeats=0)

    def test_an_axis_with_no_numeric_sample_is_not_given_a_zero_spread(self):
        """`None`, not 0.0, for an axis nobody measured."""
        stat = ta.variance(["unpriced", "unpriced"])
        assert stat["mean"] is None
        assert stat["spread"] is None
        assert stat["n"] == 0
        assert stat["n_absent"] == 2

    def test_a_bool_is_not_a_number_on_a_numeric_axis(self):
        stat = ta.variance([True, False, 1.0])
        assert stat["n"] == 1
        assert stat["n_absent"] == 2


class TestTheScorerAudit:
    """The token number is scorer-dependent, and that is published."""

    def test_the_audit_reports_both_registered_scorers(self):
        doc = ta.scorer_audit()
        assert set(doc["scorers"]) == set(ta._REGISTERED_SCORERS)

    def test_the_audit_measures_agreement_rather_than_asserting_it(self):
        doc = ta.scorer_audit()
        assert doc["mean_jaccard"] is not None
        for row in doc["queries"]:
            assert "jaccard" in row

    def test_the_audit_measures_the_stopword_share(self):
        doc = ta.scorer_audit()
        for row in doc["queries"]:
            assert set(row["stopword_only_share"]) == set(doc["scorers"])
            for share in row["stopword_only_share"].values():
                assert share is None or 0.0 <= share <= 1.0

    def test_the_audit_states_that_the_default_was_not_switched(self):
        """Changing the default here would move the headline in the flattering
        direction without anybody having decided to move it."""
        doc = ta.scorer_audit()
        assert "NOT switched" in doc["not_changed"]
        assert doc["default_scorer"] == "substring"


class TestTheB25ScorerIsAlive:
    """A scorer that returns nothing for everything is a policy inverted.

    `with_tokens` guarded on `name_tokens and tag_tokens`. A tool with NO tags
    has `tag_tokens == ()`, which is falsy, so the guard never passed and every
    call minted a new object. `fit()` normalises its input again, so the
    identity lookup in `score()` raised StopIteration and returned NO_MATCH for
    every candidate. Measured: BM25 returned 0 of 45 hits where substring
    returned 33-44.
    """

    def test_bm25_returns_hits_on_the_real_catalog(self):
        from harness.tools import typed_tool_specs
        from integrations.tool_search import SearchCandidate, rank_tools, scorer_by_name

        cands = [
            SearchCandidate(
                name=str(s.name), description=str(s.description), tags=()
            )
            for s in typed_tool_specs()
        ]
        hits = rank_tools(
            "read the configuration file",
            cands,
            scorer=scorer_by_name("bm25"),
            limit=len(cands),
        )
        assert hits, "BM25 matched NOTHING; the identity lookup is broken again"

    def test_a_tool_with_no_tags_is_still_matched(self):
        from integrations.tool_search import SearchCandidate, with_tokens

        candidate = SearchCandidate(name="read", description="Read a file", tags=())
        first = with_tokens(candidate)
        second = with_tokens(first)
        assert first is second, (
            "with_tokens minted a second object for a tagless tool, which "
            "breaks BM25's identity lookup and silently returns NO_MATCH"
        )

    def test_with_tokens_is_idempotent_for_every_real_tool(self):
        """Every one of the 45 harness tools is built with ``tags=()``, so
        "every one of them fails the guard" was the whole defect."""
        from harness.tools import typed_tool_specs
        from integrations.tool_search import SearchCandidate, with_tokens

        for spec in typed_tool_specs():
            candidate = SearchCandidate(
                name=str(spec.name), description=str(spec.description), tags=()
            )
            once = with_tokens(candidate)
            assert with_tokens(once) is once, (
                f"with_tokens is not idempotent for {spec.name!r}; BM25's "
                "identity lookup will miss and return NO_MATCH for it"
            )

    def test_the_two_scorers_disagree_and_the_report_says_so(self):
        """The negative control: they are NOT identical, and the audit is not
        publishing agreement it did not measure."""
        doc = ta.scorer_audit()
        assert doc["mean_jaccard"] < 1.0, (
            "the scorers agree perfectly, which means either the audit is "
            "broken or the corpus has collapsed to one term"
        )


# ======================================================================
# The prompt pin. It must FIRE and it must NOT FIRE, and the bypasses must
# stay closed.
# ======================================================================


def _pin_check_on(tmp_path: Path, prompts_source: str) -> dict:
    """Run the pin against a scratch copy of the prompt module.

    ``newline=""`` is load-bearing. Without it Python's text mode translates
    every ``\\n`` to the platform separator, so a fixture already containing
    CRLF comes back as CR-CRLF - and the resulting pin failure looks like a
    genuine line-ending bug in the gate rather than a bug in the test harness.
    That is the exact shape of the cry-wolf failure this module documents.
    """
    target = tmp_path / "prompts.py"
    with io.open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(prompts_source)
    return pin.check(target)


class TestThePinFires:
    """The transcript, as assertions. Each one is a real edit to real text."""

    @staticmethod
    def _edited(source: str) -> str:
        anchor = "parallel calls only when every call is explicitly read-only."
        assert anchor in source
        return source.replace(
            anchor, anchor + " Always prefer fewer tools.", 1
        )

    def test_an_unarmed_prompt_edit_fails_the_gate(self, tmp_path):
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        doc = _pin_check_on(tmp_path, self._edited(source))
        assert doc["ok"] is False
        assert doc["verdict"] == "PROMPTS_UNARMED"
        kinds = {f["kind"] for f in doc["findings"]}
        assert "unarmed_prompt_edit" in kinds

    def test_the_unchanged_tree_passes(self, tmp_path):
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        doc = _pin_check_on(tmp_path, source)
        assert doc["ok"] is True, [f["detail"] for f in doc["findings"]]
        assert doc["findings"] == []

    def test_the_finding_names_the_prompt_and_the_remedy(self, tmp_path):
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        doc = _pin_check_on(tmp_path, self._edited(source))
        finding = next(
            f for f in doc["findings"] if f["kind"] == "unarmed_prompt_edit"
        )
        assert finding["constant"] == "DAILY_SYSTEM"
        assert "ARM_COVERAGE" in finding["detail"]
        assert "--update" in finding["detail"]


class TestThePinDoesNotCryWolf:
    """A gate that fires on a reformat is a gate people disable."""

    def test_reformatting_the_SOURCE_is_not_a_prompt_change(self, tmp_path):
        """The property the AST design exists for.

        Re-indenting the assignment, splitting the literal, and inserting a
        comment are all source-level churn: the constant's VALUE is identical,
        so the digest is identical. A file-level digest would fire on all three
        and a gate that cries wolf is a gate people disable.
        """
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        churned = source.replace(
            "DAILY_SYSTEM = ",
            "# a comment that is not prompt text\nDAILY_SYSTEM = ",
            1,
        )
        doc = _pin_check_on(tmp_path, churned)
        assert doc["ok"] is True, "a comment above an assignment fired the gate"
        row = next(r for r in doc["constants"] if r["constant"] == "DAILY_SYSTEM")
        assert row["state"] == "unchanged"

    def test_whitespace_INSIDE_the_prompt_does_fire(self, tmp_path):
        """The inverse, because it is the half people get wrong.

        Adding a blank line INSIDE the triple-quoted literal changes what the
        model reads, so the gate MUST fire. Asserting the direction explicitly
        is what stops somebody 'fixing' the AST scan into a source digest.
        """
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        edited = source.replace(
            "parallel calls only when every call is explicitly read-only.",
            "parallel calls only when every call is explicitly read-only.\n\n",
            1,
        )
        doc = _pin_check_on(tmp_path, edited)
        assert doc["ok"] is False, (
            "whitespace inside the prompt did NOT fire; the gate is now reading "
            "the source rather than the value"
        )
        assert doc["constants"] != []

    def test_a_comment_elsewhere_is_not_a_prompt_change(self, tmp_path):
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        doc = _pin_check_on(tmp_path, "# a new comment\n" + source)
        assert doc["ok"] is True

    def test_a_digest_is_line_ending_independent(self, tmp_path):
        """A CRLF checkout must not report every prompt as edited."""
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        crlf = source.replace("\n", "\r\n")
        doc = _pin_check_on(tmp_path, crlf)
        assert doc["ok"] is True, "CRLF translation fired the gate"


class TestThePinBypassesStayClosed:
    """Each bypass is a way the rule becomes decorative.

    Wave 1 closed four of them. These pin the closures, and the LAST TWO are
    this wave's additions.
    """

    def test_a_wildcard_exemption_is_refused(self):
        pin.EXEMPTIONS["PROMPT_*"] = {"reason": "x", "owner": "y"}
        try:
            findings = pin._wildcard_exemption_findings()
            assert any(
                f["kind"] == "invalid_exemption" and "PROMPT_*" in f.get("constant", "")
                for f in findings
            )
        finally:
            pin.EXEMPTIONS.pop("PROMPT_*")

    def test_an_exemption_with_no_reason_is_refused(self):
        pin.EXEMPTIONS["SOME_CONSTANT"] = {"owner": "me"}
        try:
            findings = pin._wildcard_exemption_findings()
            assert any(
                f["kind"] == "invalid_exemption" and "no reason" in f["detail"]
                for f in findings
            )
        finally:
            pin.EXEMPTIONS.pop("SOME_CONSTANT")

    def test_an_exemption_with_no_owner_is_refused(self):
        pin.EXEMPTIONS["SOME_CONSTANT"] = {"reason": "because"}
        try:
            findings = pin._wildcard_exemption_findings()
            assert any(
                f["kind"] == "invalid_exemption" and "no owner" in f["detail"]
                for f in findings
            )
        finally:
            pin.EXEMPTIONS.pop("SOME_CONSTANT")

    def test_an_exemption_does_not_silence_a_changed_digest(self, tmp_path):
        """THE EXEMPTION BYPASS. A reason and an owner are not a get-out.

        `EXEMPTIONS["DAILY_SYSTEM"] = {...}` plus an edit still fails, because a
        changed digest under an exemption is a `stale_exemption` FINDING - the
        recorded digest has to move deliberately so the re-review shows up in a
        diff on this file.
        """
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        edited = source.replace(
            "parallel calls only when every call is explicitly read-only.",
            "parallel calls only when every call is explicitly read-only. Be brief.",
            1,
        )
        pin.EXEMPTIONS["DAILY_SYSTEM"] = {
            "reason": "audit probe",
            "owner": "audit",
            "reviewed": "audit",
        }
        try:
            doc = _pin_check_on(tmp_path, edited)
            assert doc["ok"] is False, (
                "an exemption silenced an unarmed prompt edit"
            )
            kinds = {f["kind"] for f in doc["findings"]}
            assert "stale_exemption" in kinds
        finally:
            pin.EXEMPTIONS.pop("DAILY_SYSTEM")

    def test_shrink_the_coverage_AND_move_the_digest_is_refused(self, tmp_path):
        """THE BYPASS THAT WAS OPEN, and it was the whole gate.

        **This failed on the first run of this suite.** Deleting a constant from
        ``ARM_COVERAGE`` and then running ``--update`` and pasting the new digest
        passes every rule the gate had:

        * the digest now matches, so there is no edit to report;
        * ``MIN_PINNED_CONSTANTS`` is satisfied, because a digest was moved
          rather than deleted;
        * ``CONTROL_ONLY_ARMS`` is never consulted, because the constant has no
          coverage entry left to consult;
        * ``EXEMPTIONS`` is untouched, so no exemption rule fires.

        The result was ``ok: True`` on an unarmed edit to the loop's system
        prompt - which is the exact -2.3pp change the gate exists to police,
        passing because its coverage table was edited in the same commit. The
        bypass was found by ATTACKING the gate rather than by reading it.

        The fix is :data:`REVIEWED_AGENT_FACING`: a constant that is no longer
        in ``ARM_COVERAGE`` is reported as ``unclassified`` and fails, so
        removing coverage is a step that has to be undone deliberately rather
        than one that silently disarms the prompt.
        """
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        edited = source.replace(
            "parallel calls only when every call is explicitly read-only.",
            "parallel calls only when every call is explicitly read-only. Be brief.",
            1,
        )
        saved_cov = pin.ARM_COVERAGE.pop("DAILY_SYSTEM")
        saved_pin = pin.PINNED_DIGESTS["DAILY_SYSTEM"]
        try:
            new_value = next(
                node.value.value
                for node in ast.parse(edited).body
                if isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "DAILY_SYSTEM"
            )
            pin.PINNED_DIGESTS["DAILY_SYSTEM"] = pin.digest_of(new_value)
            doc = _pin_check_on(tmp_path, edited)
            row = next(r for r in doc["constants"] if r["constant"] == "DAILY_SYSTEM")
            assert doc["ok"] is False, (
                "removing a constant from ARM_COVERAGE and moving its digest "
                "disarmed the gate entirely: state="
                f"{row['state']!r}, findings={[f['kind'] for f in doc['findings']]}"
            )
            assert row["state"] == "unclassified"
        finally:
            pin.ARM_COVERAGE["DAILY_SYSTEM"] = saved_cov
            pin.PINNED_DIGESTS["DAILY_SYSTEM"] = saved_pin

    def test_an_unclassified_constant_is_reported_as_a_finding(self, tmp_path):
        """The control: the unclassified state is reachable and is a finding."""
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        saved = pin.ARM_COVERAGE.pop("PLANNER_USER")
        try:
            doc = _pin_check_on(tmp_path, source)
            row = next(r for r in doc["constants"] if r["constant"] == "PLANNER_USER")
            assert row["state"] == "unclassified"
            assert doc["ok"] is False
            assert any(f["kind"] == "unclassified_prompt" for f in doc["findings"])
        finally:
            pin.ARM_COVERAGE["PLANNER_USER"] = saved

    def test_emptying_the_pins_is_refused(self):
        saved = dict(pin.PINNED_DIGESTS)
        pin.PINNED_DIGESTS.clear()
        try:
            doc = pin.check()
            kinds = {f["kind"] for f in doc["findings"]}
            assert "baseline_emptied" in kinds
            assert doc["ok"] is False
        finally:
            pin.PINNED_DIGESTS.update(saved)

    def test_moving_the_prompt_to_another_module_is_refused(self, tmp_path):
        """THE MOVE-THE-STRING BYPASS. Renaming or relocating is a bigger diff
        than the edit being policed, so it fails loudly rather than silently
        dropping out of the gate's reach."""
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        start = source.index("DAILY_SYSTEM = ")
        end = source.index('"""', start + 20) + 3
        moved = source[:start] + source[end:]
        doc = _pin_check_on(tmp_path, moved)
        assert doc["ok"] is False
        assert doc["verdict"] == "PROMPT_UNREADABLE"
        kinds = {f["kind"] for f in doc["findings"]}
        assert "prompt_unreadable" in kinds

    def test_constructing_the_prompt_at_runtime_is_refused(self, tmp_path):
        """THE RUNTIME-CONSTRUCTION BYPASS. A prompt assembled by a function is
        not an `ast.Constant`, so the scanner cannot see it - and a scanner that
        reports clean on a prompt it cannot read is the defect this class of
        bypass exists to exploit."""
        source = pin.PROMPTS_PATH.read_text(encoding="utf-8-sig")
        start = source.index('DAILY_SYSTEM = """')
        end = source.index('"""', start + 20) + 3
        original = source[start:end]
        mutated = original.replace('DAILY_SYSTEM = """', '_PROSE = """', 1)
        mutated += (
            '\n\ndef _assemble() -> str:\n'
            '    """Build the prompt at import time."""\n'
            '    return _PROSE + " Always prefer fewer tools."\n'
            '\n\nDAILY_SYSTEM = _assemble()\n'
        )
        doc = _pin_check_on(tmp_path, source[:start] + mutated + source[end:])
        assert doc["ok"] is False
        kinds = {f["kind"] for f in doc["findings"]}
        assert "prompt_unreadable" in kinds

    def test_a_dangling_coverage_entry_fires_with_no_digest_change(self):
        """A coverage set pointing at a renamed arm has stopped covering."""
        saved = pin.ARM_COVERAGE["PLANNER_SYSTEM"]
        pin.ARM_COVERAGE["PLANNER_SYSTEM"] = ("no_such_arm_exists",)
        try:
            doc = pin.check()
            kinds = {f["kind"] for f in doc["findings"]}
            assert "dangling_arm_coverage" in kinds
        finally:
            pin.ARM_COVERAGE["PLANNER_SYSTEM"] = saved


class TestTheExemptionCountIsPinned:
    """A growing exemption list is the pin decaying, and nothing showed it.

    The count is asserted here as well as bounded in the module, because a bound
    nobody tests is a comment.
    """

    def test_the_exemption_count_is_published_on_every_run(self):
        doc = pin.check()
        assert "exemption_count" in doc
        assert "max_exemptions" in doc
        assert doc["exemption_count"] == len(pin.EXEMPTIONS)

    def test_the_exemption_count_matches_the_pinned_maximum_today(self):
        """This is the assertion that makes GROWTH visible.

        Raising it is legitimate and has to be a deliberate edit here, so it
        appears in a diff on the rule rather than as a longer list nobody
        reviewed.
        """
        assert len(pin.EXEMPTIONS) == pin.MAX_EXEMPTIONS, (
            f"the exemption list holds {len(pin.EXEMPTIONS)} entries against a "
            f"pinned maximum of {pin.MAX_EXEMPTIONS}. If the growth is intended, "
            "raise MAX_EXEMPTIONS in evals/prompt_pin.py deliberately."
        )

    def test_every_exemption_names_its_gap_rather_than_hiding_it(self):
        for name, entry in pin.EXEMPTIONS.items():
            assert entry.get("reason"), name
            assert entry.get("owner"), name
            assert "no eval arm" in entry["reason"].lower() or "coverage gap" in entry[
                "reason"
            ].lower(), (
                f"exemption {name} does not say it is a coverage gap; an "
                "exemption that reads as a decision is how the list decays"
            )

    def test_exceeding_the_maximum_is_a_finding(self):
        pin.EXEMPTIONS["PROBE_CONSTANT"] = {
            "reason": "audit probe",
            "owner": "audit",
        }
        try:
            findings = pin._wildcard_exemption_findings()
            assert any(
                f["kind"] == "invalid_exemption" and "over the pinned maximum"
                in f["detail"]
                for f in findings
            )
        finally:
            pin.EXEMPTIONS.pop("PROBE_CONSTANT")

    def test_the_coverage_gap_count_is_published_and_non_empty(self):
        """The seven control-only prompts are a REAL hole in the matrix and the
        gate is the only thing keeping them named."""
        doc = pin.check()
        assert "coverage_gaps" in doc
        assert doc["coverage_gap_count"] > 0
        assert doc["coverage_gap_count"] == len(doc["coverage_gaps"])


class TestThePinIsGreenOnThisTree:
    """The gate has to be able to pass, or it is not a gate."""

    def test_this_tree_is_armed(self):
        doc = pin.check()
        assert doc["ok"] is True, [f["detail"] for f in doc["findings"]]
        assert doc["verdict"] == "PROMPTS_ARMED"

    def test_every_agent_facing_constant_is_classified_or_exempt(self):
        doc = pin.check()
        states = {r["constant"]: r["state"] for r in doc["constants"]}
        for name in pin.AGENT_FACING:
            assert name in states, f"{name} was not classified at all"
            assert states[name] in {
                "unchanged", "armed_edit", "exempt_changed", "unclassified",
            } or name in pin.EXEMPTIONS, (
                f"{name} is in an unexpected state: {states[name]!r}"
            )

    def test_the_pin_reads_the_file_it_claims_to_read(self):
        assert pin.PROMPTS_PATH.name == "prompts.py"
        assert pin.PROMPTS_PATH.parent.name == "harness"
        assert pin.PROMPTS_PATH.exists()

    def test_the_digest_is_over_the_value_not_the_source_slice(self):
        """A docstring edit must not move it; a one-character prompt edit must."""
        assert pin.digest_of("abc") == pin.digest_of("abc")
        assert pin.digest_of("abc") != pin.digest_of("abd")


# ======================================================================
# The six honest-absent rules.
# ======================================================================


class TestTheSixRulesExist:
    def test_there_are_exactly_six_and_they_are_the_brief_s_ones(self):
        assert len(truth.METRICS) == 6
        assert set(truth.METRICS) == set(truth.HONEST_ABSENT)

    def test_each_rule_names_the_lie_it_prevents(self):
        for metric, rule in truth.HONEST_ABSENT.items():
            assert rule["lie_prevented"], metric
            assert rule["distinguishes"], metric
            assert rule["implemented_by"], metric


class TestAbsentIsNeverZero:
    """Every rule, both directions. The control arm is the measured zero."""

    @pytest.mark.parametrize("metric", list(truth.METRICS))
    def test_absent_renders_unavailable(self, metric):
        r = truth.row(metric, None, reason="no measurement")
        assert r.available is False
        assert r.render().startswith(f"{metric}: {truth.UNAVAILABLE}")

    @pytest.mark.parametrize("metric", list(truth.METRICS))
    def test_absent_omits_the_value_key_entirely(self, metric):
        """A consumer doing `data["value"] or 0` must not be able to pass."""
        r = truth.row(metric, None)
        assert "value" not in r.to_dict()

    @pytest.mark.parametrize("metric", list(truth.METRICS))
    def test_a_measured_zero_is_carried_as_zero_not_unavailable(self, metric):
        """THE CONTROL. Without this the rule is satisfiable by a constant."""
        r = truth.row(metric, 0.0, source="measured")
        assert r.available is True
        assert r.value == 0.0
        assert "value" in r.to_dict()
        assert truth.UNAVAILABLE not in r.render()

    def test_a_bool_is_refused_on_a_numeric_axis(self):
        r = truth.row("prompt_tokens", False)
        assert r.available is False
        assert "bool" in r.reason

    def test_a_missing_value_carries_a_reason(self):
        r = truth.row("cache_hit_rate", None)
        assert r.reason
        assert r.to_dict()["reported_as"] == truth.UNAVAILABLE


class TestReductionRequiresABaseline:
    """THE PRIORITY RULE. 'We cut the prompt 40%' needs two numbers."""

    def test_a_reduction_with_a_measured_baseline_is_computable(self):
        """POSITIVE means the prompt got SMALLER, which is the win direction.

        The sign is asserted rather than left implicit because a reduction that
        reports `-71%` when the prompt actually shrank by 71% is a report whose
        reader has to work out the sign convention on every single number.
        """
        r = truth.reduction_row(
            3981, 1128,
            provenance={"measured_at": "P3b-W2", "source": "prompt_budget_measure"},
        )
        assert r.computable is True
        assert r.to_dict()["reduction_pct"] == pytest.approx(71.67, abs=0.01)
        assert "71.7%" in r.render()

    def test_a_reduction_with_no_baseline_is_NOT_computable(self):
        """THE WORST LIE. An 'after' with no 'before' is a current value."""
        r = truth.reduction_row(None, 1128)
        assert r.computable is False
        doc = r.to_dict()
        assert "reduction_pct" not in doc, (
            "a percentage was published with no baseline; that is a division by "
            "an invented denominator"
        )
        assert doc["reduction_pct_is"] == truth.UNAVAILABLE
        assert "baseline" in doc["why"]

    def test_a_baseline_without_provenance_is_not_a_baseline(self):
        """A remembered constant is a rumour with a digit in it."""
        r = truth.reduction_row(3981, 1128, provenance={})
        assert r.computable is False
        assert "provenance" in r.to_dict()["why"]

    def test_provenance_missing_one_required_key_is_refused(self):
        r = truth.reduction_row(
            3981, 1128, provenance={"measured_at": "P3b-W2"}
        )
        assert r.computable is False
        assert "source" in r.to_dict()["why"]

    def test_an_after_with_no_measurement_is_refused(self):
        r = truth.reduction_row(
            3981, None, provenance={"measured_at": "x", "source": "y"}
        )
        assert r.computable is False
        assert "current" in r.to_dict()["why"]

    def test_the_render_refuses_too(self):
        r = truth.reduction_row(None, 1128)
        assert truth.UNAVAILABLE in r.render()
        assert "%" not in r.render()

    def test_a_zero_baseline_does_not_divide_by_zero(self):
        r = truth.reduction_row(
            0, 0, provenance={"measured_at": "x", "source": "y"}
        )
        assert r.computable is True
        assert r.to_dict()["reduction_pct"] == 0.0

    def test_a_bool_baseline_is_refused(self):
        r = truth.reduction_row(
            True, 100, provenance={"measured_at": "x", "source": "y"}
        )
        assert r.computable is False


class TestSummariseDoesNotThinTheDenominator:
    def test_a_sum_reports_how_many_rows_it_skipped(self):
        rows = [
            truth.row("prompt_tokens", 100.0).to_dict(),
            truth.row("cache_hit_rate", None).to_dict(),
        ]
        out = truth.summarise(rows)
        assert out["n"] == 2
        assert out["n_available"] == 1
        assert out["n_absent"] == 1
        assert out["total_is_over_a_thinning_denominator"] is True
        assert out["absent_metrics"] == ["cache_hit_rate"]

    def test_a_sum_over_no_available_rows_is_none_not_zero(self):
        rows = [truth.row("prompt_tokens", None).to_dict()]
        out = truth.summarise(rows)
        assert out["total_over_available"] is None


class TestT4HonoursTheAbsentPath:
    """T4 owns the render. This verifies it by CALLING it, not by editing it."""

    def test_the_verification_runs_and_passes(self):
        doc = truth.verify_renderers()
        assert doc["ok"] is True, doc["findings"]

    def test_every_metric_is_checked_in_both_directions(self):
        doc = truth.verify_renderers()
        ours = [e for e in doc["checked"] if e["renderer"] == "shared.p3b_truth.row"]
        assert len(ours) == len(truth.METRICS)
        for entry in ours:
            assert entry["absent_renders_unavailable"] is True
            assert entry["absent_value_is_none"] is True
            assert entry["absent_doc_omits_value"] is True
            assert entry["present_carries_zero"] is True
            assert entry["present_doc_carries_value"] is True

    def test_the_renderers_actually_import(self):
        """A missing renderer must be reported, never crashed on."""
        doc = truth.verify_renderers()
        theirs = [e for e in doc["checked"] if e["renderer"] != "shared.p3b_truth.row"]
        assert theirs
        for entry in theirs:
            assert entry["imported"] is True, entry

    def test_the_unavailable_literal_is_shared_not_duplicated(self):
        """Two literals that drift is how a UI stops matching its own gate."""
        import cli.context_budget as cb

        assert cb.UNAVAILABLE == truth.UNAVAILABLE

    def test_t4_exposes_the_gates_this_verification_calls(self):
        import cli.cache_cost
        import cli.capability_view
        import cli.skill_view

        for gate in (
            cli.cache_cost.assert_absent_is_never_zero,
            cli.capability_view.assert_no_false_capability,
            cli.skill_view.assert_no_skill_claims_a_body_it_did_not_load,
        ):
            assert callable(gate)


class TestTheGoNoGoMetricsDriveEveryDeferralState:
    """Watch-list 3, closed: all THREE states, driven in one place.

    Phase 3b's deferral has three states and a suite can pass without ever
    entering two of them:

    * **offered** - the schema is in the request; no discovery needed;
    * **deferred and recovered** - absent from the request, found by search;
    * **deferred and lost** - absent from the request and never found.

    Wave 1 covered offered and lost but not recovered; this wave's own audit
    found that the *deferred* fixture in ``tests/test_p3b_tools_and_prompts.py``
    never deferred its needed tool at all, so its assertions measured the
    offered state on a query that deferred nothing else worth deferring.

    All three are driven here against the same deferred set, so "this suite
    exercises deferral" is a measurement rather than an inference from test
    names.
    """

    @staticmethod
    def _case() -> dict:
        derived = ta.derive_a_deferring_case()
        assert derived is not None, "no deferring case exists to drive"
        return derived

    def test_state_one_offered(self):
        """The schema is in the request and no search happened."""
        derived = self._case()
        _, schemas, candidates, descriptions, _ = ta._with_measured_tool(
            derived["scorer"], derivable=True
        )
        result = ta.measure_arm(
            "eager",
            query=derived["query"],
            schemas=schemas,
            candidates=candidates,
            descriptions=descriptions,
            needed=tuple(derived["deferred"]),
            discoverable=tuple(derived["deferred"]),
            scorer=derived["scorer"],
        )
        routes = set(result.metrics["reach_routes"].values())
        assert "schema_present" in routes, (
            f"the eager arm did not offer any of the deferred set by schema: "
            f"{result.metrics['reach_routes']}"
        )
        assert result.metrics["model_searches"] == 0

    def test_state_two_deferred_and_recovered(self):
        """Absent from the request, recovered by an actual search."""
        derived = self._case()
        _, schemas, candidates, descriptions, _ = ta._with_measured_tool(
            derived["scorer"], derivable=True
        )
        needed = tuple(derived["deferred"])
        result = ta.measure_arm(
            "deferred",
            query=derived["query"],
            schemas=schemas,
            candidates=candidates,
            descriptions=descriptions,
            needed=needed,
            discoverable=needed,
            scorer=derived["scorer"],
        )
        routes = set(result.metrics["reach_routes"].values())
        assert "discovered_by_search" in routes, (
            f"nothing was recovered by search: {result.metrics['reach_routes']}"
        )
        assert result.metrics["schema_count_deferred"] > 0
        assert result.metrics["discoverability_failure_rate"] == 0.0
        # The recovery is PAID FOR, and the count is the price.
        assert result.metrics["extra_round_trips_for_discovery"] == len(needed), (
            "one search per missing tool is the modelled behaviour; a different "
            "count means the driver changed and the round-trip cost this phase "
            "reports is no longer the one being paid"
        )

    def test_state_three_deferred_and_lost(self):
        """Absent from the request, and the model never looked."""
        derived = self._case()
        _, schemas, candidates, descriptions, _ = ta._with_measured_tool(
            derived["scorer"], derivable=True
        )
        needed = tuple(derived["deferred"])
        result = ta.measure_arm(
            "deferred",
            query=derived["query"],
            schemas=schemas,
            candidates=candidates,
            descriptions=descriptions,
            needed=needed,
            discoverable=[],
            scorer=derived["scorer"],
        )
        assert result.metrics["discoverability_failure_rate"] == 1.0, (
            "a deferred tool the model never searched for is unreachable, and "
            "the receipt must say so rather than reporting a clean rate"
        )
        assert result.metrics["schema_count_deferred"] > 0

    def test_the_three_states_are_mutually_distinguishable(self):
        """A suite that cannot tell the states apart has not covered them.

        Asserted across the three runs above rather than per-test, because the
        failure mode this guards against is two states producing IDENTICAL
        numbers - which every individual assertion would accept.
        """
        derived = self._case()
        _, schemas, candidates, descriptions, _ = ta._with_measured_tool(
            derived["scorer"], derivable=True
        )
        needed = tuple(derived["deferred"])
        common = dict(
            query=derived["query"],
            schemas=schemas,
            candidates=candidates,
            descriptions=descriptions,
            needed=needed,
            scorer=derived["scorer"],
        )
        offered = ta.measure_arm("eager", discoverable=needed, **common)
        recovered = ta.measure_arm("deferred", discoverable=needed, **common)
        lost = ta.measure_arm("deferred", discoverable=[], **common)

        signatures = {
            "offered": (
                offered.metrics["schema_count_offered"],
                offered.metrics["model_searches"],
                offered.metrics["discoverability_failure_rate"],
            ),
            "recovered": (
                recovered.metrics["schema_count_offered"],
                recovered.metrics["model_searches"],
                recovered.metrics["discoverability_failure_rate"],
            ),
            "lost": (
                lost.metrics["schema_count_offered"],
                lost.metrics["model_searches"],
                lost.metrics["discoverability_failure_rate"],
            ),
        }
        assert len(set(signatures.values())) == 3, (
            f"two deferral states produced identical metrics: {signatures}"
        )
        assert signatures["offered"][0] > signatures["recovered"][0], (
            "the offered arm must carry MORE schemas than the deferred arm"
        )


class TestTheSixRulesSurviveToTheUi:
    """The rule has to hold where a reader actually sees it."""

    def test_the_render_of_an_absent_row_names_the_missing_thing(self):
        r = truth.row("skill_load_rate", None, reason="no skill scan ran")
        rendered = r.render()
        assert "no skill scan ran" in rendered
        assert truth.UNAVAILABLE in rendered

    def test_a_wire_projection_round_trips_through_json(self):
        r = truth.row("deferred_count", None, reason="deferral disabled")
        doc = json.loads(json.dumps(r.to_dict()))
        assert doc["available"] is False
        assert "value" not in doc

    def test_the_reduction_row_projection_round_trips(self):
        r = truth.reduction_row(None, 1128)
        doc = json.loads(json.dumps(r.to_dict()))
        assert doc["reduction_computable"] is False
        assert "reduction_pct" not in doc
