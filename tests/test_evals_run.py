import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals import daily_driver
from evals import run as eval_run
from evals import tasks as eval_tasks


def _task(tmp_path, slug="eval_strip_boundary", target="tests/test_x.py::test_x"):
    repo = tmp_path / slug
    repo.mkdir(parents=True, exist_ok=True)
    return {
        "slug": slug,
        "repo": str(repo),
        "issue": "fix the selected behavior",
        "target": target,
        "script": {"plan": [], "scripts": {}},
    }


def _successful_receipt(spec, arm_overrides, log_root, decision_db=None, arm_name=None):
    task_dir = Path(log_root) / spec["slug"]
    task_dir.mkdir(parents=True, exist_ok=True)
    trace = task_dir / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps({"kind": kind}) for kind in ("task_start", "task_end", "result")
        )
        + "\n",
        encoding="utf-8",
    )
    (task_dir / "state.json").write_text(
        json.dumps({"files_touched": ["x.py"]}), encoding="utf-8"
    )
    return {
        "status": "success",
        "attempts": 1,
        "verified": True,
        "cost_usd": 0.0,
        "model_calls": 0,
        "wall_s": 0.0,
        "error": None,
        "integrity": {"ok": True},
        "receipts": {"ok": True, "required": {}, "missing": []},
        "ok": True,
    }


def test_selection_validation_precedes_task_build(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        eval_tasks,
        "all_tasks",
        lambda build_root: called.append(build_root) or [],
    )
    report = eval_run.run_eval(["baseline"], None, tmp_path / "out")
    assert report["verdict"] == "ERROR"
    assert called == []
    assert any(error["code"] == "baseline_only" for error in report["errors"])


@pytest.mark.parametrize(
    "arms,tasks,code",
    [
        (["baseline", "no_lint", "no_lint"], None, "duplicate_selection"),
        (["baseline", "unknown"], None, "unknown_selection"),
        (["baseline", "no_lint"], [""], "empty_selection"),
        (["baseline", "no_lint"], [], "empty_selection"),
    ],
)
def test_invalid_raw_selections_fail_before_build(
    tmp_path, monkeypatch, arms, tasks, code
):
    called = []
    monkeypatch.setattr(
        eval_tasks,
        "all_tasks",
        lambda build_root: called.append(build_root) or [],
    )
    report = eval_run.run_eval(arms, tasks, tmp_path / "out")
    assert report["verdict"] == "ERROR"
    assert called == []
    assert any(error["code"] == code for error in report["errors"])


def test_baseline_only_and_zero_comparisons_are_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_tasks, "all_tasks", lambda build_root: [])
    report = eval_run.run_eval(["baseline"], ["eval_strip_boundary"], tmp_path / "out")
    assert report["verdict"] == "ERROR"
    assert report["comparison_count"] == 0
    assert any(error["code"] == "zero_comparisons" for error in report["errors"])


def test_baseline_failure_sets_error_verdict(tmp_path, monkeypatch):
    task = _task(tmp_path)
    monkeypatch.setattr(eval_tasks, "all_tasks", lambda build_root: [task])

    def failed(*args, **kwargs):
        result = _successful_receipt(*args, **kwargs)
        result["ok"] = False
        result["status"] = "failed"
        return result

    monkeypatch.setattr(eval_run, "_run_one", failed)
    report = eval_run.run_eval(
        ["baseline", "no_lint"], ["eval_strip_boundary"], tmp_path / "out"
    )
    assert report["verdict"] == "ERROR"
    assert any(error["code"] == "baseline_failure" for error in report["errors"])


def test_target_is_forwarded_to_task_config(tmp_path, monkeypatch):
    captured = {}

    def fake_run_task(task, log_root=None):
        captured["config"] = dict(task.config)
        captured["log_root"] = Path(log_root)
        task_dir = Path(log_root) / task.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "trace.jsonl").write_text("", encoding="utf-8")
        return SimpleNamespace(
            status="error",
            attempts=0,
            verification=None,
            cost_usd=0.0,
            model_calls=[],
        )

    monkeypatch.setattr("harness.core.run_task", fake_run_task)
    task = _task(tmp_path, target="tests/test_declared.py::test_declared")
    eval_run._run_one(task, {}, tmp_path / "logs", arm_name="baseline")
    assert captured["config"]["target_test"] == task["target"]
    assert captured["log_root"] == tmp_path / "logs"
    assert not (tmp_path / "logs" / task["slug"] / task["slug"]).exists()


def test_environment_values_are_restored(tmp_path, monkeypatch):
    task = _task(tmp_path)
    monkeypatch.setattr(eval_tasks, "all_tasks", lambda build_root: [task])
    monkeypatch.setattr(eval_run, "_run_one", _successful_receipt)
    trace_value = str(tmp_path / "prior-trace")
    db_value = str(tmp_path / "prior.db")
    monkeypatch.setenv("NEO_TRACE_DIR", trace_value)
    monkeypatch.setenv("HARNESS_DECISIONS_DB", db_value)
    report = eval_run.run_eval(
        ["baseline", "no_lint"], ["eval_strip_boundary"], tmp_path / "out"
    )
    assert report["verdict"] == "CLEAN"
    assert os.environ["NEO_TRACE_DIR"] == trace_value
    assert os.environ["HARNESS_DECISIONS_DB"] == db_value


def test_json_mode_stdout_is_one_document(tmp_path, monkeypatch):
    task = _task(tmp_path)
    monkeypatch.setattr(eval_tasks, "all_tasks", lambda build_root: [task])
    monkeypatch.setattr(eval_run, "_run_one", _successful_receipt)
    coverage = eval_run._active_feature_coverage()
    coverage["ok"] = True
    coverage["uncovered"] = []
    coverage["errors"] = []
    monkeypatch.setattr(eval_run, "_active_feature_coverage", lambda: coverage)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = eval_run.main(
            [
                "--json",
                "--arms",
                "baseline,no_lint",
                "--tasks",
                "eval_strip_boundary",
                "--out-root",
                str(tmp_path / "out"),
            ]
        )
    assert code == 0
    assert stdout.getvalue().count("\n") >= 1
    document = json.loads(stdout.getvalue())
    assert document["verdict"] == "CLEAN"
    assert "-- arm:" not in stdout.getvalue()
    assert "-- arm:" in stderr.getvalue()


def test_run_uses_unique_root_for_task_builds(tmp_path, monkeypatch):
    roots = []

    def build(build_root):
        roots.append(build_root)
        task = _task(tmp_path / str(len(roots)))
        return [task]

    monkeypatch.setattr(eval_tasks, "all_tasks", build)
    monkeypatch.setattr(eval_run, "_run_one", _successful_receipt)
    first = eval_run.run_eval(
        ["baseline", "no_lint"], ["eval_strip_boundary"], tmp_path / "out"
    )
    second = eval_run.run_eval(
        ["baseline", "no_lint"], ["eval_strip_boundary"], tmp_path / "out"
    )
    assert first["verdict"] == second["verdict"] == "CLEAN"
    assert len(set(roots)) == 2
    assert all(root.parent == tmp_path / "out" for root in roots)
    assert all(root != tmp_path / "out" for root in roots)


def test_prompt_check_does_not_run_daily_driver(tmp_path, monkeypatch):
    daily_calls = []
    monkeypatch.setattr(
        eval_tasks,
        "check_set",
        lambda root: [
            {"slug": "bug01_wrap", "ok": True},
            {"slug": "eval_strip_boundary", "ok": True},
        ],
    )
    monkeypatch.setattr(
        daily_driver,
        "run_daily_suite",
        lambda *args, **kwargs: daily_calls.append((args, kwargs)),
    )
    code = eval_run.main(
        [
            "--check",
            "--out-root",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    assert daily_calls == []
    reports = list((tmp_path / "out").glob("*/eval_report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["suite"] == "prompt-regression-check"
    assert report["verdict"] == "CLEAN"


def test_default_suite_is_prompt_regression(tmp_path, monkeypatch):
    calls = {}

    def fake_run_eval(arms, slugs, out_root, quick=False, json_mode=False):
        calls.update(
            {
                "arms": arms,
                "slugs": slugs,
                "out_root": out_root,
                "quick": quick,
                "json_mode": json_mode,
            }
        )
        return {"verdict": "CLEAN", "arms": {}, "task_slugs": [], "errors": []}

    monkeypatch.setattr(eval_run, "run_eval", fake_run_eval)
    code = eval_run.main(["--quick", "--out-root", str(tmp_path / "out")])
    assert code == 0
    assert calls["arms"] == list(eval_run.ARMS)
    assert calls["slugs"] is None
    assert calls["quick"] is True


def test_explicit_prompt_selection_keeps_legacy_suite(tmp_path, monkeypatch):
    calls = {}

    def fake_run_eval(arms, slugs, out_root, quick=False, json_mode=False):
        calls.update({"arms": arms, "slugs": slugs, "quick": quick})
        return {"verdict": "CLEAN", "arms": {}, "task_slugs": [], "errors": []}

    monkeypatch.setattr(eval_run, "run_eval", fake_run_eval)
    code = eval_run.main(
        [
            "--arms",
            "baseline,no_lint",
            "--tasks",
            "eval_strip_boundary",
            "--out-root",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    assert calls == {
        "arms": ["baseline", "no_lint"],
        "slugs": ["eval_strip_boundary"],
        "quick": False,
    }


def test_combined_check_preserves_daily_report_and_writes_separate_summary(
    tmp_path, monkeypatch
):
    daily_path = tmp_path / "daily" / "daily_driver_report.json"
    daily_path.parent.mkdir(parents=True)
    daily_path.write_text('{"preserved": true}\n', encoding="utf-8")
    daily_report = {
        "matrix_check": {"ok": True},
        "feature_evidence": {"status": "complete", "results": [{"ok": True}]},
        "prompt_feature_coverage": {"complete": True, "errors": []},
        "results": [{"ok": True}],
        "summary": {
            "ready": True,
            "readiness": {"docker_lane_completed_verified": True},
            "report_path": str(daily_path),
            "pass_count": 1,
            "safe_completion_rate": 1.0,
        },
    }
    driver = SimpleNamespace(run_daily_suite=lambda *args, **kwargs: daily_report)
    monkeypatch.setattr(eval_tasks, "check_set", lambda root: [{"ok": True}])
    monkeypatch.setattr(eval_run, "_load_daily_driver", lambda: driver)
    code = eval_run.main(
        [
            "--suite",
            "combined",
            "--check",
            "--out-root",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    combined_path = daily_path.with_name("daily_driver_check_report.json")
    assert json.loads(daily_path.read_text(encoding="utf-8")) == {"preserved": True}
    combined = json.loads(combined_path.read_text(encoding="utf-8"))
    assert combined["suite"] == "combined-check"
    assert combined["verdict"] == "CLEAN"


def test_combined_check_does_not_count_a_not_ready_daily_summary(tmp_path, monkeypatch):
    daily_path = tmp_path / "daily" / "daily_driver_report.json"
    daily_path.parent.mkdir(parents=True)
    daily_path.write_text("{}\n", encoding="utf-8")
    daily_report = {
        "matrix_check": {"ok": True},
        "feature_evidence": {"status": "complete", "results": []},
        "prompt_feature_coverage": {"complete": True, "errors": []},
        "results": [{"ok": True}],
        "summary": {
            "ready": False,
            "readiness": {"docker_lane_completed_verified": False},
            "report_path": str(daily_path),
        },
    }
    driver = SimpleNamespace(run_daily_suite=lambda *args, **kwargs: daily_report)
    monkeypatch.setattr(eval_tasks, "check_set", lambda root: [{"ok": True}])
    monkeypatch.setattr(eval_run, "_load_daily_driver", lambda: driver)
    code = eval_run.main(
        [
            "--suite",
            "combined",
            "--check",
            "--out-root",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2


def test_active_feature_coverage_is_machine_readable():
    coverage = eval_run._active_feature_coverage()
    assert coverage["uncovered"] == ["self_critique"]
    assert coverage["matrix_complete"] is True
    assert coverage["complete"] is False
    assert coverage["ok"] is True
    assert coverage["errors"] == []
    for key in eval_run._ROUND_KEYS:
        row = coverage["round_keys"][key]
        arm = row["one_key_arm"]
        assert arm is not None
        assert eval_run.ARMS[arm].get(key) is False
        assert all(value is False for value in eval_run.ARMS[arm].values())
        assert row["pre_round"] is True


def test_skills_task_declares_arm_receipts(tmp_path):
    task = eval_tasks.skills_scenario_task(tmp_path / "repos")
    assert task["required_receipts"]["baseline"] == {
        "skills": {"matched": ["pytest-conventions"]},
        "skill_model_content": {
            "model_content": True,
            "rendered": ["pytest-conventions"],
        },
    }
    assert task["required_receipts"]["no_skills"]["skills"] == {
        "skipped": "skills_enabled=False"
    }
    assert task["required_receipts"]["no_skills"]["skill_model_content"] == {
        "model_content": False
    }


def test_feature_scenarios_declare_enabled_and_disabled_receipts(tmp_path):
    docs = eval_tasks.docs_scenario_task(tmp_path / "docs")
    fetch = eval_tasks.fetch_scenario_task(tmp_path / "fetch")
    lint = eval_tasks.lint_scenario_task(tmp_path / "lint")
    assert docs["required_receipts"]["baseline"] == {"docs_lookup": {"ok": True}}
    assert "no_docs" in docs["forbidden_receipts"]
    assert fetch["required_receipts"]["baseline"] == {
        "web_fetch": {"url": "https://pypi.org/project/num2words/"}
    }
    assert "no_webfetch" in fetch["forbidden_receipts"]
    assert lint["required_receipts"]["baseline"] == {
        "lint_failed": {"findings": [{"kind": "undefined_name"}]}
    }
    assert lint["forbidden_receipts"]["no_lint"] == {"lint_failed": {}}


def test_forbidden_receipt_fails_when_disabled_feature_emits(tmp_path):
    task = _task(tmp_path, slug="eval_docs_lookup")
    task["forbidden_receipts"] = {"baseline": {"docs_lookup": {}}}
    task_dir = tmp_path / "logs" / task["slug"]
    task_dir.mkdir(parents=True)
    (task_dir / "trace.jsonl").write_text(
        json.dumps({"kind": "docs_lookup", "data": {"ok": True}}) + "\n",
        encoding="utf-8",
    )
    receipts = eval_run._evaluate_receipts(
        task, "baseline", tmp_path / "logs", task["slug"]
    )
    assert receipts["ok"] is False
    assert receipts["forbidden"]["present"] == ["docs_lookup"]


def test_receipt_mismatch_makes_result_not_ok(tmp_path, monkeypatch):
    task = _task(tmp_path, slug="eval_skills_injection")
    task["required_receipts"] = {
        "baseline": {"skills": {"matched": ["pytest-conventions"]}}
    }

    def fake_run_task(task, log_root=None):
        task_dir = Path(log_root) / task.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "trace.jsonl").write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {"kind": "task_start"},
                    {"kind": "skills", "data": {"matched": []}},
                    {"kind": "task_end"},
                    {"kind": "result"},
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (task_dir / "state.json").write_text(
            json.dumps({"files_touched": ["x.py"]}), encoding="utf-8"
        )
        return SimpleNamespace(
            status="success",
            attempts=1,
            verification=SimpleNamespace(
                target_test_passed=True,
                regression_passed=True,
                flaky=False,
            ),
            cost_usd=0.0,
            model_calls=[],
        )

    monkeypatch.setattr("harness.core.run_task", fake_run_task)
    result = eval_run._run_one(task, {}, tmp_path / "logs", arm_name="baseline")
    assert result["integrity"]["ok"] is True
    assert result["receipts"]["ok"] is False
    assert result["ok"] is False


def test_missing_trace_is_not_ok(tmp_path, monkeypatch):
    task = _task(tmp_path)
    monkeypatch.setattr(
        "harness.core.run_task",
        lambda task, log_root=None: SimpleNamespace(
            status="success",
            attempts=1,
            verification=SimpleNamespace(
                target_test_passed=True,
                regression_passed=True,
                flaky=False,
            ),
            cost_usd=0.0,
            model_calls=[],
        ),
    )
    result = eval_run._run_one(task, {}, tmp_path / "logs", arm_name="baseline")
    assert result["ok"] is False
    assert result["integrity"]["ok"] is False


def test_feedback_aware_model_refuses_repair_without_explicit_aci_feedback():
    contracts = {
        "1": [
            {
                "attempt": 2,
                "all_of": ["syntax error"],
                "description": "repair requires syntax feedback",
            }
        ]
    }
    model = eval_run._FeedbackAwareScriptedModel(
        plan=[],
        scripts={1: [["broken edit"], ["corrected edit"]]},
        feedback_contracts=contracts,
    )
    first = model(
        [
            {
                "role": "system",
                "content": "your step is #1 of 1",
            },
            {
                "role": "user",
                "content": "Begin. Reply with exactly ONE bash command",
            },
        ]
    )
    assert first == "broken edit"
    refused = model(
        [
            {"role": "system", "content": "your step is #1 of 1"},
            {
                "role": "user",
                "content": "Begin. Reply with exactly ONE bash command",
            },
        ]
    )
    assert refused == "echo FEEDBACK_CONTRACT_MISSING"
    report = model.feedback_report()
    assert report["ok"] is False
    assert report["missing"][0]["missing"] == ["syntax error"]
    assert report["negative_control_triggered"] is True

    repaired = eval_run._FeedbackAwareScriptedModel(
        plan=[],
        scripts={1: [["broken edit"], ["corrected edit"]]},
        feedback_contracts=contracts,
    )
    repaired(
        [
            {"role": "system", "content": "your step is #1 of 1"},
            {"role": "user", "content": "Begin. Reply with exactly ONE bash command"},
        ]
    )
    second = repaired(
        [
            {"role": "system", "content": "your step is #1 of 1"},
            {
                "role": "user",
                "content": (
                    "Begin. Reply with exactly ONE bash command\n"
                    "Feedback: syntax error in generated edit"
                ),
            },
        ]
    )
    assert second == "corrected edit"
    assert repaired.feedback_report()["ok"] is True


def test_retry_tasks_declare_non_vacuous_feedback_contracts(tmp_path):
    repair = eval_tasks.repair_scenario_task(tmp_path / "repair")
    lint = eval_tasks.lint_scenario_task(tmp_path / "lint")
    assert repair["feedback_contracts"]["1"][0]["all_of"] == ["syntax error"]
    assert lint["feedback_contracts"]["1"][0]["all_of"] == [
        "Feedback from the previous attempt",
        "_CANONICAL_CASE",
    ]


def test_prompt_runner_fails_closed_on_zero_observed_comparisons(tmp_path, monkeypatch):
    task = _task(tmp_path)
    monkeypatch.setattr(eval_tasks, "all_tasks", lambda build_root: [task])

    def observed(_spec, _overrides, log_root, decision_db=None, arm_name=None):
        result = _successful_receipt(
            _spec,
            _overrides,
            log_root,
            decision_db=decision_db,
            arm_name=arm_name,
        )
        if arm_name == "baseline":
            return result
        result.update(
            {
                "status": "crash",
                "ok": False,
                "integrity": {"ok": False},
                "receipts": {"ok": False},
            }
        )
        return result

    monkeypatch.setattr(eval_run, "_run_one", observed)
    report = eval_run.run_eval(
        ["baseline", "no_lint"],
        ["eval_strip_boundary"],
        tmp_path / "out",
    )
    assert report["valid_comparison_count"] == 0
    assert any(error["code"] == "zero_valid_comparisons" for error in report["errors"])
    assert report["verdict"] == "ERROR"


def test_prompt_runner_reports_source_isolation(tmp_path, monkeypatch):
    task = _task(tmp_path)
    monkeypatch.setattr(eval_tasks, "all_tasks", lambda build_root: [task])
    monkeypatch.setattr(eval_run, "_run_one", _successful_receipt)
    report = eval_run.run_eval(
        ["baseline", "no_lint"],
        ["eval_strip_boundary"],
        tmp_path / "out",
    )
    assert report["benchmark_isolation"]["source_repositories_unchanged"] is True
    assert report["benchmark_isolation"]["one_action_per_scripted_reply"] is True
    assert report["valid_comparison_count"] == 1


def test_daily_cli_forwards_explicit_provider_lane(tmp_path, monkeypatch):
    captured = {}

    def fake_daily_suite(*args, **kwargs):
        captured.update(kwargs)
        return {
            "verdict": "NOT_READY",
            "summary": {},
            "results": [],
            "lanes": {"live_provider": {"status": "completed"}},
        }

    monkeypatch.setattr(daily_driver, "run_daily_suite", fake_daily_suite)
    code = eval_run.main(
        [
            "--suite",
            "daily-driver",
            "--check",
            "--json",
            "--live-provider",
            "--provider-model",
            "unit-model",
            "--provider",
            "openai",
            "--provider-base-url",
            "https://provider.invalid/v1",
            "--provider-key-env",
            "NEO_API_KEY",
            "--out-root",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2
    assert captured["include_live_provider"] is True
    assert captured["live_provider_config"]["model"] == "unit-model"
    assert captured["live_provider_config"]["provider"] == "openai"
    assert captured["live_provider_config"]["key_env"] == "NEO_API_KEY"
