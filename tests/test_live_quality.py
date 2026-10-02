"""Live quality matrix: real n/providers/CI/cost, judged verdicts, mined failures.

A live matrix below its minimum size, or a matrix with no usable provider
credentials, is reported as blocked/incomplete — never as a pass. Every
observed failure becomes a permanent regression case.
"""

from __future__ import annotations

import json

import pytest

from evals import live_quality as lq


def _sample(
    task_id, provider, *, ok=True, cost=0.002, tokens=4000, mode="deterministic"
):
    scores = (
        {
            "correctness": 1.0,
            "minimality": 0.9,
            "explanation_quality": 0.9,
            "user_acceptance": 1.0,
        }
        if ok
        else {
            "correctness": 0.0,
            "minimality": 0.0,
            "explanation_quality": 0.0,
            "user_acceptance": 0.0,
        }
    )
    return {
        "task_id": task_id,
        "provider": provider,
        "model": f"{provider}-model",
        "status": "success" if ok else "failed",
        "answer": "grounded answer" if ok else "",
        "files": ["shared/traceview.py"],
        "grounding": ["reconstruct_task"],
        "request": "explain the trace reader",
        "cost_usd": cost,
        "total_tokens": tokens,
        "latency_ms": 1234.0,
        "changed_files": [],
        "error_class": None if ok else "ProviderTimeout",
        "judge": {
            "judge_mode": mode,
            "live_judge": mode == "live",
            "scores": scores,
            "grounding_misses": [] if ok else ["reconstruct_task"],
        },
    }


def _matrix(provider_count=2, failures=()):
    providers = [f"provider-{index}" for index in range(provider_count)]
    failed = {tuple(item) for item in failures}
    samples = [
        _sample(
            task.task_id,
            provider,
            ok=(task.task_id, provider) not in failed,
            mode="live" if (task.task_id, provider) in failed else "deterministic",
        )
        for provider in providers
        for task in lq.LIVE_QUALITY_TASKS
    ]
    return samples


def test_the_fixed_task_set_meets_the_twenty_task_minimum():
    assert len(lq.LIVE_QUALITY_TASKS) >= lq.MIN_MATRIX_TASKS == 20
    assert len({task.task_id for task in lq.LIVE_QUALITY_TASKS}) == len(
        lq.LIVE_QUALITY_TASKS
    )
    assert all(task.request for task in lq.LIVE_QUALITY_TASKS)


def test_matrix_records_n_providers_success_ci_and_cost_per_task():
    samples = _matrix(provider_count=2)
    matrix = lq.summarize_live_matrix(samples, lq.LIVE_QUALITY_TASKS)
    assert matrix["n"] == len(samples) == 40
    assert matrix["n_tasks"] == 20
    assert matrix["providers"] == ["provider-0", "provider-1"]
    assert matrix["provider_count"] == 2
    ci = matrix["success_ci"]
    assert ci["n"] == len(samples)
    assert ci["successes"] == len(samples)
    assert 0.0 <= ci["low"] <= ci["high"] <= 1.0
    assert matrix["success_rate"] == 1.0
    assert matrix["cost_per_task"] == pytest.approx(0.002)
    assert matrix["cost_per_task_usd"] == pytest.approx(0.002)
    assert matrix["tokens_per_task"] == pytest.approx(4000)
    assert matrix["meets_matrix_minimum"] is True
    assert set(matrix["judge_dimensions"]) == set(lq.JUDGE_DIMENSIONS)


def test_a_matrix_below_the_minimum_is_not_a_quality_result():
    failed = [(task.task_id, "provider-0") for task in lq.LIVE_QUALITY_TASKS[:8]]
    samples = _matrix(provider_count=1, failures=failed)
    matrix = lq.summarize_live_matrix(samples, lq.LIVE_QUALITY_TASKS)
    assert matrix["provider_count"] == 1
    assert matrix["meets_matrix_minimum"] is False
    assert matrix["successes"] < matrix["n"]
    assert (
        matrix["success_ci"]["low"]
        < matrix["success_rate"]
        < matrix["success_ci"]["high"]
    )
    assert matrix["eval_noise_band"] > 0
    # The judge still reports a vector; a failing matrix is not hidden by
    # collapsing quality into one number.
    for dimension in lq.JUDGE_DIMENSIONS:
        row = matrix["judge_dimensions"][dimension]
        assert row["n"] == len(samples)
        assert row["mean"] is not None
    assert matrix["judge_dimensions"]["correctness"]["ok"] is False


def test_per_provider_breakdown_is_reported():
    samples = _matrix(
        provider_count=2, failures=[("lq_02_span_correlation", "provider-1")]
    )
    matrix = lq.summarize_live_matrix(samples, lq.LIVE_QUALITY_TASKS)
    assert set(matrix["per_provider"]) == {"provider-0", "provider-1"}
    healthy = matrix["per_provider"]["provider-0"]
    degraded = matrix["per_provider"]["provider-1"]
    assert healthy["success_rate"] == 1.0
    assert degraded["success_rate"] < 1.0
    assert degraded["noise_band"] > healthy["noise_band"]


def test_deterministic_judge_is_labelled_and_never_reported_as_live():
    task = lq.LIVE_QUALITY_TASKS[0]
    good = lq.judge_sample(
        task, "shared/traceview.py reconstruct_task merges trace.jsonl"
    )
    assert good["judge_mode"] == "deterministic"
    assert good["live_judge"] is False
    assert good["scores"]["correctness"] == 1.0
    bad = lq.judge_sample(task, "no citations here")
    assert bad["scores"]["correctness"] == 0.0
    assert bad["scores"]["user_acceptance"] == 0.0
    assert bad["grounding_misses"]


def test_a_live_judge_callable_drives_the_scores_and_is_labelled_live():
    task = lq.LIVE_QUALITY_TASKS[0]

    def live_judge(_task, _answer, _changed):
        return {
            "scores": {
                "correctness": 0.95,
                "minimality": 0.8,
                "explanation_quality": 0.85,
                "user_acceptance": 0.9,
            },
            "rationale": "cited both sources",
        }

    judged = lq.judge_sample(task, "answer", (), live_judge)
    assert judged["judge_mode"] == "live"
    assert judged["live_judge"] is True
    assert judged["accepted"] is True
    assert judged["scores"]["correctness"] == 0.95


def test_a_real_mined_failure_becomes_a_permanent_regression_case(tmp_path):
    samples = _matrix(
        provider_count=2, failures=[("lq_19_repair_broken_test", "provider-1")]
    )
    mined = lq.mine_failures(samples)
    assert len(mined) == 1
    case = mined[0]
    assert case["task_id"] == "lq_19_repair_broken_test"
    assert case["provider"] == "provider-1"
    assert case["reason"] == "ProviderTimeout"
    assert case["regression"] is True
    assert case["fingerprint"] == "lq_19_repair_broken_test|provider-1|ProviderTimeout"

    result = lq.record_failures(mined, tmp_path)
    assert result["added_count"] == 1
    registry = tmp_path / "evals" / lq.REGISTRY_NAME
    assert registry.is_file()
    assert lq.load_regression_cases(tmp_path)[0]["fingerprint"] == case["fingerprint"]

    # A mined failure joins the nightly task set from then on.
    tasks = lq.live_task_set(tmp_path)
    assert len(tasks) == len(lq.LIVE_QUALITY_TASKS) + 1
    assert tasks[-1].task_id == "lq_19_repair_broken_test"
    assert tasks[-1].kind == "regression"

    # Re-recording the same real failure is idempotent.
    assert lq.record_failures(mined, tmp_path)["added_count"] == 0


def test_mining_is_deduplicated_by_task_provider_and_reason():
    samples = _matrix(
        provider_count=2, failures=[("lq_19_repair_broken_test", "provider-1")]
    )
    mined = lq.mine_failures(samples + samples)
    assert len(mined) == 1


def test_a_successful_sample_is_never_mined_as_a_failure():
    assert lq.mine_failures(_matrix(provider_count=2)) == []


def test_run_live_matrix_is_blocked_without_usable_credentials(monkeypatch, tmp_path):
    for name in (
        "NEO_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    report = lq.run_live_matrix([], workdir=tmp_path)
    assert report["status"] == "blocked"
    assert report["verdict"] == "NOT_RUN"
    assert report["matrix"] is None
    assert report["mined_failures"] == []
    assert report["preflight"]["credential_values_read"] is False
    assert report["n_tasks"] == len(lq.LIVE_QUALITY_TASKS)

    unconfigured = lq.run_live_matrix(
        [lq.ProviderTarget("openai", "", "NEO_MISSING_KEY")], workdir=tmp_path
    )
    assert unconfigured["status"] == "blocked"
    assert any(
        "model is not configured" in reason
        for item in unconfigured["preflight"]["blocked"]
        for reason in item["blocked_reasons"]
    )


def test_preflight_never_returns_a_credential_value(monkeypatch):
    monkeypatch.setenv("NEO_API_KEY", "sk-live-must-never-appear")
    resolved = lq.preflight([lq.ProviderTarget("openai", "gpt-x", "NEO_API_KEY")])
    assert "sk-live-must-never-appear" not in json.dumps(resolved)
    assert resolved["targets"][0]["credential_present"] is True
    assert resolved["status"] == "ready"


def test_parse_targets_reads_provider_model_pairs():
    targets = lq.parse_targets("openai:gpt-4o, anthropic:claude-haiku , ,gemini:flash")
    assert [(item.provider, item.model) for item in targets] == [
        ("openai", "gpt-4o"),
        ("anthropic", "claude-haiku"),
        ("gemini", "flash"),
    ]


def test_cli_summarize_and_mine_are_machine_readable(tmp_path, capsys):
    samples = _matrix(
        provider_count=2, failures=[("lq_19_repair_broken_test", "provider-1")]
    )
    source = tmp_path / "samples.json"
    source.write_text(json.dumps({"samples": samples}), encoding="utf-8")

    assert lq.main(["--summarize", str(source), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["matrix"]["n"] == len(samples)

    out = tmp_path / "mined.json"
    assert (
        lq.main(
            [
                "--mine",
                str(source),
                "--json",
                "--out",
                str(out),
                "--registry-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    recorded = json.loads(out.read_text(encoding="utf-8"))
    assert recorded["registry"]["added_count"] == 1
    assert recorded["mined_failures"][0]["regression"] is True
    assert (tmp_path / "evals" / lq.REGISTRY_NAME).is_file()


def test_cli_require_matrix_fails_closed_on_a_below_minimum_report(tmp_path, capsys):
    samples = _matrix(provider_count=1)
    source = tmp_path / "samples.json"
    source.write_text(json.dumps({"samples": samples}), encoding="utf-8")
    assert lq.main(["--summarize", str(source), "--json", "--require-matrix"]) == 2
    capsys.readouterr()
