"""CI truth: the gate that proves CI runs what it claims to run.

The required behaviours pinned here:

* the **full test suite is required by the release gate** (not merely
  collected, and not in a lane the aggregate ignores);
* every test file appears in a workflow or on an explicit allowlist;
* declared required PR checks, the security lane, and the timing-sensitive
  flake lane are all present and are actually PR-path lanes.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals import ci_truth

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_repository_ci_truth_gate_passes():
    report = ci_truth.ci_truth_report(REPO_ROOT)
    assert report["ok"] is True, report["errors"]
    assert report["verdict"] == "CI_TRUTHFUL"
    assert all(report["checks"].values())


def test_the_full_test_suite_is_required_by_the_release_gate():
    report = ci_truth.release_gate_report(REPO_ROOT)
    assert report["workflow_present"] is True
    assert report["full_suite_in_release_gate"] is True
    assert report["full_suite_lanes"], report
    # The suite lane must be one the release verdict actually depends on.
    for lane in report["full_suite_lanes"]:
        assert lane in report["required_jobs"]


def test_a_collection_count_alone_does_not_satisfy_the_full_suite_gate():
    """`pytest --collect-only` is not running the suite."""
    fake = Path(REPO_ROOT)
    report = ci_truth.release_gate_report(fake)
    assert "pytest --collect-only" not in json.dumps(report["full_suite_lanes"])


def test_every_test_file_runs_in_a_workflow_or_is_allowlisted():
    coverage = ci_truth.coverage_report(REPO_ROOT)
    assert coverage["n_test_files"] == len(ci_truth.test_inventory(REPO_ROOT))
    assert coverage["uncovered"] == [], coverage["uncovered"]
    assert coverage["covered_count"] == coverage["n_test_files"]
    assert coverage["every_test_file_covered"] is True


def test_a_new_untested_test_file_is_reported_as_a_coverage_gap(tmp_path):
    """The gate is live, not a snapshot: an uncovered file fails it."""
    (tmp_path / "tests").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "tests" / "test_orphan.py").write_text("def test_x():\n    pass\n")
    (tmp_path / ".github" / "workflows" / "release-gate.yml").write_text(
        "on:\n  push:\n    branches: ['**']\njobs:\n"
        "  full-suite:\n    name: full suite\n    steps:\n"
        "      - run: python -m pytest tests/test_other.py\n"
        "  release-gate:\n    name: release gate verdict\n    needs:\n"
        "      - full-suite\n    steps:\n      - run: echo ok\n",
        encoding="utf-8",
    )
    report = ci_truth.ci_truth_report(tmp_path)
    assert report["ok"] is False
    assert report["verdict"] == "CI_GAPS"
    assert "tests/test_orphan.py" in report["coverage"]["uncovered"]
    assert report["checks"]["every_test_file_covered"] is False
    assert any(
        error["code"] == "test_file_not_in_workflow" for error in report["errors"]
    )


def test_removing_the_full_suite_lane_fails_the_release_gate_check(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "tests" / "test_a.py").write_text("def test_x():\n    pass\n")
    (tmp_path / ".github" / "workflows" / "release-gate.yml").write_text(
        "on:\n  pull_request:\n    branches: ['**']\njobs:\n"
        "  partial:\n    name: partial\n    steps:\n"
        "      - run: python -m pytest tests/test_a.py\n"
        "  release-gate:\n    name: release gate verdict\n    needs:\n"
        "      - partial\n    steps:\n      - run: echo ok\n",
        encoding="utf-8",
    )
    report = ci_truth.ci_truth_report(tmp_path)
    assert report["checks"]["full_suite_in_release_gate"] is False
    assert any(error["code"] == "full_suite_not_required" for error in report["errors"])


def test_a_whole_tree_run_nobody_requires_is_not_coverage(tmp_path):
    """A full-suite lane outside the release aggregate cannot gate anything."""
    (tmp_path / "tests").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "tests" / "test_a.py").write_text("def test_x():\n    pass\n")
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "on:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n"
        "  nightly:\n    name: nightly\n    steps:\n"
        "      - run: python -m pytest tests/\n",
        encoding="utf-8",
    )
    coverage = ci_truth.coverage_report(tmp_path)
    assert coverage["whole_tree_lanes"] == []
    assert coverage["uncovered"] == ["tests/test_a.py"]


def test_the_prompt_regression_matrix_is_a_required_pr_check():
    report = ci_truth.required_check_report(REPO_ROOT)
    assert report["missing_required_checks"] == []
    assert "prompt-regression-matrix" in report["available_pr_check_names"]
    assert report["ok"] is True


def test_a_missing_required_check_is_a_hard_gap():
    assert "prompt-regression-matrix" in ci_truth.REQUIRED_PR_CHECKS
    # The name must come from a pull_request workflow, not a nightly only.
    documents = ci_truth.workflow_documents(REPO_ROOT)
    labels = set()
    for text in documents.values():
        if ci_truth._triggers_pull_request(text):
            for key, block in ci_truth._job_blocks(text).items():
                labels.add(ci_truth._job_name(block) or key)
    assert set(ci_truth.REQUIRED_PR_CHECKS) <= labels


def test_the_security_lane_is_in_the_pull_request_path():
    report = ci_truth.security_lane_report(REPO_ROOT)
    assert report["security_lane_in_pr_path"] is True
    assert report["security_lanes"]


def test_timing_sensitive_tests_are_flake_tested():
    report = ci_truth.flake_lane_report(REPO_ROOT)
    assert report["missing_from_flake_lane"] == []
    assert report["ok"] is True
    assert report["flake_lanes"]
    for path, reason in ci_truth.TIMING_SENSITIVE_TESTS.items():
        assert (REPO_ROOT / path).is_file(), path
        assert reason


def test_declared_timing_sensitive_files_all_exist():
    for path in ci_truth.TIMING_SENSITIVE_TESTS:
        assert (REPO_ROOT / path).is_file(), path


def test_opt_in_live_lanes_are_never_required_pr_checks():
    report = ci_truth.required_check_report(REPO_ROOT)
    assert report["manual_checks_are_manual"] is True
    assert not set(ci_truth.REQUIRED_PR_CHECKS) & set(report["manual_only_checks"])
    # The live matrix is scheduled/manual only, so it cannot be a PR check.
    documents = ci_truth.workflow_documents(REPO_ROOT)
    live = next(text for name, text in documents.items() if "nightly-quality" in name)
    assert not ci_truth._triggers_pull_request(live)
    assert ci_truth._triggers_schedule(live)


def test_aggregate_needs_is_parsed_from_the_verdict_job():
    documents = ci_truth.workflow_documents(REPO_ROOT)
    release = next(text for name, text in documents.items() if "release-gate" in name)
    needs = ci_truth.aggregate_needs(release)
    assert "full-suite" in needs
    assert "build" in needs


def test_cli_check_exits_zero_only_when_ci_is_truthful(capsys):
    assert ci_truth.main(["--root", str(REPO_ROOT), "--check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "CI_TRUTHFUL"


def test_cli_check_exits_two_on_a_gap(tmp_path, capsys):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lonely.py").write_text("def test_x():\n    pass\n")
    assert ci_truth.main(["--root", str(tmp_path), "--check", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "CI_GAPS"
