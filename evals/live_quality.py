"""Live quality evaluation: a nightly matrix judged on real model output.

The scripted arms in :mod:`evals.run` prove the *machinery* did not
regress. They cannot say anything about whether the agent's work is good.
This module closes that gap with a real, nightly, multi-provider quality
matrix plus a judge that scores four dimensions of the work:

    correctness | minimality | explanation_quality | user_acceptance

Honesty rules this module is built around:

* The matrix is **incomplete without >= 20 real tasks and >= 2 providers**.
  Anything less reports ``meets_matrix_minimum: false`` and is not a pass.
* Without provider credentials the runner reports ``blocked`` — never
  ``complete``, never a pass, never a zero score presented as quality.
* A judge that is not a live model is labelled ``judge_mode:
  "deterministic"`` and cannot be reported as a live quality result.
* Every real failure that survives judging is **mined into a permanent
  regression case** (``evals/regression_cases.json``) which then becomes
  part of the nightly task set. A failure is not allowed to disappear.

CLI:
    python -m evals.live_quality --providers openai:model,anthropic:model
    python -m evals.live_quality --summarize <samples.json> --json
    python -m evals.live_quality --mine <samples.json> --json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from evals.slos import eval_noise_band, wilson_interval

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Minimum real tasks and providers for a nightly matrix to count.
MIN_MATRIX_TASKS = 20
MIN_MATRIX_PROVIDERS = 2

#: The four judged dimensions. A quality claim is a vector, not a number.
JUDGE_DIMENSIONS: Tuple[str, ...] = (
    "correctness",
    "minimality",
    "explanation_quality",
    "user_acceptance",
)

REGISTRY_NAME = "regression_cases.json"


@dataclass(frozen=True)
class LiveTask:
    """One real repository task in the nightly quality matrix.

    ``grounding`` lists strings a correct answer must actually cite, so
    the judge can measure correctness from output instead of trusting the
    model's own claim. ``mutation`` plants a real defect in a throwaway
    copy of the repository, which makes the fix tasks genuine bug fixes
    rather than prose exercises.
    """

    task_id: str
    title: str
    kind: str
    request: str
    files: Tuple[str, ...]
    grounding: Tuple[str, ...] = ()
    mutation: Optional[Tuple[str, str]] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-compatible task definition."""
        return asdict(self)


def _task(*args: Any, **kwargs: Any) -> LiveTask:
    return LiveTask(*args, **kwargs)


#: 20 real tasks drawn from this repository's actual code. They are fixed
#: so nightly runs are comparable; mined failures are appended to the
#: registry and join the same matrix.
LIVE_QUALITY_TASKS: Tuple[LiveTask, ...] = (
    _task(
        "lq_01_trace_reader_contract",
        "Explain the trace reconstruction contract",
        "explain",
        "Explain how shared.traceview reconstructs a task lifecycle and which "
        "sources it merges. Cite the module and the merge function.",
        ("shared/traceview.py",),
        ("reconstruct_task", "trace.jsonl"),
    ),
    _task(
        "lq_02_span_correlation",
        "Explain GenAI span correlation",
        "explain",
        "Explain how a GenAI span is correlated to a run, task, session, and "
        "model in this codebase. Cite the source.",
        ("shared/tracing.py",),
        ("span_id", "trace_id", "session_id"),
    ),
    _task(
        "lq_03_slo_measurement",
        "Explain how SLOs avoid boolean self-certification",
        "explain",
        "Explain how this repository prevents an SLO from being satisfied by a "
        "literal boolean. Name the mechanism and the module.",
        ("evals/slos.py",),
        ("artifact_sha256", "unmeasured"),
    ),
    _task(
        "lq_04_wilson_interval",
        "Explain the Wilson interval choice",
        "explain",
        "Explain why a Wilson score interval is used for the live success rate "
        "instead of a normal approximation. Cite the function.",
        ("evals/slos.py",),
        ("wilson_interval",),
    ),
    _task(
        "lq_05_verifier_authority",
        "Explain who mints verified success",
        "explain",
        "Explain in this repository which component mints a verified success "
        "status and how completed_unverified is prevented from rendering as "
        "success. Cite the code.",
        ("execution/verify.py",),
        ("completed_verified",),
    ),
    _task(
        "lq_06_sandbox_boundary",
        "Explain the sandbox execution boundary",
        "explain",
        "Explain how a command is executed inside the Docker sandbox and what "
        "mounts are allowed. Cite the module.",
        ("execution/sandbox.py",),
        ("docker",),
    ),
    _task(
        "lq_07_router_adaptivity",
        "Explain adaptive model routing",
        "explain",
        "Explain how difficulty prediction changes model selection. Cite the "
        "router and the difficulty module.",
        ("runtime/model_router.py", "runtime/difficulty.py"),
        ("difficulty",),
    ),
    _task(
        "lq_08_context_compiler",
        "Explain the context compiler",
        "explain",
        "Explain what ContextCompiler produces and where it is consumed. Cite "
        "the source.",
        ("harness/context_compiler.py",),
        ("ContextCompiler",),
    ),
    _task(
        "lq_09_retrieval_symbols",
        "Explain symbol retrieval",
        "explain",
        "Explain how symbol-level retrieval differs from substring search in "
        "this codebase. Cite the retrieval module.",
        ("harness/retrieval.py",),
        ("symbol",),
    ),
    _task(
        "lq_10_lsp_contract",
        "Explain the LSP lifecycle",
        "explain",
        "Explain the language-server lifecycle this repository drives: "
        "initialize, open, diagnose, change, shutdown. Cite the module.",
        ("harness/lsp.py",),
        ("LspManager",),
    ),
    _task(
        "lq_11_checkpoint_resume",
        "Explain hard-kill resume",
        "explain",
        "Explain what state a hard-killed run must retain to resume correctly. "
        "Cite the checkpoint module.",
        ("runtime/checkpoint.py",),
        ("checkpoint",),
    ),
    _task(
        "lq_12_orchestration_dag",
        "Explain the orchestration DAG",
        "explain",
        "Explain the worktree-isolated task DAG and how symbol claims prevent "
        "lost edits. Cite the source.",
        ("runtime/orchestration.py",),
        ("claim",),
    ),
    _task(
        "lq_13_approval_integrity",
        "Explain approval binding",
        "explain",
        "Explain how an approval is bound to the effect that actually runs. "
        "Cite the code.",
        ("runtime/approval.py",),
        ("approval",),
    ),
    _task(
        "lq_14_secret_redaction",
        "Explain secret redaction",
        "explain",
        "Explain the single redaction implementation shared by trace, overlay, "
        "diff, and memory. Cite the module.",
        ("shared/security.py",),
        ("redact",),
    ),
    _task(
        "lq_15_evidence_bundle",
        "Explain evidence retention",
        "explain",
        "Explain how a release evidence bundle is sanitized and verified before "
        "it is committed. Cite the module.",
        ("evals/evidence.py",),
        ("bundle",),
    ),
    _task(
        "lq_16_ci_truth",
        "Explain the CI truth gate",
        "explain",
        "Explain how this repository proves every test file runs in some "
        "workflow or allowlist. Cite the module.",
        ("evals/ci_truth.py",),
        ("allowlist",),
    ),
    _task(
        "lq_17_daily_driver_matrix",
        "Explain the daily-driver matrix",
        "explain",
        "Explain what the daily-driver matrix asserts and why a passing subset "
        "is not readiness. Cite the module.",
        ("evals/daily_driver.py",),
        ("readiness",),
    ),
    _task(
        "lq_18_prompt_regression_arms",
        "Explain the paired arms",
        "explain",
        "Explain how a prompt-regression arm differs from a code fork and what "
        "a regression means. Cite the runner.",
        ("evals/run.py",),
        ("baseline",),
    ),
    _task(
        "lq_19_repair_broken_test",
        "Repair a real broken assertion",
        "fix",
        "The helper below has a real bug: it returns the wrong value for empty "
        "input. Fix it with the smallest change and keep the existing test "
        "contract.",
        ("live_quality_fixture.py",),
        ("empty",),
        (("live_quality_fixture.py", "app.py"), None),
    ),
    _task(
        "lq_20_off_by_one_repair",
        "Repair a real off-by-one",
        "fix",
        "The slice helper below drops the last element. Fix it minimally.",
        ("live_quality_fixture.py",),
        ("last",),
    ),
)

#: The file a fix task repairs. Planted into a throwaway repo copy so the
#: real repository is never mutated by an evaluation run.
FIXTURE_MODULE = """\
def total(prices):
    return sum(prices)


def is_empty(values):
    return len(values) == 0


def head(values):
    return values[:-1]
"""


def regression_registry_path(root: Optional[Path] = None) -> Path:
    """Where mined regression cases are persisted (a committed artifact)."""
    return Path(root or REPO_ROOT) / "evals" / REGISTRY_NAME


def live_task_set(root: Optional[Path] = None) -> List[LiveTask]:
    """The fixed task set plus every mined regression case, in stable order."""
    tasks = list(LIVE_QUALITY_TASKS)
    for case in load_regression_cases(root):
        tasks.append(
            _task(
                str(case.get("task_id") or "regression_unknown"),
                str(case.get("title") or "mined regression case"),
                "regression",
                str(case.get("request") or ""),
                tuple(case.get("files") or ()),
                tuple(case.get("grounding") or ()),
            )
        )
    return tasks


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

_DIMENSION_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "explanation_quality": ("because", "therefore", "so that", "which means", "first"),
}


def _citations(answer: str) -> List[str]:
    """File-like tokens the answer actually cites."""
    return sorted(
        {match.rstrip(".,;:)`") for match in re.findall(r"[\w./-]+\.py", answer)}
    )


def deterministic_judge(
    task: LiveTask, answer: str, changed_files: Sequence[str]
) -> Dict[str, Any]:
    """Evidence-based judge used when no live judge model is available.

    This is a *measurable* rubric (grounding coverage, edit size, citation
    structure), not a model opinion — and it is always labelled
    ``judge_mode="deterministic"`` so it can never be reported as a live
    quality score.
    """
    text = str(answer or "")
    lowered = text.casefold()
    grounding = [marker for marker in task.grounding if marker.casefold() in lowered]
    grounding_ratio = (len(grounding) / len(task.grounding)) if task.grounding else 1.0
    citations = _citations(text)
    cited_known = [
        name
        for name in citations
        if any(name.endswith(str(item)) for item in task.files)
    ]
    length = len(text)
    minimality = 1.0 if length <= 2000 else max(0.0, 1.0 - (length - 2000) / 8000.0)
    if task.kind == "fix":
        minimality = (
            1.0 if 0 < len(changed_files) <= 2 else (0.6 if changed_files else 0.0)
        )
        if grounding_ratio < 1.0:
            minimality = min(minimality, 0.3)
    structure = sum(
        1
        for pattern in _DIMENSION_PATTERNS["explanation_quality"]
        if pattern in lowered
    )
    explanation = min(
        1.0, 0.4 * grounding_ratio + 0.2 * bool(cited_known) + 0.1 * structure
    )
    if length < 40:
        explanation = min(explanation, 0.2)
    acceptance = (
        1.0
        if grounding_ratio >= 1.0
        and explanation >= 0.5
        and (task.kind != "fix" or changed_files)
        else (0.4 if grounding_ratio >= 0.5 else 0.0)
    )
    return {
        "judge_mode": "deterministic",
        "live_judge": False,
        "scores": {
            "correctness": round(grounding_ratio, 4),
            "minimality": round(minimality, 4),
            "explanation_quality": round(explanation, 4),
            "user_acceptance": round(acceptance, 4),
        },
        "grounding_hits": grounding,
        "grounding_misses": [
            marker for marker in task.grounding if marker not in grounding
        ],
        "citations": citations,
        "changed_files": list(changed_files),
    }


def judge_sample(
    task: LiveTask,
    answer: str,
    changed_files: Sequence[str] = (),
    judge_call: Optional[
        Callable[[LiveTask, str, Sequence[str]], Dict[str, Any]]
    ] = None,
) -> Dict[str, Any]:
    """Score one live answer on the four judged dimensions.

    ``judge_call`` is the live judge (a provider-backed callable). When it
    is absent the deterministic rubric runs instead and the verdict is
    explicitly marked non-live.
    """
    if judge_call is None:
        return deterministic_judge(task, answer, changed_files)
    raw = judge_call(task, answer, changed_files)
    scores = dict(raw.get("scores") or {}) if isinstance(raw, Mapping) else {}
    normalized: Dict[str, float] = {}
    for dimension in JUDGE_DIMENSIONS:
        value = scores.get(dimension)
        normalized[dimension] = round(float(value), 4) if _is_number(value) else 0.0
    accepted = all(
        normalized.get(dim, 0.0) >= 0.7 for dim in ("correctness", "user_acceptance")
    )
    return {
        "judge_mode": "live",
        "live_judge": True,
        "scores": normalized,
        "accepted": accepted,
        "rationale": str(raw.get("rationale") or "")
        if isinstance(raw, Mapping)
        else "",
        "citations": _citations(str(answer or "")),
        "changed_files": list(changed_files),
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Matrix aggregation
# ---------------------------------------------------------------------------


def summarize_live_matrix(
    samples: Sequence[Mapping[str, Any]],
    tasks: Sequence[LiveTask] = LIVE_QUALITY_TASKS,
    providers: Sequence[str] = (),
) -> Dict[str, Any]:
    """Aggregate observed samples into the nightly quality report.

    Records ``n``, the providers covered, the Wilson interval for the
    success rate, the eval noise band, and cost/task. A matrix below the
    minimum task/provider count is reported as incomplete rather than as
    a quality result.
    """
    rows = [dict(sample) for sample in samples if isinstance(sample, Mapping)]
    task_ids = [str(sample.get("task_id") or "") for sample in rows]
    observed_providers = sorted(
        {str(sample.get("provider") or "unknown") for sample in rows}
    )
    declared = sorted({str(item) for item in providers if str(item)})
    covered = sorted(set(observed_providers) | set(declared))
    successes = sum(1 for sample in rows if _is_success(sample))
    n = len(rows)
    low, high = wilson_interval(successes, n)
    cost_values = [
        float(sample["cost_usd"])
        for sample in rows
        if _is_number(sample.get("cost_usd"))
    ]
    token_values = [
        float(sample["total_tokens"])
        for sample in rows
        if _is_number(sample.get("total_tokens"))
    ]
    judged = [sample for sample in rows if isinstance(sample.get("judge"), Mapping)]
    dimensions: Dict[str, Dict[str, Any]] = {}
    for dimension in JUDGE_DIMENSIONS:
        values = [
            float(sample["judge"]["scores"][dimension])
            for sample in judged
            if isinstance(sample.get("judge"), Mapping)
            and isinstance(sample["judge"].get("scores"), Mapping)
            and _is_number(sample["judge"]["scores"].get(dimension))
        ]
        mean = round(sum(values) / len(values), 4) if values else None
        dimensions[dimension] = {
            "mean": mean,
            "n": len(values),
            "threshold": 0.7,
            "ok": bool(values) and mean is not None and mean >= 0.7,
        }
    per_provider: Dict[str, Dict[str, Any]] = {}
    for provider in observed_providers:
        subset = [sample for sample in rows if str(sample.get("provider")) == provider]
        ok = sum(1 for sample in subset if _is_success(sample))
        provider_low, provider_high = wilson_interval(ok, len(subset))
        per_provider[provider] = {
            "n": len(subset),
            "successes": ok,
            "success_rate": round(ok / len(subset), 6) if subset else None,
            "success_ci": {"low": provider_low, "high": provider_high, "level": 0.95},
            "noise_band": eval_noise_band(ok, len(subset)),
            "cost_per_task_usd": (
                round(
                    sum(
                        float(sample["cost_usd"])
                        for sample in subset
                        if _is_number(sample.get("cost_usd"))
                    )
                    / len(subset),
                    8,
                )
                if subset
                else None
            ),
        }
    meets_minimum = bool(
        len(tasks) >= MIN_MATRIX_TASKS and len(covered) >= MIN_MATRIX_PROVIDERS
    )
    return {
        "schema_version": 1,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n": n,
        "n_tasks": len(tasks),
        "n_task_ids_observed": len(set(task_ids)),
        "providers": covered,
        "provider_count": len(covered),
        "min_matrix_tasks": MIN_MATRIX_TASKS,
        "min_matrix_providers": MIN_MATRIX_PROVIDERS,
        "meets_matrix_minimum": meets_minimum,
        "successes": successes,
        "success_rate": round(successes / n, 6) if n else None,
        "success_ci": {
            "low": low,
            "high": high,
            "level": 0.95,
            "n": n,
            "successes": successes,
        },
        "eval_noise_band": eval_noise_band(successes, n),
        "cost_per_task": round(sum(cost_values) / n, 8) if n and cost_values else None,
        "cost_per_task_usd": round(sum(cost_values) / n, 8)
        if n and cost_values
        else None,
        "tokens_per_task": round(sum(token_values) / n, 4)
        if n and token_values
        else None,
        "total_cost_usd": round(sum(cost_values), 8) if cost_values else 0.0,
        "judge_dimensions": dimensions,
        "per_provider": per_provider,
        "live_judged_samples": sum(
            1 for sample in judged if sample["judge"].get("live_judge") is True
        ),
        "deterministic_judged_samples": sum(
            1 for sample in judged if sample["judge"].get("live_judge") is not True
        ),
        "unscored_task_ids": sorted(
            set(task_ids) - {str(getattr(t, "task_id", "")) for t in tasks}
        ),
    }


def _is_success(sample: Mapping[str, Any]) -> bool:
    """A sample counts as a success only with a verifier-minted status."""
    if str(sample.get("status")) not in ("completed_verified", "success", "completed"):
        return False
    judge = sample.get("judge")
    if isinstance(judge, Mapping):
        scores = judge.get("scores")
        if not isinstance(scores, Mapping):
            return False
        correctness = scores.get("correctness")
        return _is_number(correctness) and float(correctness) >= 0.7
    return False


# ---------------------------------------------------------------------------
# Failure mining — a real failure becomes a permanent regression case
# ---------------------------------------------------------------------------


def mine_failures(samples: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Turn observed failing samples into storable regression cases.

    A failure is one real observed run that did not reach an accepted
    outcome. Deduplicated by ``(task_id, provider, reason)`` so a nightly
    re-run does not multiply the registry.
    """
    mined: List[Dict[str, Any]] = []
    seen = set()
    for sample in samples:
        if not isinstance(sample, Mapping) or _is_success(sample):
            continue
        task_id = str(sample.get("task_id") or "unknown_task")
        provider = str(sample.get("provider") or "unknown_provider")
        judge = sample.get("judge") if isinstance(sample.get("judge"), Mapping) else {}
        misses = list(judge.get("grounding_misses") or [])
        reason = str(
            sample.get("error_class")
            or (
                "grounding_miss:" + ",".join(misses)
                if misses
                else sample.get("status") or "failed"
            )
        )
        fingerprint = f"{task_id}|{provider}|{reason}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        mined.append(
            {
                "task_id": task_id,
                "title": f"mined regression: {task_id} on {provider}",
                "provider": provider,
                "model": str(sample.get("model") or ""),
                "status": str(sample.get("status") or ""),
                "reason": reason,
                "grounding_misses": misses,
                "files": list(sample.get("files") or []),
                "grounding": list(sample.get("grounding") or []),
                "request": str(sample.get("request") or ""),
                "observed_at": str(sample.get("observed_at") or sample.get("ts") or ""),
                "fingerprint": fingerprint,
                "regression": True,
            }
        )
    return mined


def load_regression_cases(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every permanently recorded regression case (empty when none exist)."""
    path = regression_registry_path(root)
    if path.is_symlink() or not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(value, Mapping):
        value = value.get("cases", [])
    return (
        [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, list)
        else []
    )


def record_failures(
    failures: Sequence[Mapping[str, Any]],
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append mined failures to the permanent regression registry.

    Idempotent per fingerprint. The registry is a committed, reviewable
    artifact — a mined failure is never allowed to be log-only.
    """
    path = regression_registry_path(root)
    existing = load_regression_cases(root)
    known = {str(case.get("fingerprint")) for case in existing}
    added: List[Dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        fingerprint = str(failure.get("fingerprint") or "")
        if not fingerprint or fingerprint in known:
            continue
        known.add(fingerprint)
        added.append(dict(failure))
    payload = {
        "schema_version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "case_count": len(existing) + len(added),
        "cases": existing + added,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "registry_path": str(path),
        "added": added,
        "added_count": len(added),
        "skipped_duplicates": len(list(failures)) - len(added),
        "case_count": payload["case_count"],
    }


# ---------------------------------------------------------------------------
# Live matrix runner
# ---------------------------------------------------------------------------


@dataclass
class ProviderTarget:
    """One provider/model the nightly matrix must cover."""

    provider: str
    model: str
    key_env: str = "NEO_API_KEY"

    def to_dict(self) -> Dict[str, Any]:
        """JSON-compatible target."""
        return asdict(self)


def preflight(targets: Sequence[ProviderTarget]) -> Dict[str, Any]:
    """Resolve provider targets without reading or serializing credentials."""
    import os

    resolved: List[Dict[str, Any]] = []
    for target in targets:
        key_env = str(target.key_env or "NEO_API_KEY")
        present = bool(os.environ.get(key_env))
        blocked = []
        if not target.model:
            blocked.append("model is not configured")
        if not present:
            blocked.append(f"credential env {key_env} is not set")
        resolved.append(
            {
                "provider": target.provider,
                "model": target.model,
                "credential_env": key_env,
                "credential_present": present,
                "status": "blocked" if blocked else "ready",
                "blocked_reasons": blocked,
            }
        )
    blocked = [item for item in resolved if item["status"] == "blocked"]
    return {
        "targets": resolved,
        "ready": [item for item in resolved if item["status"] == "ready"],
        "blocked": blocked,
        "status": "ready" if resolved and not blocked else "blocked",
        "credential_values_read": False,
    }


def _prepare_repo(task: LiveTask, root: Path) -> Path:
    """Materialize a real, disposable repository view for one task."""
    repo = root / task.task_id
    repo.mkdir(parents=True, exist_ok=True)
    for relative in task.files:
        source = REPO_ROOT / relative
        if source.is_file() and not source.is_symlink():
            destination = repo / Path(relative).name
            shutil.copyfile(source, destination)
    (repo / "live_quality_fixture.py").write_text(FIXTURE_MODULE, encoding="utf-8")
    return repo


def _run_sample(
    task: LiveTask,
    target: ProviderTarget,
    workdir: Path,
    judge_call: Optional[Callable[[LiveTask, str, Sequence[str]], Dict[str, Any]]],
    timeout: float = 300.0,
) -> Dict[str, Any]:
    """Run one (task, provider) cell against a real model, honestly.

    Any provider failure is recorded as a failed sample with its error
    class — never silently dropped from the denominator.
    """
    repo = _prepare_repo(task, workdir)
    sample: Dict[str, Any] = {
        "task_id": task.task_id,
        "provider": target.provider,
        "model": target.model,
        "status": "",
        "answer": "",
        "files": [Path(item).name for item in task.files],
        "grounding": list(task.grounding),
        "request": task.request,
        "cost_usd": None,
        "total_tokens": None,
        "latency_ms": None,
        "changed_files": [],
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    started = time.monotonic()
    try:
        from harness.qa_mode import run_question

        result = run_question(
            task.request,
            str(repo),
            config={
                "provider": target.provider,
                "model": target.model,
                "api_key": _credential(target.key_env),
                "adaptive_routing": False,
            },
            log_root=str(workdir / task.task_id / "logs"),
            task_id=f"live-quality-{task.task_id}",
        )
    except Exception as exc:  # provider/transport failure is a real result
        sample["status"] = "failed"
        sample["error_class"] = type(exc).__name__
        sample["latency_ms"] = round((time.monotonic() - started) * 1000.0, 3)
        sample["judge"] = judge_sample(task, "", (), judge_call)
        return sample
    calls = (
        result.get("model_calls") if isinstance(result.get("model_calls"), list) else []
    )
    sample["answer"] = str(result.get("answer") or "")
    sample["status"] = "success" if str(result.get("status")) == "success" else "failed"
    sample["cost_usd"] = float(result.get("cost_usd", 0.0) or 0.0)
    sample["total_tokens"] = sum(
        int(call.get("tokens", 0) or 0) for call in calls if isinstance(call, Mapping)
    )
    sample["latency_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    sample["changed_files"] = list(result.get("files") or [])
    sample["judge"] = judge_sample(
        task, sample["answer"], sample["changed_files"], judge_call
    )
    return sample


def _credential(key_env: str) -> str:
    import os

    return str(os.environ.get(str(key_env or "NEO_API_KEY"), "") or "")


def parse_targets(spec: str) -> List[ProviderTarget]:
    """Parse ``provider:model`` (comma separated) into targets."""
    targets: List[ProviderTarget] = []
    for chunk in str(spec or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        provider, _, model = item.partition(":")
        targets.append(ProviderTarget(provider.strip(), model.strip() or "default"))
    return targets


def run_live_matrix(
    targets: Sequence[ProviderTarget],
    *,
    workdir: Optional[Path] = None,
    tasks: Optional[Sequence[LiveTask]] = None,
    judge_call: Optional[
        Callable[[LiveTask, str, Sequence[str]], Dict[str, Any]]
    ] = None,
    timeout: float = 300.0,
) -> Dict[str, Any]:
    """Run the nightly multi-provider quality matrix.

    Without usable credentials this returns ``status="blocked"`` with no
    quality verdict. A blocked matrix is never reported as a pass, and its
    samples are never mined as failures.
    """
    selected = list(tasks if tasks is not None else live_task_set())
    report = preflight(targets)
    if not targets or report["blocked"]:
        return {
            "schema_version": 1,
            "status": "blocked",
            "verdict": "NOT_RUN",
            "reason": "no usable provider configuration for the live quality matrix",
            "preflight": report,
            "n_tasks": len(selected),
            "providers": [target.provider for target in targets],
            "samples": [],
            "matrix": None,
            "mined_failures": [],
        }
    root = (
        Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="neo-live-quality-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    samples: List[Dict[str, Any]] = []
    for target in targets:
        for task in selected:
            samples.append(_run_sample(task, target, root, judge_call, timeout))
    matrix = summarize_live_matrix(samples, selected, [t.provider for t in targets])
    matrix["meets_matrix_minimum"] = bool(
        len(selected) >= MIN_MATRIX_TASKS
        and matrix["provider_count"] >= MIN_MATRIX_PROVIDERS
    )
    failures = mine_failures(samples)
    return {
        "schema_version": 1,
        "status": "complete" if matrix["meets_matrix_minimum"] else "incomplete",
        "verdict": "MEETS_MATRIX"
        if matrix["meets_matrix_minimum"]
        else "BELOW_MATRIX_MINIMUM",
        "reason": ""
        if matrix["meets_matrix_minimum"]
        else "matrix did not reach 20 real tasks across 2 providers",
        "preflight": report,
        "n_tasks": len(selected),
        "providers": matrix["providers"],
        "samples": samples,
        "matrix": matrix,
        "mined_failures": failures,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_samples(path: str) -> List[Dict[str, Any]]:
    if not path:
        return []
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"could not read samples: {exc}") from exc
    if isinstance(value, Mapping):
        value = value.get("samples", [])
    return [dict(item) for item in value if isinstance(item, Mapping)]


def main(argv: Optional[List[str]] = None) -> int:
    """Run, summarize, or mine the live quality matrix. Exit 2 never passes."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.live_quality",
        description="Nightly live-provider quality matrix with a judged verdict.",
    )
    parser.add_argument(
        "--providers",
        default="",
        help="comma list of provider:model targets (>= 2 providers required)",
    )
    parser.add_argument(
        "--summarize", default=None, help="summarize a saved samples file"
    )
    parser.add_argument(
        "--mine", default=None, help="mine failures from a saved samples file"
    )
    parser.add_argument(
        "--registry-root",
        default=None,
        help="root that owns evals/regression_cases.json (default: the repository)",
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output only"
    )
    parser.add_argument("--out", default=None, help="write the report to this path")
    parser.add_argument(
        "--require-matrix",
        action="store_true",
        help="exit 2 unless the matrix reached its minimum size and was judged",
    )
    args = parser.parse_args(argv)

    if args.summarize or args.mine:
        samples = _load_samples(args.summarize or args.mine or "")
        payload: Dict[str, Any] = {"matrix": summarize_live_matrix(samples)}
        if args.mine:
            failures = mine_failures(samples)
            payload["mined_failures"] = failures
            if failures:
                payload["registry"] = record_failures(
                    failures, Path(args.registry_root) if args.registry_root else None
                )
        report = payload
    else:
        report = run_live_matrix(parse_targets(args.providers))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        matrix = report.get("matrix") or {}
        print(f"status: {report.get('status')}")
        if matrix:
            print(
                f"n={matrix.get('n')} providers={','.join(matrix.get('providers') or [])} "
                f"success={matrix.get('success_rate')} "
                f"ci=[{matrix.get('success_ci', {}).get('low')}, "
                f"{matrix.get('success_ci', {}).get('high')}] "
                f"cost/task=${matrix.get('cost_per_task_usd')}"
            )
        for failure in report.get("mined_failures") or []:
            print(f"  mined regression: {failure.get('fingerprint')}")
    if args.require_matrix:
        matrix = report.get("matrix") or {}
        if report.get("status") != "complete" or not matrix.get("meets_matrix_minimum"):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
