"""Task-level multi-candidate ensemble routing (Improvement Round 2).

Extends the adaptive-routing mechanism with an ENSEMBLE strategy for tasks
the difficulty predictor flags as HARD, instead of escalating straight to
the expensive tier (the single-attempt ON arm's behavior):

  1. Predict task difficulty ONCE from the issue text — same v2 predictor
     (runtime.difficulty), same message shape the per-call router ingress
     scores on the planner call (issue wrapped in the planner's
     "## Issue ... ## Retrieved context" user message, so the task-level
     hint matches what the ON arm's router decides on that call).
  2. easy/medium -> run EXACTLY like the ON arm (per-call adaptive routing;
     struggle escalation can still fire mid-task). This isolates the
     three-arm comparison to the hard-predicted tasks.
  3. hard -> generate TWO cheap-tier candidate fixes IN PARALLEL. Each
     candidate is a full, verifier-gated harness run pinned to the cheap
     model (a "candidate" cannot be a single call_model: verification
     lives in the harness, above Boundary 2). If EITHER candidate
     verifies: done — the expensive tier was never touched.
  4. Only if BOTH candidates miss (any non-success status, including
     endpoint errors/timeouts) -> ONE escalation run pinned to the
     expensive model, OFF-arm semantics (full attempt budget, every call
     expensive).

Honest properties, by construction:
  - The original adaptive routing mechanism is untouched: model_router /
    difficulty are NOT modified; this driver composes the existing
    scheduler/worker/harness machinery (additive mode, separately ablated).
  - Both candidates always run to completion and BOTH costs count —
    parallel generation is a latency/resilience trade at real cost, not a
    free lunch (the ablation reports that cost honestly).
  - The escalation run is a FRESH attempt: it sees the issue + repo only,
    not the failed candidates' transcripts (no cross-run context channel
    exists; documented rather than hidden).
  - When both candidates verify, the FIRST (c1) is reported as the winner
    (deterministic; both are recorded in sub_tasks either way).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from runtime.scheduler import run as scheduler_run
from shared.types import Task

_CANDIDATE_ROLES = ("c1", "c2")
_ADAPTIVE_ROLE = "a"
_ESCALATION_ROLE = "x"


def predict_task_difficulty(
    issue_text: str,
    estimator: str = "heuristic",
    llm_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Task-level difficulty hint for one issue text.

    Assumes ``issue_text`` is the bare issue (no prompt scaffolding). The
    message list passed to the predictor replicates the planner user
    message the per-call router ingress scores ("## Issue\\n{issue}\\n\\n##
    Retrieved context..."), so difficulty._issue_text_from strips the
    scaffolding identically and the hint matches the router's planner-call
    decision. Returns (hint, info) exactly like runtime.difficulty.
    predict_difficulty; never raises (predictor failures degrade inside
    predict_difficulty, and the heuristic path is deterministic).
    """
    from runtime.difficulty import predict_difficulty

    messages = [
        {
            "role": "user",
            "content": (
                f"## Issue\n{issue_text}\n\n## Retrieved context\n"
                "(elided — task-level prediction scores the issue only)"
            ),
        }
    ]
    return predict_difficulty(
        issue_text, estimator=estimator, llm_cfg=llm_cfg, messages=messages
    )


def _sub_id(role: str, slug: str) -> str:
    return f"ens-{role}-{slug}"


def _read_ledger(task_id: str, logs_root: Path) -> List[Dict[str, Any]]:
    """A sub-task's model-call ledger (same file the ablation reads)."""
    p = logs_root / f"{task_id}.runtime" / "model_ledger.jsonl"
    if not p.exists():
        return []
    try:
        return [
            json.loads(line)
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return []


def _bug_overrides(bug: Dict[str, Any]) -> Dict[str, Any]:
    """Per-repo passthrough keys (target_test/test_command — the multirepo
    set needs them; the fixture/extra sets don't set them)."""
    return {k: bug[k] for k in ("target_test", "test_command") if bug.get(k)}


def run_ensemble(
    bugs: List[Dict[str, Any]],
    common: Dict[str, Any],
    cheap_tier: Dict[str, str],
    expensive_tier: Dict[str, str],
    *,
    concurrency: int = 5,
    logs_root: str = "logs",
    run_id: str = "ensemble-run",
    estimator: str = "heuristic",
    llm_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Run one ENSEMBLE arm over ``bugs``; returns (per_bug, meta).

    Assumes ``bugs`` entries carry slug/fixture/issue (plus optional
    target_test/test_command passthroughs); ``common`` holds the shared
    harness/runtime knobs exactly as the on/off arms use them; the tier
    dicts are resolved {provider, model, api_key, api_base} entries
    (ablation._tier shape). Optional per-bug test-only passthroughs:
    ``ens_candidate_overrides`` / ``ens_escalation_overrides`` merge into
    the respective sub-task configs (used by tests to force candidate
    failure modes; absent in real runs). Sub-task ids are
    ens-{a|c1|c2|x}-{slug}; per_bug is keyed by slug with the same
    per-task stat shape ablation.collect produces (aggregated across the
    bug's sub-tasks, ledgers concatenated in execution order), plus the
    ensemble-specific strategy/escalation fields.
    """
    logs = Path(logs_root)
    phase1: List[Task] = []
    plan: Dict[str, Dict[str, Any]] = {}

    for bug in bugs:
        slug = bug["slug"]
        hint, info = predict_task_difficulty(
            bug["issue"], estimator=estimator, llm_cfg=llm_cfg
        )
        if hint == "hard":
            cand_over = dict(bug.get("ens_candidate_overrides") or {})
            for role in _CANDIDATE_ROLES:
                cfg = {
                    **common,
                    **_bug_overrides(bug),
                    **cand_over,
                    "adaptive_routing": False,
                    **cheap_tier,
                }
                phase1.append(
                    Task(
                        task_id=_sub_id(role, slug),
                        repo_path=str(bug["fixture"]),
                        issue_text=bug["issue"],
                        config=cfg,
                    )
                )
            plan[slug] = {
                "hint": "hard",
                "features": info.get("features", {}),
                "strategy": "ensemble-2cheap",
                "sub_ids": [_sub_id(r, slug) for r in _CANDIDATE_ROLES],
            }
        else:
            cfg = {
                **common,
                **_bug_overrides(bug),
                "adaptive_routing": True,
                "model_tiers": {
                    "easy": dict(cheap_tier),
                    "medium": dict(cheap_tier),
                    "hard": dict(expensive_tier),
                },
            }
            tid = _sub_id(_ADAPTIVE_ROLE, slug)
            phase1.append(
                Task(
                    task_id=tid,
                    repo_path=str(bug["fixture"]),
                    issue_text=bug["issue"],
                    config=cfg,
                )
            )
            plan[slug] = {
                "hint": hint,
                "features": info.get("features", {}),
                "strategy": "single-adaptive",
                "sub_ids": [tid],
            }

    t0 = time.time()
    res1 = scheduler_run(
        phase1, concurrency=concurrency, logs_root=str(logs), run_id=f"{run_id}-p1"
    )

    # Phase 2: escalate hard bugs whose candidates ALL missed (any
    # non-success status — failed, error, timeout — counts as a miss; a
    # dead cheap endpoint escalates, which is the intended resilience).
    esc_tasks: List[Task] = []
    for bug in bugs:
        slug = bug["slug"]
        entry = plan[slug]
        if entry["strategy"] != "ensemble-2cheap":
            continue
        if any(
            res1.get(t) is not None and res1[t].status == "success"
            for t in entry["sub_ids"]
        ):
            continue  # a cheap candidate verified — no escalation needed
        esc_over = dict(bug.get("ens_escalation_overrides") or {})
        cfg = {
            **common,
            **_bug_overrides(bug),
            **esc_over,
            "adaptive_routing": False,
            **expensive_tier,
        }
        tid = _sub_id(_ESCALATION_ROLE, slug)
        esc_tasks.append(
            Task(
                task_id=tid,
                repo_path=str(bug["fixture"]),
                issue_text=bug["issue"],
                config=cfg,
            )
        )
        entry["sub_ids"].append(tid)
        entry["strategy"] = "ensemble-2cheap+escalated"

    res2 = (
        scheduler_run(
            esc_tasks,
            concurrency=concurrency,
            logs_root=str(logs),
            run_id=f"{run_id}-p2",
        )
        if esc_tasks
        else {}
    )
    results = {**res1, **res2}
    wall_s = round(time.time() - t0, 1)

    per_bug = {
        bug["slug"]: _aggregate_bug(
            plan[bug["slug"]],
            results,
            logs,
            cheap_model=cheap_tier["model"],
            expensive_model=expensive_tier["model"],
        )
        for bug in bugs
    }
    meta = {
        "wall_clock_s": wall_s,
        "phase1_run_id": f"{run_id}-p1",
        "phase2_run_id": f"{run_id}-p2" if esc_tasks else None,
        "n_escalations": len(esc_tasks),
    }
    return per_bug, meta


def _aggregate_bug(
    entry: Dict[str, Any],
    results: Dict[str, Task],
    logs_root: Path,
    *,
    cheap_model: str,
    expensive_model: str,
) -> Dict[str, Any]:
    """Per-bug stats: sub-task results + concatenated-ledger accounting.

    Assumes entry is run_ensemble's plan entry (hint/strategy/sub_ids) and
    results maps every sub-task id to its TaskResult. Ledger records are
    concatenated in sub-task execution order (c1, c2, then escalation), so
    the cheap->expensive escalation metric (same algorithm as
    ablation.collect) counts each hard bug's escalation run once.
    """
    sub_results = [results.get(t) for t in entry["sub_ids"]]
    records: List[Dict[str, Any]] = []
    for t in entry["sub_ids"]:
        records.extend(_read_ledger(t, logs_root))

    models: Dict[str, int] = {}
    hints: Dict[str, int] = {}
    for rec in records:
        models[rec["model"]] = models.get(rec["model"], 0) + 1
        h = rec.get("difficulty_hint")
        if h:
            hints[h] = hints.get(h, 0) + 1
    escalations = 0
    seen_cheap = False
    for rec in records:
        if rec["model"] == cheap_model:
            seen_cheap = True
        elif rec["model"] == expensive_model and seen_cheap:
            escalations += 1
            seen_cheap = False

    winner = next(
        (r for r in sub_results if r is not None and r.status == "success"), None
    )
    final = winner or next((r for r in sub_results if r is not None), None)

    sub_tasks: List[Dict[str, Any]] = []
    for t, r in zip(entry["sub_ids"], sub_results, strict=True):
        recs = _read_ledger(t, logs_root)
        sub_tasks.append(
            {
                "task_id": t,
                "status": r.status if r is not None else None,
                "attempts": r.attempts if r is not None else None,
                "calls": len(recs),
                "cost_usd": round(sum(x.get("cost_usd", 0.0) for x in recs), 6),
            }
        )

    return {
        "hint": entry["hint"],
        "prediction_features": entry["features"],
        "strategy": entry["strategy"],
        "status": final.status if final is not None else "error",
        "attempts": final.attempts if final is not None else None,
        "result_cost_usd": round(
            sum(r.cost_usd for r in sub_results if r is not None), 6
        ),
        "escalated": entry["strategy"].endswith("escalated"),
        "candidate_win": bool(winner) and entry["strategy"] == "ensemble-2cheap",
        "sub_tasks": sub_tasks,
        "calls": len(records),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in records),
        "completion_tokens": sum(r.get("completion_tokens", 0) for r in records),
        "ledger_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in records), 6),
        "models": models,
        "difficulty_hints": hints,
        "escalations_cheap_to_expensive": escalations,
    }


def ensemble_stats(per_bug: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Arm-level stats from run_ensemble's per_bug map.

    Assumes per_bug is keyed by bug slug (values from _aggregate_bug).
    Returns the same top-level shape ablation.collect produces (so the
    runner's report/printing paths stay uniform) plus the ensemble-specific
    fields, with per_task re-keyed as "abl-ensemble-{slug}" to match the
    other arms' reporting keys.
    """
    per_task = {f"abl-ensemble-{slug}": agg for slug, agg in per_bug.items()}
    values = list(per_bug.values())
    statuses = {
        s: sum(1 for a in values if a["status"] == s)
        for s in ("success", "failed", "error", "timeout")
    }
    return {
        "per_task": per_task,
        "success_rate": (
            sum(1 for a in values if a["status"] == "success") / len(values)
        )
        if values
        else 0.0,
        "statuses": statuses,
        "total_calls": sum(a["calls"] for a in values),
        "total_tokens": sum(
            a["prompt_tokens"] + a["completion_tokens"] for a in values
        ),
        "total_cost_usd": round(sum(a["ledger_cost_usd"] for a in values), 6),
        "model_mix": {
            m: sum(a["models"].get(m, 0) for a in values)
            for a in values
            for m in a["models"]
        },
        "total_escalations": sum(a["escalations_cheap_to_expensive"] for a in values),
        "tasks_with_escalation": sum(
            1 for a in values if a["escalations_cheap_to_expensive"] > 0
        ),
        # ensemble-specific
        "tasks_predicted_hard": sum(
            1 for a in values if a["strategy"] != "single-adaptive"
        ),
        "ensemble_escalated_tasks": sum(1 for a in values if a["escalated"]),
        "ensemble_candidate_wins": sum(1 for a in values if a["candidate_win"]),
    }
