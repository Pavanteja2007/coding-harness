"""Contract tests for the deterministic daily-driver evaluation harness."""

from __future__ import annotations

import json
import os
import site
from pathlib import Path

import pytest

from evals import daily_driver
from evals import run as eval_run


def test_matrix_has_all_unique_required_scenarios():
    report = daily_driver.check_matrix()
    assert report["ok"] is True
    assert report["case_count"] == len(daily_driver.REQUIRED_SCENARIOS) == 26
    assert len(set(report["case_slugs"])) == 26
    assert tuple(report["scenario_ids"]) == daily_driver.REQUIRED_SCENARIOS
    assert len(report["required_capabilities"]) == 17


def test_every_case_has_real_probe_and_both_arm_receipts():
    for case in daily_driver.case_definitions():
        assert callable(case.probe)
        assert case.owner
        assert case.integration_target
        assert case.required_receipts["baseline"]
        assert case.required_receipts["adversarial"]
        assert set(case.expected_statuses) == set(daily_driver.ARMS)


@pytest.mark.parametrize(
    "cases,arms,error_code",
    [
        (["dd_01_explain_symbol"], ["baseline"], "zero_comparisons"),
        (["unknown"], ["baseline", "adversarial"], "unknown_case"),
        (
            ["dd_01_explain_symbol", "dd_01_explain_symbol"],
            daily_driver.ARMS,
            "duplicate_case",
        ),
        (["dd_01_explain_symbol"], ["adversarial"], "missing_baseline"),
        (["dd_01_explain_symbol"], ["baseline", "unknown"], "unknown_arm"),
        ([], daily_driver.ARMS, "empty_case_selection"),
        (["dd_01_explain_symbol"], [], "empty_arm_selection"),
    ],
)
def test_invalid_selection_fails_closed(cases, arms, error_code):
    with pytest.raises(daily_driver.EvaluationError):
        daily_driver.validate_selection(cases, arms)


def test_prompt_feature_coverage_is_fail_closed_until_evidence_is_observed():
    coverage = daily_driver.prompt_feature_coverage()
    assert coverage["complete"] is False
    assert coverage["observed_feature_count"] == 0
    assert set(coverage["uncovered"]) == {
        feature.key for feature in daily_driver.ACTIVE_PROMPT_FEATURES
    }
    assert "self_critique" in coverage["uncovered"]
    assert "coordination_gate" in coverage["uncovered"]
    assert "steering_enabled" in coverage["uncovered"]


def test_feature_evidence_lane_covers_every_active_feature(tmp_path):
    report = daily_driver.run_feature_evidence(tmp_path / "feature-evidence")
    # 14 registered features x 2 arms. The count is derived from the registry
    # AND pinned to a literal, so adding a feature without a matching arm (or
    # silently dropping one) fails here rather than quietly shrinking coverage.
    feature_count = len(daily_driver.FEATURE_EVIDENCE_SPECS)
    assert feature_count == 14
    assert len(daily_driver.ACTIVE_PROMPT_FEATURES) == feature_count
    assert report["status"] == "complete"
    assert report["pass_count"] == 2 * feature_count
    assert report["fail_count"] == 0
    assert {item["feature"] for item in report["results"]} == {
        feature.key for feature in daily_driver.ACTIVE_PROMPT_FEATURES
    }
    coverage = daily_driver.prompt_feature_coverage(report)
    assert coverage["complete"] is True
    assert coverage["observed_feature_count"] == feature_count
    assert coverage["passed_arm_count"] == 2 * feature_count
    assert "verification_intelligence" in {
        feature.key for feature in daily_driver.ACTIVE_PROMPT_FEATURES
    }


def test_blocked_feature_arm_is_not_a_pass():
    spec = daily_driver.FEATURE_EVIDENCE_SPECS["intent_enabled"]
    blocked = daily_driver._blocked_feature_result(
        spec, spec.enabled_arm, "worker unavailable"
    )
    blocked["ok"] = True
    coverage = daily_driver.prompt_feature_coverage([blocked])
    assert coverage["complete"] is False
    assert "intent_enabled" in coverage["uncovered"]


def test_manual_repair_evidence_requires_explicit_source_fields(tmp_path):
    source = tmp_path / "manual.json"
    source.write_text(
        json.dumps(
            {
                "samples": [
                    {"id": "a", "status": "success", "manual_repair_required": False},
                    {"id": "b", "status": "success", "manual_repair_required": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    report = daily_driver.load_manual_repair_evidence(source)
    assert report["eligible"] is True
    assert report["sample_count"] == 2
    assert report["no_manual_repair_count"] == 1
    assert report["no_manual_repair_rate"] == 0.5
    assert report["threshold_met"] is False


def test_manual_repair_evidence_is_unknown_when_source_is_absent(tmp_path):
    report = daily_driver.load_manual_repair_evidence(tmp_path / "missing.json")
    assert report["status"] == "missing"
    assert report["eligible"] is False
    assert report["sample_count"] == 0
    assert report["no_manual_repair_rate"] is None
    assert report["no_manual_repair_count"] is None


def test_legacy_success_report_without_manual_fields_does_not_fabricate_rate():
    report = daily_driver.load_manual_repair_evidence(
        {
            "parse": {
                "status": "success",
                "checks": [{"name": "verified", "ok": True}],
            }
        }
    )
    assert report["sample_count"] == 1
    assert report["eligible"] is False
    assert report["no_manual_repair_rate"] is None


def test_readiness_requires_full_observed_matrix_and_real_lanes(tmp_path):
    feature_results = []
    for spec in daily_driver.FEATURE_EVIDENCE_SPECS.values():
        for arm, receipt in (
            (spec.enabled_arm, spec.enabled_receipt),
            (spec.disabled_arm, spec.disabled_receipt),
        ):
            feature_results.append(
                {
                    "feature": spec.key,
                    "arm": arm,
                    "status": "completed_verified",
                    "ok": True,
                    "assertions": {"ok": True},
                    "receipts": {receipt: True},
                    "evidence": {"observed": True},
                    "required_receipts": [receipt],
                    "artifact_dir": str(tmp_path / spec.key / arm),
                    "trace_path": str(tmp_path / spec.key / arm / "trace.jsonl"),
                }
            )
    results = []
    for case in daily_driver.case_definitions():
        for arm in daily_driver.ARMS:
            status = case.expected_statuses[arm][0]
            receipts = {receipt: True for receipt in case.required_receipts[arm]}
            if case.slug == "dd_26_lsp_diagnostic_repair":
                status = "completed_verified"
                receipts["lsp_diagnostic_repair_receipt"] = True
            results.append(
                {
                    "case": case.slug,
                    "arm": arm,
                    "ok": True,
                    "status": status,
                    "latency_ms": 1,
                    "metrics": {
                        "cost_usd": 0.0,
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                        "trace_to_ui_latency_ms": [],
                        "multi_action_replies": 0,
                    },
                    "assertions": {"required_receipts_present": True},
                    "receipts": receipts,
                    "trace_path": str(tmp_path / case.slug / arm / "trace.jsonl"),
                    "artifact_dir": str(tmp_path / case.slug / arm),
                    "unauthorized_mutations": 0,
                    "lost_edits": 0,
                    "false_verified_successes": 0,
                    "context_continuity_failures": 0,
                    "permission_failures": 0,
                    "resume_failures": 0,
                    "ui_thread_stalls": 0,
                }
            )
    manual = {
        "status": "complete",
        "eligible": True,
        "no_manual_repair_rate": 1.0,
        "sample_count": 1,
        "manual_repair_count": 0,
    }
    summary = daily_driver._aggregate(
        results,
        [case.slug for case in daily_driver.case_definitions()],
        tmp_path,
        "completed_verified",
        docker_lane={"status": "completed_verified", "metrics": {}},
        feature_evidence={
            "status": "complete",
            "selected": True,
            "results": feature_results,
        },
        manual_evidence=manual,
        selected_arms=daily_driver.ARMS,
        live_provider={
            "status": "completed",
            "selected": True,
            "required_for_readiness": True,
            "metrics": {},
        },
    )
    assert summary["ready"] is True
    assert summary["valid_comparison_count"] == 26
    assert summary["readiness"]["all_required_cases_selected"] is True
    assert summary["readiness"]["all_required_arms_selected"] is True
    assert summary["readiness"]["feature_evidence_lane_selected_and_completed"] is True
    assert summary["readiness"]["live_provider_lane_selected_and_completed"] is True
    assert summary["readiness"]["required_quality_capabilities_observed"] is True
    assert (
        summary["readiness"]["sampled_real_development_manual_repair_at_least_90pct"]
        is True
    )
    assert summary["manual_corrective_follow_up_count"] == 0


def test_legacy_prompt_coverage_reports_gaps_without_failing_registered_matrix():
    coverage = eval_run._active_feature_coverage()
    assert coverage["matrix_complete"] is True
    assert coverage["ok"] is True
    assert coverage["uncovered"] == ["self_critique"]
    assert coverage["errors"] == []


def test_isolated_worker_environment_omits_provider_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("NEO_API_KEY", "unit-placeholder-never-a-real-credential")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-placeholder-never-a-real-credential")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-placeholder-never-a-real-credential")
    env = daily_driver._isolated_child_env(tmp_path / "case")
    assert "NEO_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert Path(env["HOME"]).is_relative_to(tmp_path)
    assert Path(env["HARNESS_DECISIONS_DB"]).is_relative_to(tmp_path)
    assert Path(env["NEO_CONFIG"]).is_relative_to(tmp_path)
    assert Path(env["NEO_TRACE_DIR"]).is_relative_to(tmp_path)
    assert env["HARNESS_LOGS_DIR"] != str(Path.home() / "logs")
    python_paths = env["PYTHONPATH"].split(os.pathsep)
    assert all(
        path in python_paths
        for path in site.getsitepackages()
        if "site-packages" in path
    )
    assert any(
        "site-packages" in path or "dist-packages" in path for path in python_paths
    )


def test_quick_matrix_runs_real_comparison_with_receipts(tmp_path):
    report = daily_driver.run_daily_suite(
        tmp_path / "out",
        case_slugs=["dd_01_explain_symbol"],
        arms=list(daily_driver.ARMS),
        include_docker=False,
        json_mode=True,
    )
    assert report["comparison_count"] == 1
    assert report["valid_comparison_count"] == 1
    assert len(report["results"]) == 2
    assert all(result["ok"] is True for result in report["results"])
    assert all(
        result["assertions"]["required_receipts_present"] is True
        for result in report["results"]
    )
    assert report["feature_evidence"]["status"] == "complete"
    assert report["prompt_feature_coverage"]["complete"] is True
    assert report["lanes"]["live_provider"]["status"] == "not_selected"
    assert report["lanes"]["live_provider"]["required_for_readiness"] is True
    assert report["readiness"]["all_required_cases_selected"] is False
    assert report["readiness"]["live_provider_lane_selected_and_completed"] is False
    assert report["ready"] is False
    report_path = Path(report["summary"]["report_path"])
    assert report_path.is_file()
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["results"][0]["receipts"]["model_context_contains_symbol"] is True


def test_aggregate_reports_required_metrics_and_critical_failures(tmp_path):
    results = [
        {
            "arm": "baseline",
            "case": "dd_01_explain_symbol",
            "ok": True,
            "status": "completed_unverified",
            "latency_ms": 10,
            "metrics": {
                "cost_usd": 0.1,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "trace_to_ui_latency_ms": [5],
            },
            "receipts": {"receipt": True},
            "unauthorized_mutations": 0,
            "lost_edits": 0,
            "false_verified_successes": 0,
            "context_continuity_failures": 0,
            "permission_failures": 0,
            "resume_failures": 0,
            "ui_thread_stalls": 0,
        },
        {
            "arm": "adversarial",
            "case": "dd_01_explain_symbol",
            "ok": False,
            "status": "failed",
            "latency_ms": 30,
            "metrics": {
                "cost_usd": 0.2,
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "trace_to_ui_latency_ms": [15],
            },
            "receipts": {},
            "unauthorized_mutations": 1,
            "lost_edits": 1,
            "false_verified_successes": 1,
            "context_continuity_failures": 1,
            "permission_failures": 1,
            "resume_failures": 1,
            "ui_thread_stalls": 1,
        },
    ]
    summary = daily_driver._aggregate(
        results, ["dd_01_explain_symbol"], tmp_path, "blocked"
    )
    required = {
        "completed_verified",
        "completed_unverified",
        "blocked",
        "failed",
        "cancelled",
        "unauthorized_mutations",
        "false_verified_successes",
        "context_continuity_failures",
        "permission_failures",
        "resume_failures",
        "ui_thread_stalls",
        "trace_to_ui_latency_ms",
        "latency_ms",
        "cost_usd_total",
        "token_total",
        "manual_corrective_follow_up_count",
    }
    assert required.issubset(summary)
    assert summary["unauthorized_mutations"] == 1
    assert summary["false_verified_successes"] == 1
    assert summary["lost_edits"] == 1
    assert summary["ready"] is False
    assert summary["skip_count"] == 1
    assert summary["live_provider_required_for_readiness"] is True


@pytest.mark.parametrize("arm", daily_driver.ARMS)
@pytest.mark.parametrize(
    "slug",
    [
        "dd_21_repair_broken_test",
        "dd_22_project_instructions",
        "dd_23_large_repo_map",
        "dd_24_long_session_continuity",
        "dd_25_checkpoint_hard_kill",
    ],
)
def test_new_daily_quality_cases_pass_both_arms(tmp_path, slug, arm):
    case = daily_driver.case_map()[slug]
    outcome = case.probe(tmp_path / slug / arm, arm)
    assert outcome.status in case.expected_statuses[arm]
    assert all(outcome.assertions.values()), outcome.assertions
    assert all(
        outcome.receipts.get(name) is True for name in case.required_receipts[arm]
    )


def test_lsp_lane_consumes_real_diagnostics_and_covers_quality_capability(tmp_path):
    outcome = daily_driver._probe_26(tmp_path, "baseline")
    assert outcome.status == "completed_verified"
    assert outcome.receipts["lsp_diagnostic_repair_receipt"] is True
    assert outcome.receipts["lsp_lifecycle_receipt"] is True
    assert outcome.receipts["lsp_diagnostic_consumed_receipt"] is True
    assert outcome.evidence["diagnostic_before"]["code"] == "undefined-name"
    assert outcome.evidence["diagnostic_after"] == []
    assert outcome.evidence["ast_lint_used"] is False
    document = {
        "results": [
            {
                "case": "dd_26_lsp_diagnostic_repair",
                "arm": arm,
                "status": outcome.status,
                "ok": True,
                "receipts": outcome.receipts,
            }
            for arm in daily_driver.ARMS
        ]
    }
    coverage = daily_driver.quality_capability_coverage(document)
    assert "lsp_diagnostic_repair" not in coverage["uncovered"]


def test_daily_actions_are_independent():
    model = daily_driver._QueueModel(
        [
            json.dumps({"tool": "read", "path": "app.py"}),
            json.dumps(
                {
                    "calls": [
                        {"tool": "read", "path": "app.py"},
                        {"tool": "glob", "pattern": "*.py"},
                    ]
                }
            ),
        ]
    )
    model([{"role": "user", "content": "one"}])
    with pytest.raises(AssertionError, match="multiple actions"):
        model([{"role": "user", "content": "two"}])
    assert model.action_replies == 1
    assert model.multi_action_replies == 1


def test_provider_preflight_fails_closed_without_model_or_credential(monkeypatch):
    for name in (
        "NEO_EVAL_PROVIDER_MODEL",
        "NEO_MODEL",
        "NEO_EVAL_PROVIDER_BASE_URL",
        "NEO_BASE_URL",
        "NEO_API_BASE",
        "NEO_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    blocked = daily_driver._provider_preflight(
        {
            "model": "",
            "base_url": "https://provider.invalid/v1",
            "key_env": "NEO_API_KEY",
        }
    )
    assert blocked["status"] == "blocked"
    assert "model is not configured" in blocked["blocked_reasons"]
    assert "api_key" not in blocked
    monkeypatch.setenv("NEO_MODEL", "unit-provider-model")
    monkeypatch.setenv("NEO_API_KEY", "unit-placeholder-never-real")
    ready = daily_driver._provider_preflight(
        {"base_url": "https://provider.invalid/v1"}
    )
    assert ready["status"] == "ready"
    assert ready["credential_present"] is True
    assert "unit-placeholder-never-real" not in str(ready)


def test_observed_comparisons_must_be_positive_and_complete(tmp_path, monkeypatch):
    assert (
        daily_driver._valid_daily_comparisons(
            [], ["dd_01_explain_symbol"], daily_driver.ARMS
        )
        == 0
    )

    def failed_worker(case, arm, case_root, timeout_s):
        return {
            "status": "failed" if arm == "adversarial" else "completed_unverified",
            "assertions": {"worker_completed": arm == "baseline"},
            "receipts": {
                receipt: arm == "baseline" for receipt in case.required_receipts[arm]
            },
            "evidence": {},
            "events": [],
            "metrics": {},
            "error": "adversarial failure" if arm == "adversarial" else "",
        }

    monkeypatch.setattr(daily_driver, "_run_worker", failed_worker)
    report = daily_driver.run_daily_suite(
        tmp_path / "out",
        case_slugs=["dd_01_explain_symbol"],
        arms=list(daily_driver.ARMS),
        include_docker=False,
        include_feature_evidence=False,
        json_mode=True,
    )
    assert report["verdict"] == "ERROR"
    assert report["valid_comparison_count"] == 0
    assert any(error["code"] == "daily_case_failed" for error in report["errors"])
    assert any(error["code"] == "zero_valid_comparisons" for error in report["errors"])
