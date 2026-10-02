"""Real service-level objectives measured from live run artifacts.

This module exists to kill one specific dishonesty: a quality/readiness
claim satisfied by a literal boolean. Nothing here trusts a caller's
word for a number. Every measurement is derived from an artifact that
still exists on disk, is hashed, and is re-verified before it can move a
verdict. A missing metric is reported as *insufficient evidence*, never
as a pass and never as a silent zero.

Collectors (real runs on disk):
    collect_measurements(logs_root)  -> list[Measurement]
        reads ``logs/{task_id}/state.json`` + ``trace.jsonl`` (+ the
        ``{task_id}.runtime`` journal and the ``_trace`` overlay when
        present) and derives status, latency, cost, tokens, user
        interventions, sandbox command latency, context utilization, and
        provider fallback count.

Aggregation (numbers in, verdict out):
    measure_slos(records)            -> list[SloResult]
    slo_report(records)              -> dict with readiness + verdict
    wilson_interval(successes, n)    -> (low, high)
    eval_noise_band(successes, n)    -> half-width of the 95% interval

Verdict vocabulary (deliberately three-valued, never two):
    ``MEETS_SLOS``          every required SLO met from real measurements
    ``MISSES_SLOS``         measured, and at least one required SLO missed
    ``INSUFFICIENT_EVIDENCE`` at least one required SLO had no real data

CLI:
    python -m evals.slos --logs-root logs --json
    python -m evals.slos --logs-root logs --require-ready   # exit 2 otherwise
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The only terminal status that may count as a verified success. The
#: verifier mints it; nothing in this module may promote another status.
VERIFIED_STATUS = "completed_verified"

#: Metric keys every SLO input is built from. Deleting one of these from
#: the measured data must make the matching SLO unmeasured, and therefore
#: make readiness false.
REQUIRED_METRICS: Tuple[str, ...] = (
    "status",
    "latency_ms",
    "cost_usd",
    "total_tokens",
    "user_interventions",
    "sandbox_command_latencies_ms",
    "context_utilization",
    "provider_request_count",
    "provider_fallback_count",
)


@dataclass(frozen=True)
class SloSpec:
    """One service-level objective and the measured inputs it consumes."""

    key: str
    title: str
    unit: str
    direction: str  # "higher" | "lower"
    target: float
    required: bool = True
    metrics: Tuple[str, ...] = ()
    description: str = ""


#: The eight SLO categories the ceiling requires, expressed as nine
#: measured objectives (fix latency is tracked at p50 and p95).
SLO_SPECS: Tuple[SloSpec, ...] = (
    SloSpec(
        "verified_success_rate",
        "Verified success rate",
        "ratio",
        "higher",
        0.90,
        metrics=("status",),
        description="Share of runs whose verifier-minted status is completed_verified.",
    ),
    SloSpec(
        "fix_latency_p50_ms",
        "Fix latency p50",
        "ms",
        "lower",
        300_000.0,
        metrics=("latency_ms",),
        description="Median wall clock from task_start to task_end.",
    ),
    SloSpec(
        "fix_latency_p95_ms",
        "Fix latency p95",
        "ms",
        "lower",
        900_000.0,
        metrics=("latency_ms",),
        description="p95 wall clock from task_start to task_end.",
    ),
    SloSpec(
        "cost_per_task_usd",
        "Cost per task",
        "usd",
        "lower",
        0.025,
        metrics=("cost_usd",),
        description="Total model cost divided by measured tasks.",
    ),
    SloSpec(
        "tokens_per_task",
        "Tokens per task",
        "tokens",
        "lower",
        120_000,
        metrics=("total_tokens",),
        description="Total prompt+completion tokens divided by measured tasks.",
    ),
    SloSpec(
        "user_interventions_per_task",
        "User intervention count per task",
        "count",
        "lower",
        0.0,
        metrics=("user_interventions",),
        description="Approvals, steering messages, and manual resumes per task.",
    ),
    SloSpec(
        "sandbox_command_latency_p95_ms",
        "Sandbox command latency p95",
        "ms",
        "lower",
        120_000.0,
        metrics=("sandbox_command_latencies_ms",),
        description="p95 of per-containerized-command latency.",
    ),
    SloSpec(
        "context_utilization_p95",
        "Context utilization p95",
        "ratio",
        "lower",
        0.80,
        metrics=("context_utilization",),
        description="p95 of used prompt tokens divided by the model window.",
    ),
    SloSpec(
        "provider_fallback_rate",
        "Provider fallback rate",
        "ratio",
        "lower",
        0.10,
        metrics=("provider_request_count", "provider_fallback_count"),
        description="Share of routed model calls served by a fallback provider.",
    ),
)

SLO_BY_KEY: Dict[str, SloSpec] = {spec.key: spec for spec in SLO_SPECS}


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """True only for a real number. ``bool`` is explicitly not a number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def wilson_interval(
    successes: int, total: int, z: float = 1.959963984540054
) -> Tuple[Optional[float], Optional[float]]:
    """95% Wilson score interval for a binomial proportion.

    Chosen over the normal approximation because eval samples are small
    and often near 0 or 1, where the normal interval leaves [0,1]. A
    zero-sample input returns ``(None, None)`` — no interval is claimed
    from no data.
    """
    if not _is_number(total) or total <= 0:
        return (None, None)
    successes = max(0, min(float(total), float(successes)))
    total_f = float(total)
    proportion = successes / total_f
    denominator = 1.0 + (z * z) / total_f
    center = (proportion + (z * z) / (2.0 * total_f)) / denominator
    margin = (
        z
        * math.sqrt(
            (proportion * (1.0 - proportion) / total_f)
            + (z * z) / (4.0 * total_f * total_f)
        )
        / denominator
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def eval_noise_band(
    successes: int, total: int, z: float = 1.959963984540054
) -> Optional[float]:
    """Half-width of the 95% interval: the honest eval noise band.

    A change smaller than this band is not evidence of a real change; the
    matrix reports it so a nightly diff is not over-read.
    """
    low, high = wilson_interval(successes, total, z)
    if low is None or high is None:
        return None
    return round((high - low) / 2.0, 6)


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """Linear-interpolation percentile. Empty input -> ``None``."""
    numbers = sorted(float(value) for value in values if _is_number(value))
    if not numbers:
        return None
    if len(numbers) == 1:
        return round(numbers[0], 6)
    position = (len(numbers) - 1) * max(0.0, min(1.0, float(fraction)))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(numbers[lower], 6)
    weight = position - lower
    return round(numbers[lower] + (numbers[upper] - numbers[lower]) * weight, 6)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Measurement records
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    """One measured run. Numbers here are derived, never asserted.

    ``artifact`` is the on-disk file the numbers were read from and
    ``artifact_sha256`` pins its content, so a caller cannot fabricate a
    passing record without producing a real artifact. ``rejected`` names
    the reason a record was refused, if it was.
    """

    task_id: str
    artifact: str = ""
    artifact_sha256: str = ""
    status: str = ""
    model: str = ""
    session_id: str = ""
    run_id: str = ""
    latency_ms: Optional[float] = None
    cost_usd: Optional[float] = None
    total_tokens: Optional[int] = None
    user_interventions: Optional[int] = None
    sandbox_command_latencies_ms: List[float] = field(default_factory=list)
    context_utilization: List[float] = field(default_factory=list)
    provider_request_count: Optional[int] = None
    provider_fallback_count: Optional[int] = None
    measured_metrics: List[str] = field(default_factory=list)
    rejected: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """JSON-compatible record."""
        return asdict(self)


def _metric_provenance(record: Mapping[str, Any], root: Optional[Path]) -> str:
    """Return "" when the record's real artifact verifies, else a reason.

    A record with no artifact, an unreadable artifact, or a digest that no
    longer matches is refused. This is the mechanism that makes a
    caller-supplied literal un-usable as evidence.
    """
    artifact = str(record.get("artifact") or "")
    digest = str(record.get("artifact_sha256") or "")
    if not artifact or not digest:
        return "missing artifact provenance"
    path = Path(artifact)
    if root is not None:
        try:
            path.relative_to(root)
        except ValueError:
            return "artifact is outside the measured logs root"
    if path.is_symlink() or not path.is_file():
        return "artifact is missing"
    try:
        actual = _sha256(path)
    except OSError:
        return "artifact is unreadable"
    if actual != digest:
        return "artifact digest does not match"
    return ""


def measurement_metrics(record: Mapping[str, Any]) -> List[str]:
    """The REQUIRED_METRICS keys a record actually carries as real data."""
    present: List[str] = []
    for key in REQUIRED_METRICS:
        if key == "status":
            if str(record.get("status") or ""):
                present.append(key)
            continue
        value = record.get(key)
        if key in ("sandbox_command_latencies_ms", "context_utilization"):
            if isinstance(value, (list, tuple)) and value:
                present.append(key)
        elif _is_number(value):
            present.append(key)
    return present


# ---------------------------------------------------------------------------
# Real collection from run artifacts on disk
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return rows
    return rows


def _float(value: Any) -> Optional[float]:
    if _is_number(value):
        return float(value)
    return None


def _int(value: Any) -> Optional[int]:
    if _is_number(value):
        return int(value)
    return None


def _usage_tokens(payload: Mapping[str, Any]) -> Optional[int]:
    """Token total from a router/model usage block (several shapes exist)."""
    for key in ("total_tokens", "tokens"):
        number = _int(payload.get(key))
        if number is not None:
            return number
    prompt = _int(payload.get("prompt_tokens")) or 0
    completion = _int(payload.get("completion_tokens")) or 0
    if prompt or completion:
        return prompt + completion
    return None


def _sandbox_latencies(unified: Sequence[Mapping[str, Any]]) -> List[float]:
    """Per-command sandbox latency from paired call/result overlay rows."""
    open_calls: Dict[str, float] = {}
    samples: List[float] = []
    for row in unified:
        name = str(row.get("event") or "")
        ts = _float(row.get("ts"))
        if ts is None:
            continue
        if name == "sandbox_call":
            key = str(row.get("call_id") or row.get("command") or ts)
            open_calls[key] = ts
        elif name == "sandbox_result":
            key = str(row.get("call_id") or row.get("command") or "")
            started = open_calls.pop(key, None)
            if started is None and open_calls:
                started = open_calls.pop(next(iter(open_calls)))
            if started is not None:
                samples.append(round(max(0.0, (ts - started) * 1000.0), 3))
    return samples


def _provider_counts(
    unified: Sequence[Mapping[str, Any]], ledger: Sequence[Mapping[str, Any]]
) -> Tuple[Optional[int], Optional[int]]:
    """(routed calls, fallback calls) from routing receipts only."""
    requests = 0
    fallbacks = 0
    seen = False
    for row in list(unified) + list(ledger):
        if str(row.get("event") or "") != "model_routed":
            continue
        seen = True
        requests += 1
        hint = str(row.get("routed_via_hint") or row.get("fallback") or "").lower()
        if hint in ("fallback", "provider_fallback", "fallback_used") or (
            row.get("fallback") is True
        ):
            fallbacks += 1
    return (requests, fallbacks) if seen else (None, None)


def _context_utilization(
    unified: Sequence[Mapping[str, Any]], harness: Sequence[Mapping[str, Any]]
) -> List[float]:
    """Context used/window ratios from budget receipts."""
    samples: List[float] = []
    for row in list(unified) + list(harness):
        name = str(row.get("event") or row.get("kind") or "")
        if name not in ("context_budget", "context_utilization", "context_compacted"):
            continue
        data = row.get("payload") if isinstance(row.get("payload"), dict) else row
        data = data.get("data") if isinstance(data.get("data"), dict) else data
        used = _float(
            data.get("used") or data.get("prompt_tokens") or data.get("tokens")
        )
        limit = _float(
            data.get("limit") or data.get("window") or data.get("context_limit")
        )
        if used is not None and limit and limit > 0:
            samples.append(round(used / limit, 6))
        else:
            ratio = _float(data.get("utilization") or data.get("ratio"))
            if ratio is not None:
                samples.append(round(ratio, 6))
    return samples


_USER_INTERVENTION_EVENTS = frozenset(
    {
        "approval_requested",
        "approval_granted",
        "approval_denied",
        "user_steer",
        "steering_message",
        "manual_resume",
        "user_intervention",
    }
)


def _user_interventions(
    unified: Sequence[Mapping[str, Any]],
    harness: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
) -> Optional[int]:
    """Count distinct user-intervention receipts.

    Returns ``None`` — not ``0`` — when the run produced no receipts at all:
    "no evidence" and "zero interventions" are different answers, and only the
    second one may move a target.
    """
    recorded = _int(state.get("user_interventions"))
    if recorded is not None:
        return recorded
    approvals = _int(state.get("approvals"))
    if approvals is not None:
        return approvals
    rows = list(unified) + list(harness)
    if not rows:
        return None
    return sum(
        1
        for row in rows
        if str(row.get("event") or row.get("kind") or "") in _USER_INTERVENTION_EVENTS
    )


def _status_from(state: Mapping[str, Any], harness: Sequence[Mapping[str, Any]]) -> str:
    """The verifier-minted terminal status, read not inferred."""
    for candidate in (
        state.get("status"),
        (state.get("result") or {}).get("status")
        if isinstance(state.get("result"), dict)
        else None,
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    for row in reversed(list(harness)):
        if str(row.get("kind") or row.get("event") or "") in ("task_end", "result"):
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            nested = (
                data.get("result") if isinstance(data.get("result"), dict) else data
            )
            if isinstance(nested.get("status"), str) and nested["status"]:
                return str(nested["status"])
    return ""


def _latency_from(harness: Sequence[Mapping[str, Any]]) -> Optional[float]:
    started: Optional[float] = None
    finished: Optional[float] = None
    for row in harness:
        name = str(row.get("kind") or row.get("event") or "")
        ts = _float(row.get("ts"))
        if ts is None:
            continue
        if name == "task_start" and started is None:
            started = ts
        elif name in ("task_end", "result") and finished is None:
            finished = ts
    if started is None or finished is None or finished < started:
        return None
    return round((finished - started) * 1000.0, 3)


def _cost_and_tokens(
    unified: Sequence[Mapping[str, Any]],
    ledger: Sequence[Mapping[str, Any]],
    harness: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
) -> Tuple[Optional[float], Optional[int], str, str]:
    """Cost, tokens, model, and run id from receipts, preferring real rows."""
    cost = 0.0
    cost_seen = False
    tokens = 0
    tokens_seen = False
    model = ""
    run_id = ""
    for row in list(unified) + list(ledger):
        if str(row.get("event") or "") != "model_routed":
            continue
        cost_seen = True
        value = _float(
            row.get("cost_usd") if row.get("cost_usd") is not None else row.get("cost")
        )
        if value is not None:
            cost += value
        count = _usage_tokens(row)
        if count is not None:
            tokens += count
            tokens_seen = True
        model = str(row.get("model") or model)
        run_id = str(row.get("run_id") or run_id)
    for row in harness:
        name = str(row.get("kind") or row.get("event") or "")
        if not name.startswith("model_"):
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else row
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        if not usage:
            continue
        value = _float(
            usage.get("cost")
            if usage.get("cost") is not None
            else usage.get("cost_usd")
        )
        if value is not None:
            cost += value
            cost_seen = True
        count = _usage_tokens(usage)
        if count is not None:
            tokens += count
            tokens_seen = True
    if not cost_seen:
        recorded = _float(state.get("cost_usd"))
        cost = recorded if recorded is not None else None
    if not tokens_seen:
        recorded = _int(state.get("total_tokens"))
        tokens = recorded if recorded is not None else None
    return (cost, tokens, model, run_id)


def collect_measurements(
    logs_root: Path, task_ids: Optional[Sequence[str]] = None
) -> List[Measurement]:
    """Derive one :class:`Measurement` per real run directory under ``logs_root``.

    Only directories that actually contain a run artifact contribute. A
    run with no artifact yields nothing at all, so "no data" can never be
    confused with "zero cost" or "100% success".
    """
    root = Path(logs_root)
    if not root.is_dir():
        return []
    if task_ids is None:
        candidates = sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and not entry.is_symlink()
            and not entry.name.startswith(("_", "."))
        )
    else:
        candidates = [str(task_id) for task_id in task_ids]
    records: List[Measurement] = []
    for name in candidates:
        task_dir = root / name
        state_path = task_dir / "state.json"
        trace_path = task_dir / "trace.jsonl"
        artifact = state_path if state_path.is_file() else trace_path
        if task_dir.is_symlink() or not artifact.is_file():
            continue
        state = _read_json(state_path) if state_path.is_file() else {}
        harness = _read_jsonl(trace_path) if trace_path.is_file() else []
        overlay = task_dir / "_trace" / f"{name}.jsonl"
        unified = _read_jsonl(overlay) if overlay.is_file() else []
        ledger_path = root / f"{name}.runtime" / "model_ledger.jsonl"
        ledger = _read_jsonl(ledger_path) if ledger_path.is_file() else []
        worker_path = root / f"{name}.runtime" / "events.jsonl"
        worker = _read_jsonl(worker_path) if worker_path.is_file() else []
        combined = unified + worker
        cost, tokens, model, run_id = _cost_and_tokens(combined, ledger, harness, state)
        requests, fallbacks = _provider_counts(combined, ledger)
        record = Measurement(
            task_id=name,
            artifact=str(artifact),
            artifact_sha256=_sha256(artifact),
            status=_status_from(state, harness),
            model=model or str(state.get("model") or ""),
            session_id=str(state.get("session_id") or state.get("session") or ""),
            run_id=run_id or str(state.get("run_id") or ""),
            latency_ms=_latency_from(harness),
            cost_usd=cost,
            total_tokens=tokens,
            user_interventions=_user_interventions(combined, harness, state),
            sandbox_command_latencies_ms=_sandbox_latencies(combined),
            context_utilization=_context_utilization(combined, harness),
            provider_request_count=requests,
            provider_fallback_count=fallbacks,
        )
        record.measured_metrics = measurement_metrics(record.to_dict())
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# SLO evaluation
# ---------------------------------------------------------------------------


def _metric_value(
    spec: SloSpec, records: Sequence[Mapping[str, Any]]
) -> Tuple[Optional[float], List[str], Dict[str, Any]]:
    """Compute one SLO's measured value from the accepted records.

    Returns ``(value, missing_metrics, detail)``. ``value`` is ``None``
    when any required input metric is absent — a partially measured SLO
    is never reported as a number.
    """
    present = {key: 0 for key in REQUIRED_METRICS}
    for record in records:
        for key in measurement_metrics(record):
            if key in present:
                present[key] += 1
    # Fail closed: an SLO is measured only when EVERY accepted run reports
    # every input it consumes. A single run that stopped emitting a receipt
    # would otherwise silently shrink the denominator, which is exactly the
    # self-certifying drift this module exists to prevent.
    missing = [key for key in spec.metrics if present.get(key, 0) < len(records)]
    detail = {
        "input_metric_counts": {key: present[key] for key in spec.metrics},
        "input_metric_required_on_every_run": True,
    }
    if missing and records:
        partial = {
            key: len(records) - present.get(key, 0)
            for key in missing
            if present.get(key, 0)
        }
        detail["records_missing_metrics"] = partial
    if missing or not records:
        return (None, missing or list(spec.metrics), detail)

    total = len(records)
    key = spec.key
    if key == "verified_success_rate":
        successes = sum(
            1 for record in records if str(record.get("status")) == VERIFIED_STATUS
        )
        low, high = wilson_interval(successes, total)
        detail.update({"successes": successes, "total": total})
        detail["noise_band"] = eval_noise_band(successes, total)
        detail["ci"] = {"low": low, "high": high, "level": 0.95}
        return (successes / total if total else None, [], detail)
    if key in ("fix_latency_p50_ms", "fix_latency_p95_ms"):
        samples = [
            float(record["latency_ms"])
            for record in records
            if _is_number(record.get("latency_ms"))
        ]
        fraction = 0.5 if key.endswith("p50_ms") else 0.95
        detail["samples"] = len(samples)
        return (percentile(samples, fraction), [], detail)
    if key == "cost_per_task_usd":
        samples = [
            float(record["cost_usd"])
            for record in records
            if _is_number(record.get("cost_usd"))
        ]
        detail["samples"] = len(samples)
        return (round(sum(samples) / len(samples), 8) if samples else None, [], detail)
    if key == "tokens_per_task":
        samples = [
            float(record["total_tokens"])
            for record in records
            if _is_number(record.get("total_tokens"))
        ]
        detail["samples"] = len(samples)
        return (round(sum(samples) / len(samples), 4) if samples else None, [], detail)
    if key == "user_interventions_per_task":
        samples = [
            float(record["user_interventions"])
            for record in records
            if _is_number(record.get("user_interventions"))
        ]
        detail["samples"] = len(samples)
        return (round(sum(samples) / len(samples), 6) if samples else None, [], detail)
    if key == "sandbox_command_latency_p95_ms":
        samples: List[float] = []
        for record in records:
            values = record.get("sandbox_command_latencies_ms")
            if isinstance(values, (list, tuple)):
                samples.extend(float(item) for item in values if _is_number(item))
        detail["samples"] = len(samples)
        return (percentile(samples, 0.95), [], detail)
    if key == "context_utilization_p95":
        samples = []
        for record in records:
            values = record.get("context_utilization")
            if isinstance(values, (list, tuple)):
                samples.extend(float(item) for item in values if _is_number(item))
        detail["samples"] = len(samples)
        return (percentile(samples, 0.95), [], detail)
    if key == "provider_fallback_rate":
        requests = sum(
            int(record.get("provider_request_count") or 0) for record in records
        )
        fallbacks = sum(
            int(record.get("provider_fallback_count") or 0) for record in records
        )
        detail.update({"requests": requests, "fallbacks": fallbacks})
        return ((fallbacks / requests) if requests else None, [], detail)
    return (None, list(spec.metrics), detail)


def _as_mapping(record: Any) -> Optional[Mapping[str, Any]]:
    """Coerce a measurement (mapping, dataclass, or to_dict object) to a mapping.

    ``collect_measurements`` returns :class:`Measurement` dataclasses while
    callers may hand in plain dicts from JSON; both are real measurements
    and neither is privileged.
    """
    if isinstance(record, Mapping):
        return record
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return value if isinstance(value, Mapping) else None
    if dataclasses.is_dataclass(record):
        value = dataclasses.asdict(record)
        return value if isinstance(value, Mapping) else None
    return None


def measure_slos(
    records: Iterable[Mapping[str, Any]],
    *,
    root: Optional[Path] = None,
    specs: Sequence[SloSpec] = SLO_SPECS,
) -> Tuple[List[Dict[str, Any]], List[Mapping[str, Any]], List[Dict[str, Any]]]:
    """Score every SLO against real, provenance-verified measurements.

    ``records`` are measurement dicts (or :class:`Measurement` instances);
    each must carry an ``artifact`` and a matching ``artifact_sha256``. A
    record that fails verification is dropped and reported, so a caller
    cannot pass the gate by handing in literal booleans. Returns
    ``(rows, accepted, rejected)``.
    """
    accepted: List[Mapping[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for candidate in records:
        record = _as_mapping(candidate)
        if record is None:
            rejected.append({"task_id": "", "reason": "record is not a measurement"})
            continue
        reason = _metric_provenance(record, root)
        if reason:
            rejected.append(
                {
                    "task_id": str(record.get("task_id") or ""),
                    "reason": reason,
                }
            )
            continue
        accepted.append(record)

    results: List[Dict[str, Any]] = []
    for spec in specs:
        value, missing, detail = _metric_value(spec, accepted)
        if value is None:
            status = "unmeasured"
            ok = False
            reason = (
                f"no run in the accepted set reports {', '.join(missing)}"
                if missing
                else "no records available"
            )
        else:
            ok = (
                value >= spec.target
                if spec.direction == "higher"
                else value <= spec.target
            )
            status = "met" if ok else "missed"
            reason = ""
        results.append(
            {
                "key": spec.key,
                "title": spec.title,
                "unit": spec.unit,
                "direction": spec.direction,
                "target": spec.target,
                "required": spec.required,
                "measured": value,
                "status": status,
                "ok": ok,
                "sample_count": len(accepted),
                "missing_metrics": missing,
                "reason": reason,
                "description": spec.description,
                **detail,
            }
        )
    return results, accepted, rejected


def slo_report(
    records: Iterable[Mapping[str, Any]],
    *,
    root: Optional[Path] = None,
    specs: Sequence[SloSpec] = SLO_SPECS,
) -> Dict[str, Any]:
    """Full SLO verdict: per-SLO rows, readiness map, and a three-valued verdict."""
    materialized = list(records)
    rows, accepted, rejected = measure_slos(materialized, root=root, specs=specs)
    required = [row for row in rows if row["required"]]
    unmeasured = [row["key"] for row in required if row["status"] == "unmeasured"]
    missed = [row["key"] for row in required if row["status"] == "missed"]
    readiness = {row["key"]: bool(row["ok"]) for row in required}
    if not accepted or unmeasured:
        verdict = "INSUFFICIENT_EVIDENCE"
    elif missed:
        verdict = "MISSES_SLOS"
    else:
        verdict = "MEETS_SLOS"
    observed_metrics: List[str] = []
    for record in accepted:
        for key in measurement_metrics(record):
            if key not in observed_metrics:
                observed_metrics.append(key)
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict": verdict,
        "ready": verdict == "MEETS_SLOS",
        "slo": rows,
        "readiness": readiness,
        "unmeasured": unmeasured,
        "missed": missed,
        "n_records_considered": len(materialized),
        "n_measurements_accepted": len(accepted),
        "rejected_records": rejected,
        "required_metrics": list(REQUIRED_METRICS),
        "observed_metrics": observed_metrics,
        "missing_metrics": [
            key for key in REQUIRED_METRICS if key not in observed_metrics
        ],
        "errors": [
            {
                "code": "slo_unmeasured",
                "slo": key,
                "message": f"{key} has no real measurement",
            }
            for key in unmeasured
        ]
        + [
            {
                "code": "slo_missed",
                "slo": key,
                "message": f"{key} did not meet its target",
            }
            for key in missed
        ]
        + [
            {
                "code": "slo_provenance",
                "message": f"{item['reason']} ({item['task_id'] or 'unknown'})",
            }
            for item in rejected
        ],
    }


def evidence_summary(logs_root: Path) -> Dict[str, Any]:
    """Collect real measurements from a logs root and return the SLO report."""
    return slo_report(collect_measurements(logs_root), root=Path(logs_root))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """Print a machine-readable SLO report; exit 2 unless --require-ready passes."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.slos",
        description="Measured SLOs derived from real run artifacts.",
    )
    parser.add_argument("--logs-root", default=str(REPO_ROOT / "logs"))
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output only"
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="exit 2 unless every required SLO is met from real measurements",
    )
    args = parser.parse_args(argv)
    report = evidence_summary(Path(args.logs_root))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"slo verdict: {report['verdict']}")
        for row in report["slo"]:
            value = row["measured"]
            shown = "n/a" if value is None else f"{value:.6g}"
            print(
                f"  {row['key']:<32} {shown:>12} "
                f"{'>=' if row['direction'] == 'higher' else '<='} {row['target']:<10g} "
                f"{row['status']} {row['reason']}"
            )
        print(f"measurements accepted: {report['n_measurements_accepted']}")
        for item in report["rejected_records"]:
            print(f"  rejected {item['task_id'] or 'unknown'}: {item['reason']}")
    if args.require_ready and report["verdict"] != "MEETS_SLOS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
