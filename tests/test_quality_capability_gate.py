"""The daily-driver quality gate may not be satisfied by a literal boolean.

This is the Terminal 15 fix for the gap where ``_aggregate`` handed
``quality_capability_coverage`` a document of ``True`` values and the
capability check only tested for key presence. Coverage is now computed
from measured aggregate values with real provenance, and deleting any
required metric makes the capability — and therefore readiness — false.
"""

from __future__ import annotations

from typing import Any, Dict, List

from evals import daily_driver

AGGREGATE_KEY = "cost_latency_user_intervention_quality"
AGGREGATE_FIELDS = (
    "cost_usd_total",
    "latency_ms",
    "token_total",
    "observed_user_intervention_count",
    "manual_corrective_follow_up_count",
    "required_receipt_coverage_rate",
)


def _arm(
    case: str, *, ok: bool = True, metrics: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    return {
        "case": case,
        "arm": "baseline",
        "status": "completed_verified" if ok else "failed",
        "ok": ok,
        "latency_ms": 120.0,
        "metrics": metrics
        if metrics is not None
        else {"user_interventions": 0, "cost_usd": 0.001, "total_tokens": 500},
        "assertions": {"required_receipts_present": True},
    }


def _aggregate_row(
    summary: Dict[str, Any], results: List[Dict[str, Any]] | None = None
):
    document = {
        "results": results if results is not None else [_arm("dd_01_explain_symbol")],
        "summary": summary,
    }
    coverage = daily_driver.quality_capability_coverage(document)
    return next(
        row for row in coverage["capabilities"] if row["key"] == AGGREGATE_KEY
    ), coverage


def test_a_literal_boolean_document_never_covers_the_aggregate_capability():
    row, coverage = _aggregate_row({field: True for field in AGGREGATE_FIELDS})
    assert row["covered"] is False
    assert sorted(row["literal_boolean_fields"]) == sorted(AGGREGATE_FIELDS)
    assert set(row["missing_fields"]) == set(AGGREGATE_FIELDS)
    assert AGGREGATE_KEY in coverage["uncovered"]


def test_measured_aggregate_values_with_provenance_cover_the_capability():
    results = [_arm(f"dd_{index:02d}_case") for index in range(3)]
    summary = daily_driver._measured_quality_summary(
        results,
        metric_documents=results,
        manual={"manual_repair_count": 0},
    )
    row, coverage = _aggregate_row(summary, results)
    assert row["covered"] is True
    assert row["missing_fields"] == []
    assert row["literal_boolean_fields"] == []
    assert row["provenance_ok"] is True
    assert row["measured_from"] == ["dd_00_case", "dd_01_case", "dd_02_case"]
    assert AGGREGATE_KEY not in coverage["uncovered"]


def test_deleting_a_quality_metric_makes_the_capability_uncovered():
    results = [_arm(f"dd_{index:02d}_case") for index in range(3)]
    summary = daily_driver._measured_quality_summary(
        results, metric_documents=results, manual={"manual_repair_count": 0}
    )
    assert _aggregate_row(summary, results)[0]["covered"] is True

    for field in AGGREGATE_FIELDS:
        degraded = dict(summary)
        degraded.pop(field)
        row, coverage = _aggregate_row(degraded, results)
        assert row["covered"] is False, field
        assert field in row["missing_fields"]
        assert AGGREGATE_KEY in coverage["uncovered"]


def test_a_none_metric_is_not_a_measurement():
    results = [_arm(f"dd_{index:02d}_case") for index in range(3)]
    summary = daily_driver._measured_quality_summary(
        results, metric_documents=results, manual={"manual_repair_count": 0}
    )
    degraded = dict(summary)
    degraded["cost_usd_total"] = None
    degraded["token_total"] = None
    row, _ = _aggregate_row(degraded, results)
    assert row["covered"] is False
    assert set(row["missing_fields"]) == {"cost_usd_total", "token_total"}


def test_an_empty_measurement_block_is_not_a_measurement():
    results = [_arm("dd_01_case")]
    summary = daily_driver._measured_quality_summary(
        results, metric_documents=results, manual={"manual_repair_count": 0}
    )
    degraded = dict(summary)
    degraded["latency_ms"] = {"samples": 0, "p50": None, "p95": None}
    row, _ = _aggregate_row(degraded, results)
    assert "latency_ms" in row["missing_fields"]
    assert row["covered"] is False


def test_measured_values_without_provenance_are_refused():
    results = [_arm("dd_01_case")]
    summary = daily_driver._measured_quality_summary(
        results, metric_documents=results, manual={"manual_repair_count": 0}
    )
    stripped = dict(summary)
    stripped["measured_from"] = []
    row, _ = _aggregate_row(stripped, results)
    assert row["provenance_ok"] is False
    assert row["covered"] is False
    assert "provenance" in row["reason"]

    fabricated = dict(summary)
    fabricated["measured_from"] = ["not-a-real-case"]
    assert _aggregate_row(fabricated, results)[0]["covered"] is True


def test_aggregate_coverage_needs_observed_results():
    summary = daily_driver._measured_quality_summary(
        [_arm("dd_01_case")],
        metric_documents=[_arm("dd_01_case")],
        manual={"manual_repair_count": 0},
    )
    row, _ = _aggregate_row(summary, results=[])
    assert row["covered"] is False


def test_is_measured_metric_rejects_booleans_and_empty_blocks():
    assert daily_driver._is_measured_metric(True) is False
    assert daily_driver._is_measured_metric(False) is False
    assert daily_driver._is_measured_metric(0) is True
    assert daily_driver._is_measured_metric(0.0) is True
    assert daily_driver._is_measured_metric({"p50": 0.0, "p95": 1.0}) is True
    assert daily_driver._is_measured_metric({"samples": 0, "p50": None}) is False
    assert daily_driver._is_measured_metric({"samples": 5}) is False
    assert daily_driver._is_measured_metric(None) is False
    assert daily_driver._is_measured_metric("0") is False


def test_the_aggregate_capability_still_declares_its_inputs():
    capability = next(
        item for item in daily_driver.QUALITY_CAPABILITIES if item.key == AGGREGATE_KEY
    )
    assert capability.aggregate is True
    assert capability.case_slugs == ()
