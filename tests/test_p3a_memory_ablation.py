"""P3a: the memory ablation arm -- the exit gate's evidence.

Three things are pinned here, and the third is the one that matters most:

1. recall is **MEASURED** against a labelled query set, not asserted;
2. each arm **isolates** the axis it names;
3. **no arm key is in ``harness/config.py::DEFAULTS``**.

Point 3 is the anti-rot gate. A truthy value in ``DEFAULTS`` is merged into
every task and every eval arm, so one entry would silently switch the whole
project onto the new memory architecture -- the specific failure the phase
forbids. ``evals/run.py``'s own arm table cannot hold these keys at all, because
``tests/test_evals_tasks.py`` requires every arm key to exist in ``DEFAULTS``
and ``DEFAULTS`` lives in ``harness/``, which this terminal may not edit. That
constraint is why this suite exists as its own module rather than as an entry in
``ARMS``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.memory_ablation import (
    ARM_KEYS,
    LABELLED_QUERIES,
    MEMORY_ARMS,
    arm_config,
    memory_ablation_report,
    memory_enabled,
    render,
    unusable_value,
)

FEATURE_KEYS = ("memory_recall_enabled", "memory_pipeline_enabled", "memory_ranking_enabled")


class TestRecallIsMeasured:
    def test_the_baseline_arm_reports_a_measured_recall_at_k(self):
        report = memory_ablation_report(arms=["memory_on"], k=5)
        assert report["verdict"] == "CLEAN", report["errors"]
        arm = report["arms"]["memory_on"]
        recall = arm["metrics"]["recall_at_k"]
        assert recall["measured"] is True, (
            "recall@k is the phase's headline claim; None-with-a-reason is "
            "not-implemented, never a pass"
        )
        assert recall["recall_at_k"] is not None
        assert 0.0 <= recall["recall_at_k"] <= 1.0
        assert recall["queries_measured"] == len(LABELLED_QUERIES)
        assert recall["queries_total"] == len(LABELLED_QUERIES)

    def test_recall_is_scored_in_or_mode_and_says_why(self):
        """An AND search over paraphrase queries measures the labelling."""
        report = memory_ablation_report(arms=["memory_on"])
        assert report["match_mode"] == "or"
        assert "paraphrase" in report["match_mode_reason"]

    def test_every_query_is_scored_through_a_named_engine(self):
        report = memory_ablation_report(arms=["memory_on"])
        for query in report["arms"]["memory_on"]["metrics"]["recall_at_k"]["queries"]:
            assert query["engine"], f"query {query['query']!r} has no engine"
            assert query["search_available"] is True

    def test_the_recall_arm_measures_recall_at_k_actually_at_k(self):
        report = memory_ablation_report(arms=["memory_on"], k=2)
        recall = report["arms"]["memory_on"]["metrics"]["recall_at_k"]
        assert recall["k"] == 2
        for query in recall["queries"]:
            assert query["k"] == 2

    def test_the_baseline_recall_is_reported_at_the_top_level(self):
        report = memory_ablation_report(arms=["memory_on"])
        assert report["baseline_arm"] == "memory_on"
        assert report["baseline_recall_at_k"] is not None
        assert report["deterministic"] is True
        assert report["model_calls"] == 0


class TestHonestAbsent:
    def test_the_recall_off_arm_reports_NOT_MEASURED_not_zero(self):
        report = memory_ablation_report(arms=["no_recall"])
        recall = report["arms"]["no_recall"]["metrics"]["recall_at_k"]
        assert recall["measured"] is False
        assert recall["recall_at_k"] is None, (
            "a recall of zero would say 'we looked and found nothing' when "
            "nothing was injected"
        )
        assert "not a recall of zero" in recall["reason"]
        assert report["arms"]["no_recall"]["vacuous"]["recall"] is True

    def test_the_citation_rate_is_vacuous_when_nothing_was_injected(self):
        report = memory_ablation_report(arms=["no_recall"])
        citation = report["arms"]["no_recall"]["metrics"]["citation_rate"]
        assert citation["vacuous"] is True
        assert citation["citation_rate"] is None
        assert "not a rate of zero" in citation["reason"]

    def test_the_baseline_arm_measures_every_metric_at_least_once(self):
        report = memory_ablation_report(arms=["memory_on"])
        arm = report["arms"]["memory_on"]
        metrics = arm["metrics"]
        assert metrics["recall_at_k"]["recall_at_k"] is not None
        assert metrics["citation_rate"]["citation_rate"] is not None
        assert metrics["citation_rate"]["vacuous"] is False
        assert metrics["stale_memory_rate"]["stale_memory_rate"] is not None
        assert metrics["decision_recurrence_rate"]["decision_recurrence_rate"] is not None
        assert all(value is False for value in arm["vacuous"].values())

    def test_the_report_declares_what_it_does_not_claim(self):
        report = memory_ablation_report(arms=["memory_on"])
        claims = " ".join(report["claims"])
        assert "not task success" in claims
        assert "no model call" in claims.lower()
        assert "injected" in claims, (
            "the citation set is synthetic and the report must say so, or a "
            "citation rate reads as observed"
        )
        assert report["docker"] is False
        assert report["network"] is False

    def test_recurrence_is_partial_because_the_corpus_is(self):
        """1.0 and 0.0 both measure nothing; the seeded run re-takes one of two."""
        report = memory_ablation_report(arms=["memory_on"])
        recurrence = report["arms"]["memory_on"]["metrics"]["decision_recurrence_rate"]
        assert recurrence["measured"] is True
        assert 0.0 < recurrence["decision_recurrence_rate"] < 1.0
        assert recurrence["prior"] == 2
        assert recurrence["current"] == 2
        assert recurrence["recurrent"] == 1


class TestArmsIsolateTheirAxis:
    def test_every_off_arm_differs_in_exactly_its_axis(self):
        """An arm that switches two axes measures their union, not either."""
        baseline = MEMORY_ARMS["memory_on"]
        for name, overrides in MEMORY_ARMS.items():
            if name == "memory_on":
                continue
            differing = [
                key for key in ARM_KEYS if bool(overrides.get(key)) != bool(baseline.get(key))
            ]
            assert differing, f"arm {name!r} is identical to the baseline"
            if name == "pre_phase":
                assert set(differing) == set(ARM_KEYS), (
                    "the wholesale arm must switch off EVERY key; a partial one "
                    "is just a one-axis arm with a misleading name"
                )
                continue
            assert len(differing) == 1, (
                f"arm {name!r} differs in {differing}, so it does not isolate one axis"
            )

    def test_the_baseline_switches_on_every_feature_key(self):
        for key in ARM_KEYS:
            assert MEMORY_ARMS["memory_on"][key] is True, key

    def test_each_axis_has_its_own_off_arm(self):
        expected = {
            "memory_recall_enabled": "no_recall",
            "memory_ranking_enabled": "no_ranking",
            "memory_pipeline_enabled": "no_pipeline",
        }
        for key, arm in expected.items():
            assert arm in MEMORY_ARMS, f"{key} has no isolating arm"
            assert MEMORY_ARMS[arm][key] is False
            for other in ARM_KEYS:
                if other != key:
                    assert MEMORY_ARMS[arm][other] is True, (
                        f"{arm!r} must leave {other!r} ON or it isolates nothing"
                    )

    def test_no_recall_is_off_but_still_ranking_and_recall_are_distinguishable(self):
        report = memory_ablation_report(arms=["memory_on", "no_recall", "no_ranking"])
        on = report["arms"]["memory_on"]
        off = report["arms"]["no_recall"]
        norank = report["arms"]["no_ranking"]
        # recall OFF: nothing injected at all
        assert off["injected"] == 0
        assert on["injected"] > 0
        # ranking OFF: memory IS injected, so recall is still measurable -- this
        # is the arm that separates the two mechanisms.
        assert norank["injected"] > 0
        assert norank["metrics"]["recall_at_k"]["measured"] is True
        assert norank["ranking"]["usage_applied"] in (True, False)

    def test_an_unknown_arm_is_refused(self):
        with pytest.raises(KeyError):
            arm_config("not-an-arm")
        with pytest.raises(KeyError):
            memory_ablation_report(arms=["not-an-arm"])

    def test_arm_config_returns_a_copy(self):
        first = arm_config("memory_on")
        first["memory_recall_enabled"] = False
        assert MEMORY_ARMS["memory_on"]["memory_recall_enabled"] is True


class TestConfigDiscipline:
    def test_no_memory_arm_key_is_in_harness_defaults(self):
        """The anti-rot gate.

        A truthy value in ``DEFAULTS`` is merged into every task and every eval
        arm, so one entry would switch every run in the project onto this
        architecture at once. These keys are read by key presence instead.
        """
        from harness.config import DEFAULTS

        present = [key for key in ARM_KEYS if key in DEFAULTS]
        assert present == [], (
            f"{present} are in harness/config.py::DEFAULTS; a value there is "
            "merged into every task and every eval arm"
        )

    def test_the_arm_keys_are_exactly_the_three_feature_keys(self):
        assert set(ARM_KEYS) == set(FEATURE_KEYS)

    def test_the_report_publishes_the_defaults_absence(self):
        report = memory_ablation_report(arms=["memory_on"])
        assert report["keys_in_harness_defaults"] == {key: False for key in ARM_KEYS}

    @pytest.mark.parametrize("value", [True, "1", "true", "yes", "on", "enabled"])
    def test_a_truthy_value_enables(self, value):
        assert memory_enabled({"memory_recall_enabled": value}, "memory_recall_enabled")

    @pytest.mark.parametrize(
        "value", [False, "0", "false", "no", "off", "", None, "maybe", "  ", 0]
    )
    def test_a_falsey_or_absent_or_typo_value_does_not_enable(self, value):
        assert not memory_enabled(
            {"memory_recall_enabled": value}, "memory_recall_enabled"
        )

    def test_an_absent_key_is_off(self):
        assert memory_enabled({}, "memory_recall_enabled") is False
        assert memory_enabled(None, "memory_recall_enabled") is False

    def test_a_typo_is_reported_rather_than_silently_off(self):
        config = {"memory_recall_enabeld": True}
        assert memory_enabled(config, "memory_recall_enabled") is False
        assert unusable_value(config, "memory_recall_enabled") is False
        assert unusable_value({"memory_recall_enabled": "maybe"}, "memory_recall_enabled")

    def test_the_string_false_does_not_enable(self):
        """A settings file with "False" is truthy in Python; it must mean OFF."""
        assert memory_enabled({"memory_ranking_enabled": "False"}, "memory_ranking_enabled") is False


class TestTheRenderedTable:
    def test_an_unmeasured_metric_renders_as_not_measured(self):
        report = memory_ablation_report(arms=["memory_on", "no_recall"])
        text = render(report)
        assert "n/m" in text
        assert "n/m = NOT MEASURED" in text
        assert "not a zero" in text
        assert "memory_on" in text and "no_recall" in text

    def test_the_render_names_the_search_engine(self):
        report = memory_ablation_report(arms=["memory_on"])
        assert "search engine" in render(report)
        assert "fts5" in render(report) or "unavailable" in render(report)

    def test_the_render_carries_the_does_not_claim_lines(self):
        report = memory_ablation_report(arms=["memory_on"])
        text = render(report)
        assert text.count("DOES NOT CLAIM") >= 3

    def test_the_report_is_json_serialisable(self):
        report = memory_ablation_report(arms=["memory_on"])
        assert json.loads(json.dumps(report, default=str))["verdict"] == "CLEAN"


class TestIsolationBetweenArms:
    def test_each_arm_gets_its_own_store(self, tmp_path: Path):
        """A shared store would let one arm's citations count for another's recall."""
        report = memory_ablation_report(
            arms=["memory_on", "no_ranking"], workdir=tmp_path
        )
        on = report["arms"]["memory_on"]
        off = report["arms"]["no_ranking"]
        assert on["store_rows"] == off["store_rows"], (
            "both arms seed the same corpus, so a row-count difference would "
            "mean the stores are shared"
        )
        roots = {path.parent.name for path in tmp_path.rglob("*.db")}
        assert {"memory_on", "no_ranking"} <= roots, (
            f"each arm must own its own store directory; found {sorted(roots)}"
        )

    def test_the_workdir_is_removed_when_temporary(self, tmp_path: Path, monkeypatch):
        import evals.memory_ablation as module

        target = tmp_path / "auto-removed"
        original = module.tempfile.mkdtemp
        monkeypatch.setattr(module.tempfile, "mkdtemp", lambda *a, **k: str(target))
        del original
        report = memory_ablation_report(arms=["memory_on"])
        assert report["verdict"] == "CLEAN"
        assert not target.exists(), "a temporary workdir must not be left behind"


class TestNonVacuity:
    def test_the_labelled_query_set_is_non_trivial(self):
        assert len(LABELLED_QUERIES) >= 5
        for query in LABELLED_QUERIES:
            assert query["gold_ids"], query
            assert query["note"], query

    def test_the_seed_corpus_covers_every_labelled_gold_id(self):
        from evals.memory_ablation import SEED_ROWS

        seeded = {row["doc_id"] for row in SEED_ROWS}
        for query in LABELLED_QUERIES:
            for gold in query["gold_ids"]:
                assert gold in seeded, f"{gold!r} is labelled but never seeded"

    def test_the_corpus_has_distractor_rows(self):
        """A corpus with no distractors makes recall trivially 1.0."""
        from evals.memory_ablation import SEED_ROWS

        labelled = {gold for query in LABELLED_QUERIES for gold in query["gold_ids"]}
        distractors = [row for row in SEED_ROWS if row["doc_id"] not in labelled]
        assert len(distractors) >= 2, (
            "recall@k over a corpus with no distractors is not a measurement"
        )

    def test_the_corpus_contains_an_untrusted_row(self):
        from evals.memory_ablation import SEED_ROWS

        assert any(row.get("untrusted_source") for row in SEED_ROWS), (
            "trust tiering must be exercised by the arm, not only by unit tests"
        )

    def test_the_corpus_contains_a_user_stated_row(self):
        from evals.memory_ablation import SEED_ROWS

        assert any(row["source"] == "manual" for row in SEED_ROWS)
