"""Measured SLOs: real aggregates drive the verdict, never a boolean.

Two required behaviours are pinned here:

* deleting a quality metric makes readiness false (no silent pass, and no
  "missed" either — it is honest ``INSUFFICIENT_EVIDENCE``);
* real aggregate values, not caller-supplied booleans, drive the verdict
  (a record without a verifiable on-disk artifact is refused).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from evals import slos


def _write_run(
    logs: Path,
    task_id: str,
    *,
    status: str = "completed_verified",
    cost: float = 0.001,
    tokens: int = 1000,
    latency: float = 1000.0,
    start_ts: float = 1000.0,
    interventions: int = 0,
    sandbox_ms: float = 120.0,
    context_ratio: float = 0.4,
    routed: int = 2,
    fallbacks: int = 0,
) -> Path:
    """Write one REAL run directory: state.json + trace.jsonl + overlay."""
    task_dir = logs / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "state.json").write_text(
        json.dumps(
            {
                "status": status,
                "session_id": "session-1",
                "model": "model-1",
                "user_interventions": interventions,
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {"kind": "task_start", "ts": start_ts, "data": {}},
        {
            "kind": "context_budget",
            "ts": start_ts + 0.1,
            "data": {"used": 400, "limit": 1000},
        },
        {
            "kind": "model_response",
            "ts": start_ts + 0.2,
            "data": {"usage": {"cost": cost, "total_tokens": tokens}},
        },
        {
            "kind": "task_end",
            "ts": start_ts + latency / 1000.0,
            "data": {"status": status},
        },
    ]
    (task_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    overlay = [
        {
            "ts": start_ts + 0.01,
            "module": "execution",
            "event": "sandbox_call",
            "task_id": task_id,
            "call_id": "c1",
            "command": "pytest",
        },
        {
            "ts": start_ts + 0.01 + sandbox_ms / 1000.0,
            "module": "execution",
            "event": "sandbox_result",
            "task_id": task_id,
            "call_id": "c1",
        },
    ]
    for index in range(routed):
        overlay.append(
            {
                "ts": start_ts + 0.02,
                "module": "runtime",
                "event": "model_routed",
                "task_id": task_id,
                "model": "model-1",
                "provider": "p1",
                "cost_usd": cost / max(1, routed),
                "total_tokens": tokens // max(1, routed),
                "routed_via_hint": "fallback" if index < fallbacks else "hint",
            }
        )
    overlay_dir = task_dir / "_trace"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    (overlay_dir / f"{task_id}.jsonl").write_text(
        "\n".join(json.dumps(row) for row in overlay) + "\n", encoding="utf-8"
    )
    return task_dir / "state.json"


def test_collect_measurements_derives_every_slo_input_from_disk(tmp_path):
    logs = tmp_path / "logs"
    _write_run(logs, "task-a")
    records = slos.collect_measurements(logs)
    assert len(records) == 1
    record = records[0].to_dict()
    assert record["status"] == slos.VERIFIED_STATUS
    assert record["latency_ms"] == pytest.approx(1000.0)
    assert record["cost_usd"] > 0
    assert record["total_tokens"] > 0
    assert record["user_interventions"] == 0
    assert record["sandbox_command_latencies_ms"] == [pytest.approx(120.0)]
    assert record["context_utilization"] == [pytest.approx(0.4)]
    assert record["provider_request_count"] == 2
    assert record["provider_fallback_count"] == 0
    assert slos.measurement_metrics(record) == list(slos.REQUIRED_METRICS)


def test_a_healthy_real_run_set_meets_every_slo(tmp_path):
    logs = tmp_path / "logs"
    for index in range(5):
        _write_run(logs, f"task-{index}", cost=0.001, tokens=1000, latency=1000.0)
    report = slos.evidence_summary(logs)
    assert report["n_measurements_accepted"] == 5
    assert report["missing_metrics"] == []
    assert report["missed"] == []
    assert report["verdict"] == "MEETS_SLOS"
    assert report["ready"] is True
    keys = {row["key"] for row in report["slo"]}
    assert {
        "verified_success_rate",
        "fix_latency_p50_ms",
        "fix_latency_p95_ms",
        "cost_per_task_usd",
        "tokens_per_task",
        "user_interventions_per_task",
        "sandbox_command_latency_p95_ms",
        "context_utilization_p95",
        "provider_fallback_rate",
    } <= keys


def test_deleting_a_quality_metric_makes_readiness_false(tmp_path):
    logs = tmp_path / "logs"
    for index in range(3):
        _write_run(logs, f"task-{index}")
    baseline = slos.evidence_summary(logs)
    assert baseline["ready"] is True

    # Remove the context-utilization receipt from one run: that run no
    # longer carries the metric, so the SLO loses its measurement.
    task = logs / "task-1" / "trace.jsonl"
    rows = [
        line
        for line in task.read_text(encoding="utf-8").splitlines()
        if "context_budget" not in line
    ]
    task.write_text("\n".join(rows) + "\n", encoding="utf-8")

    degraded = slos.evidence_summary(logs)
    assert degraded["ready"] is False
    assert degraded["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "context_utilization_p95" in degraded["unmeasured"]
    assert degraded["readiness"]["context_utilization_p95"] is False
    # It is reported as unmeasured, never as a silent pass.
    row = next(
        item for item in degraded["slo"] if item["key"] == "context_utilization_p95"
    )
    assert row["measured"] is None
    assert row["status"] == "unmeasured"


def test_deleting_every_run_artifact_is_insufficient_evidence_not_a_pass(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    report = slos.evidence_summary(logs)
    assert report["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert report["ready"] is False
    assert report["n_measurements_accepted"] == 0
    assert set(report["unmeasured"]) == {spec.key for spec in slos.SLO_SPECS}


def test_caller_supplied_booleans_cannot_drive_the_verdict(tmp_path):
    literal = {
        "task_id": "fabricated",
        "status": "completed_verified",
        "verified": True,
        "cost_usd": True,
        "total_tokens": True,
        "user_interventions": True,
        "latency_ms": True,
        "provider_request_count": True,
        "provider_fallback_count": True,
        "sandbox_command_latencies_ms": True,
        "context_utilization": True,
    }
    report = slos.slo_report([literal], root=tmp_path)
    assert report["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert report["ready"] is False
    assert report["n_measurements_accepted"] == 0
    assert report["rejected_records"][0]["reason"] == "missing artifact provenance"


def test_a_record_without_a_matching_artifact_is_refused(tmp_path):
    logs = tmp_path / "logs"
    _write_run(logs, "task-0")
    records = [item.to_dict() for item in slos.collect_measurements(logs)]
    assert records and records[0]["artifact"]
    Path(records[0]["artifact"]).write_text(
        '{"status": "completed_verified"}\n', encoding="utf-8"
    )
    report = slos.slo_report(records, root=logs)
    assert report["n_measurements_accepted"] == 0
    assert any(
        item["reason"] == "artifact digest does not match"
        for item in report["rejected_records"]
    )
    assert report["ready"] is False


def test_a_measured_miss_is_distinct_from_missing_evidence(tmp_path):
    logs = tmp_path / "logs"
    for index in range(4):
        _write_run(
            logs,
            f"task-{index}",
            cost=0.50,
            tokens=900_000,
            latency=2_000_000.0,
            interventions=3,
        )
    report = slos.evidence_summary(logs)
    assert report["verdict"] == "MISSES_SLOS"
    assert report["ready"] is False
    assert set(report["missed"]) >= {
        "fix_latency_p50_ms",
        "cost_per_task_usd",
        "tokens_per_task",
        "user_interventions_per_task",
    }
    assert report["unmeasured"] == []
    assert all(row["status"] != "unmeasured" for row in report["slo"])


def test_completed_unverified_never_counts_as_a_verified_success(tmp_path):
    logs = tmp_path / "logs"
    for index in range(2):
        _write_run(logs, f"verified-{index}")
    for index in range(2):
        _write_run(logs, f"unverified-{index}", status="completed_unverified")
    report = slos.evidence_summary(logs)
    row = next(item for item in report["slo"] if item["key"] == "verified_success_rate")
    assert row["successes"] == 2
    assert row["measured"] == pytest.approx(0.5)
    assert row["ok"] is False
    assert report["ready"] is False


def test_wilson_interval_is_bounded_and_shrinks_with_samples():
    low, high = slos.wilson_interval(0, 0)
    assert low is None and high is None
    low, high = slos.wilson_interval(9, 10)
    assert 0.0 <= low < 0.9 < high <= 1.0
    small = slos.wilson_interval(5, 10)
    large = slos.wilson_interval(500, 1000)
    assert (small[1] - small[0]) > (large[1] - large[0])
    assert math.isclose(slos.wilson_interval(0, 10)[0], 0.0)
    assert math.isclose(slos.wilson_interval(10, 10)[1], 1.0)


def test_eval_noise_band_is_published_and_honest_about_no_samples():
    assert slos.eval_noise_band(0, 0) is None
    assert slos.eval_noise_band(8, 10) == pytest.approx(0.226578, abs=1e-5)


def test_percentile_reports_no_value_for_no_samples():
    assert slos.percentile([], 0.5) is None
    assert slos.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert slos.percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)


def test_boolean_is_never_treated_as_a_number():
    assert slos._is_number(True) is False
    assert slos._is_number(False) is False
    assert slos._is_number(0) is True
    assert slos._is_number(0.0) is True
    assert slos._is_number("1") is False


def test_slo_cli_reports_and_fails_closed(tmp_path, capsys):
    logs = tmp_path / "logs"
    _write_run(logs, "task-1")
    assert slos.main(["--logs-root", str(logs), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "MEETS_SLOS"
    assert slos.main(["--logs-root", str(logs), "--require-ready"]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert slos.main(["--logs-root", str(empty), "--require-ready"]) == 2
