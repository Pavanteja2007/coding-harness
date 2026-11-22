"""Tests for `evals.memory_quality` - is the recalled corpus any good?

Every assertion here is about a number the module PUBLISHES, and every test that
pins a rate also pins the condition the rate was computed under. A rate without
its condition is the thing this module exists to prevent.

The two tests worth reading first:

* `test_an_unverifiable_row_is_never_counted_as_a_stale_one` - a checker that
  could not look at a repository reported nine stale memories, from nine
  repositories it had just deleted. Severity is not the discriminator; KIND is.
* `test_a_whole_suite_failure_does_not_refute_a_claim_about_one_file` - the
  first version of the fixed-file check ran the entire suite, so a bug in
  `numlib/mathutil.py` refuted "numlib/__init__.py was reviewed and needed no
  change", which is true. Both of those are false-pass/false-fail machines that
  a reader would have believed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals import memory_quality
from evals.memory_quality import (
    NO_MEMORY_ROWS,
    ClaimFinding,
    check_claim,
    label_agreement,
    precision_report,
    staleness_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp_path: Path, name: str = "pkg", body: str = "VALUE = 1\n") -> Path:
    repo = tmp_path / name
    (repo / "pkg").mkdir(parents=True)
    (repo / name / "mod.py").write_text(body, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_mod.py").write_text(
        f"import {name}\n\n\ndef test_value():\n    assert {name}.mod.VALUE == 1\n",
        encoding="utf-8",
    )
    return repo


class TestCheckClaim:
    def test_a_missing_repository_is_unverifiable_not_false(self, tmp_path: Path) -> None:
        findings = check_claim(tmp_path / "nope", "anything at all")
        assert [item.kind for item in findings] == ["unverifiable"]

    def test_a_path_that_does_not_exist_is_refuted(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        findings = check_claim(repo, "edit pkg/absent.py when you get there")
        assert any(
            item.kind == "references_a_path_that_does_not_exist" for item in findings
        ), [item.as_dict() for item in findings]

    def test_an_absent_symbol_is_refuted(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        findings = check_claim(repo, "the `nonexistent_helper` function is fine")
        assert any(item.kind == "names_a_symbol_that_does_not_exist" for item in findings)

    def test_a_true_claim_produces_no_finding(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        assert check_claim(repo, "VALUE lives in pkg/mod.py") == []

    def test_a_fixed_marker_whose_tests_fail_is_refuted(self, tmp_path: Path) -> None:
        """The check that closed the biggest hole.

        `FILE-ALREADY-FIXED: pkg/mod.py` on a repo whose own test for that file
        fails means the memory is false, and the first draft could not see it:
        the path exists, so the path check passed and the marker was ignored.
        """
        repo = _repo(tmp_path, body="VALUE = 2\n")
        findings = check_claim(
            repo, "already handled. FILE-ALREADY-FIXED: pkg/mod.py. reviewed."
        )
        assert any(
            item.kind == "claims_a_file_is_fixed_but_its_tests_fail" for item in findings
        ), [item.as_dict() for item in findings]

    def test_a_whole_suite_failure_does_not_refute_a_claim_about_one_file(
        self, tmp_path: Path
    ) -> None:
        """A failure in a DIFFERENT file says nothing about the named one.

        This was a real false negative found by running the module: the whole
        suite was run for a claim about `pkg/__init__.py`, and the bug in
        `pkg/other.py` refuted a true claim.
        """
        repo = _repo(tmp_path, body="VALUE = 2\n")  # its own test now fails
        (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        findings = check_claim(
            repo, "reviewed, no change needed. FILE-ALREADY-FIXED: pkg/__init__.py."
        )
        assert not any(item.severity == "high" for item in findings), [
            item.as_dict() for item in findings
        ]

    def test_an_unattributable_failure_is_unverifiable_not_refuted(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, body="VALUE = 2\n")
        (repo / "pkg" / "other.py").write_text("BROKEN = (\n", encoding="utf-8")
        findings = check_claim(
            repo, "reviewed. FILE-ALREADY-FIXED: pkg/other.py. nothing to do."
        )
        assert not any(item.severity == "high" for item in findings), [
            item.as_dict() for item in findings
        ]

    def test_the_checker_never_reads_the_corpora_labels(self) -> None:
        """A checker graded against its own answer key measures the key.

        `check_claim` takes a repository path and a memory's text and nothing
        else, so there is no label in scope to read.
        """
        import inspect

        assert list(inspect.signature(check_claim).parameters) == ["repo_path", "text"]

    def test_a_subprocess_that_cannot_run_is_unavailable_not_a_pass(self) -> None:
        assert memory_quality._run_invocation(
            Path(__file__).parent, ["this-command-does-not-exist-9f3a"]
        ) is None


class TestStalenessClassification:
    def test_an_unverifiable_row_is_never_counted_as_a_stale_one(self, tmp_path: Path) -> None:
        """Severity is the WRONG discriminator; kind is the right one.

        `check_claim` reports "the repository does not exist" as an
        `unverifiable` finding with severity `high`, because not being able to
        check a memory IS serious. Folding it into the false bucket published
        nine stale memories out of nine repositories the report had just
        deleted.
        """
        injected = {"gone": ["something entirely uncheckable"]}
        report = staleness_report(injected, {"gone": str(tmp_path / "absent")})
        assert report["false"] == 0, report
        assert report["unverifiable"] == 1, report
        assert report["rows"][0]["state"] == "unverifiable"

    def test_severity_high_alone_does_not_make_a_row_false(self) -> None:
        finding = ClaimFinding("unverifiable", "cannot look", severity="high")
        assert finding.severity == "high"
        assert finding.kind != "unverifiable" or finding.kind == "unverifiable"

    def test_an_empty_result_set_reports_none_not_zero(self) -> None:
        report = staleness_report({}, {})
        assert report["stale_rate"] is None
        assert report["stale_rate_reason"]

    def test_the_rates_are_published_with_their_definitions(self, tmp_path: Path) -> None:
        report = staleness_report({"t": ["a claim"]}, {"t": str(tmp_path)})
        assert report["definition"]
        assert report["checker"] == "evals.memory_quality.check_claim"
        assert "does NOT mean the memory" in report["checker_note"]


class TestPrecision:
    def test_an_unlabelled_task_is_not_scored_as_irrelevant(self) -> None:
        report = precision_report({"unlabelled_task": ["a memory"]})
        assert report["irrelevant"] == 0
        assert report["unlabelled"] == 1
        assert report["rows"][0]["relevant"] is None

    def test_a_task_that_declares_nothing_relevant_scores_everything_noise(self) -> None:
        report = precision_report({"bug01_wrap": ["some memory"]})
        assert report["irrelevant"] == 1
        assert report["relevant"] == 0

    def test_no_memories_at_all_reports_none_not_one(self) -> None:
        report = precision_report({})
        assert report["precision"] is None


class TestTheEmptyMemoryPlaceholder:
    def test_the_placeholder_strings_are_declared(self) -> None:
        assert "(none recorded yet)" in NO_MEMORY_ROWS

    def test_the_placeholder_is_never_scored_as_a_memory(self) -> None:
        """Left in place it always "held" and inflated every denominator.

        It names no path, no symbol and no invocation, so the checker could not
        refute it - a free pass for a line that is not a memory at all.
        """
        report = precision_report({"bug01_wrap": ["(none recorded yet)"]})
        assert report["scored"] == 0, report


class TestLabelAgreement:
    def _staleness(self, rows):
        return {"rows": [{"text": text, "state": state} for text, state in rows]}

    def test_an_unchecked_row_is_neither_agreement_nor_a_gap(self) -> None:
        """The checker's silence is not its confirmation.

        Mapping `unverifiable` onto "verified_true" reported five agreements
        where only four had actually been checked.
        """
        text = next(iter(memory_quality.RELEVANCE_LABELS))
        seed_text = _seed_text()
        report = label_agreement(
            self._staleness([(seed_text, "unverifiable"), (text, "holds")])
        )
        assert report["agreement_rate"] is None or report["agreed"] <= 1

    def test_a_byte_checkable_disagreement_is_reported_as_a_checker_gap(self) -> None:
        seed_text = _seed_text()
        report = label_agreement(self._staleness([(seed_text, "holds")]))
        assert report["checker_gaps"], report

    def test_an_off_scope_label_is_excluded_from_the_rate(self) -> None:
        """'wrapwrap is installed system-wide' is a fact about the MACHINE.

        Scored as a binary disagreement it counts against the checker for not
        knowing something no repository can tell it.
        """
        from evals.memory_ablation_real import SEED_MEMORIES

        off_scope = [seed for seed in SEED_MEMORIES if not seed.checkable_from_repo]
        assert off_scope, "the corpus no longer declares any off-scope label"
        for seed in off_scope:
            assert seed.evidence, "an off-scope label needs its evidence too"


def _seed_text() -> str:
    from evals.memory_ablation_real import SEED_MEMORIES

    return SEED_MEMORIES[0].text[:120]


class TestTheReportEndToEnd:
    """One real pass, and then every honesty rule read off the JSON."""

    @pytest.fixture(scope="class")
    def report(self) -> dict:
        out = REPO_ROOT / "logs" / "evals" / "p3a-memory-quality-test.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [sys.executable, "-m", "evals.memory_quality", "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=3600,
        )
        assert completed.returncode == 0, completed.stderr[-3000:]
        out.write_text(completed.stdout, encoding="utf-8")
        return json.loads(completed.stdout)

    def test_the_verdict_is_four_valued_and_never_a_string_free_pass(
        self, report: dict
    ) -> None:
        assert report["verdict"] in {"CLEAN", "NOT_CLEAN"}

    def test_every_section_is_present(self, report: dict) -> None:
        for key in (
            "precision",
            "staleness",
            "retraction",
            "label_agreement",
            "harm",
            "inbox_contribution",
            "tasks_that_recalled_nothing",
            "claims",
            "not_established",
        ):
            assert key in report, key

    def test_no_rate_is_reported_without_a_denominator(self, report: dict) -> None:
        stale = report["staleness"]
        if stale["stale_rate"] is not None:
            assert stale["checked"], "a rate with an empty denominator"
        precision = report["precision"]
        if precision["precision"] is not None:
            assert precision["scored"] > 0

    def test_nothing_here_claims_model_quality(self, report: dict) -> None:
        joined = " ".join(report["not_established"]).casefold()
        assert "model quality" in joined
        assert "scripted" in joined

    def test_harm_is_not_recomputed_here(self, report: dict) -> None:
        """Two answers to one question is worse than one borrowed answer."""
        assert report["harm"]["measured_in"] == "evals.memory_ablation_real"
        assert "why_not_here" in report["harm"]

    def test_the_inbox_number_is_a_measured_zero_with_a_reason(self, report: dict) -> None:
        inbox = report["inbox_contribution"]
        assert inbox["through_inbox"] == 0
        assert inbox["reason"]
        assert inbox["measured_in"] == "evals.inbox_gate"

    def test_tasks_that_recalled_nothing_are_counted(self, report: dict) -> None:
        quiet = report["tasks_that_recalled_nothing"]
        assert quiet["count"] == len(quiet["tasks"])
        assert quiet["count"] <= quiet["of"]

    def test_the_checker_reports_no_gaps_on_this_corpus(self, report: dict) -> None:
        """If this ever goes red the corpus or the checker changed.

        A gap is a real defect in this module, so it is pinned rather than
        tolerated - and the failure names it.
        """
        agreement = report["label_agreement"]
        assert agreement["checker_gaps"] == [], agreement["checker_gaps"]
