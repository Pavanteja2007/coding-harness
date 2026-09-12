"""Improvement Round 2 Task C ablation: agent-written edge-case tests
ON vs OFF.

Measures whether the agent-tests gate (Tasks A+B: before success is
minted, the model writes edge-case tests implied by the issue and runs
them through the same verify() pipeline) actually catches anything a
later review would care about — or whether it just adds cost.

Design (honest, mirroring runtime/ablation_memplan.py's conventions):
- Both arms: the REAL full stack (real scheduler -> real worker
  subprocesses -> real harness.core.run_task -> real Docker sandbox and
  verify) with the SAME pinned model for every call (z-ai/glm-5.3-free
  via tokenrouter — adaptive routing OFF: this ablation measures the
  GATE, and a routing confound would make the delta unattributable).
- Arms differ in exactly one config key: agent_tests True/False. Every
  other harness knob is identical.
- Metrics: per-task status, attempts, model calls, tokens/cost
  (ledger), wall clock — PLUS the gate-specific signals mined from the
  harness traces: agent_tests_generated / _baseline_pass / _all_
  baseline_pass / _verify / _failed / _passed / _skip event counts,
  how many generated tests survived the baseline filter, gate skips
  and their reasons, and (the honest "did it catch anything" measure)
  agent_tests_failed events — each one is a fix that passed the WHOLE
  existing suite but failed an issue-implied edge, i.e. exactly the
  incompleteness class a later human review would have had to find.
- Task sets: fixtures (Terminal 1's 5) and multirepo (the 5 real OSS
  repos) — the multirepo set is where the gate has real room to fire
  (the existing suites are SUBSET pins, so issue-implied edges the
  suite doesn't cover are plentiful).

Usage:
  python -m harness.ablation_agenttests --arm on|off [--tasks fixtures|multirepo]
        [--concurrency N] [--out logs/agent-tests-ablation/<ts>]
  (default: both arms sequentially; summary merges into --out/summary.json
  like runtime.ablation does — separate invocations accumulate.)

Env: TOKENROUTER_API_KEY for the model endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from runtime.ablation import EXPENSIVE_TIER, _tier  # endpoint conventions
from shared.types import Task

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

GATE_EVENT_KINDS = (
    "agent_tests_generated",
    "agent_tests_baseline_pass",
    "agent_tests_all_baseline_pass",
    "agent_tests_verify",
    "agent_tests_failed",
    "agent_tests_passed",
    "agent_tests_skip",
)


def _fixture_tasks() -> List[Dict[str, str]]:
    """The 5 fixture bugs with natural issue texts + target tests
    (mirrors runtime/ablation.py's fixture set so results are
    comparable across the ablation family)."""
    return [
        {
            "slug": "bug01-boundary",
            "fixture": "bug01_wrap",
            "issue": (
                "wrap() drops the final line of the text when that line is "
                "shorter than the wrap width. The last line should still be "
                "returned, not silently lost."
            ),
            "target_test": "tests/test_textutil.py::test_wrap_short_trailing_line",
        },
        {
            "slug": "bug02-offbyone",
            "fixture": "bug02_mean",
            "issue": (
                "mean() returns the wrong average: it divides by len(values) - 1 "
                "(sample-style) instead of len(values). All test cases get the "
                "wrong expected values."
            ),
            "target_test": "tests/test_mathutil.py::test_mean_even_count",
        },
        {
            "slug": "bug03-guard",
            "fixture": "bug03_stack",
            "issue": (
                "pop() on an empty stack raises IndexError, but the library "
                "promises StackEmptyError for that case. Reproduce: create a "
                "Stack(), call pop() with no items pushed."
            ),
            "target_test": "tests/test_stack.py::test_pop_empty_raises_stackemptyerror",
        },
        {
            "slug": "bug04-nameerror",
            "fixture": "bug04_nameerror",
            "issue": (
                "days_in_month() crashes with NameError: name '_DAYS_PER_MONTHS' "
                "is not defined. Something references a typo'd constant name; "
                "the function should work for any valid month."
            ),
            "target_test": "tests/test_dateutil.py::test_days_in_month_fixed",
        },
        {
            "slug": "bug05-mutabledefault",
            "fixture": "bug05_cart",
            "issue": (
                "price_report() leaks state between calls: lines added in one "
                "call show up again in later calls. Likely a mutable default "
                "argument being mutated — each call must start from a clean "
                "report."
            ),
            "target_test": "tests/test_cart.py::test_price_report_isolated",
        },
    ]


def _multirepo_bugs(repos_root: Path) -> List[Dict[str, Any]]:
    """The 5 REAL OSS repo bugs (runtime.multirepo_tasks) with their
    per-repo target/suite pins riding along."""
    from runtime.multirepo_tasks import MULTIREPO_TASKS

    out: List[Dict[str, Any]] = []
    for t in MULTIREPO_TASKS:
        out.append(
            {
                "slug": t["slug"],
                "fixture": str(repos_root / t["slug"]),
                "issue": t["issue"],
                "target_test": t["target"],
                "test_command": t["suite"],
            }
        )
    return out


def build_tasks(
    arm: str, bugs: List[Dict[str, Any]], common: Dict[str, Any]
) -> List[Task]:
    """Tasks for one arm. agent_tests is the ONLY difference between
    arms; the model is PINNED for every call (adaptive_routing off) so
    no routing confound can masquerade as a gate delta. Per-bug
    target_test/test_command overrides win over the common dict (the
    multirepo set needs them)."""
    tasks: List[Task] = []
    for bug in bugs:
        cfg = dict(common)
        for k in ("target_test", "test_command"):
            if bug.get(k):
                cfg[k] = bug[k]
        cfg["agent_tests"] = arm == "on"
        tasks.append(
            Task(
                task_id=f"at-{arm}-{bug['slug']}",
                repo_path=str(bug["fixture"]),
                issue_text=bug["issue"],
                config=cfg,
            )
        )
    return tasks


def _ledger_for(task_id: str, logs_root: Path) -> List[Dict[str, Any]]:
    p = logs_root / f"{task_id}.runtime" / "model_ledger.jsonl"
    if not p.exists():
        return []
    try:
        return [
            json.loads(l)
            for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    except OSError:
        return []


def _trace(logs_root: Path, task_id: str) -> List[Dict[str, Any]]:
    p = logs_root / task_id / "trace.jsonl"
    if not p.exists():
        return []
    try:
        return [
            json.loads(l)
            for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    except OSError:
        return []


def collect(
    logs_root: Path, task_ids: List[str], results: Dict[str, Any], arm: str
) -> Dict[str, Any]:
    """Per-arm stats: attempts, calls, cost — plus the GATE-specific
    signals from the harness trace (generated/survived/failed/skip
    event counts; skip reasons; saved-test evidence)."""
    per_task: Dict[str, Any] = {}
    for tid in task_ids:
        res = results.get(tid)
        ledger = _ledger_for(tid, logs_root)
        events = _trace(logs_root, tid)
        gate_counts = {k: 0 for k in GATE_EVENT_KINDS}
        skip_reasons: List[str] = []
        gen_files = 0
        for ev in events:
            kind = ev.get("kind")
            if kind in gate_counts:
                gate_counts[kind] += 1
            if kind == "agent_tests_generated":
                gen_files += len(ev.get("data", {}).get("files", []))
            if kind == "agent_tests_skip":
                skip_reasons.append(str(ev.get("data", {}).get("reason", "")))
        # tests that SURVIVED the baseline filter = generated minus
        # baseline-passing minus all-baseline-pass (the skip-with-drop
        # path); verify events count post-fix runs of survivors.
        per_task[tid] = {
            "status": getattr(res, "status", None),
            "attempts": getattr(res, "attempts", None),
            "calls": len([e for e in events if e.get("kind") == "model_request"]),
            "ledger_calls": len(ledger),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in ledger),
            "completion_tokens": sum(r.get("completion_tokens", 0) for r in ledger),
            "ledger_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in ledger), 6),
            "gate_events": gate_counts,
            "generated_files_total": gen_files,
            "gate_skip_reasons": skip_reasons[:3],
        }
    ok = [t for t in per_task.values() if t["status"] == "success"]
    return {
        "per_task": per_task,
        "success_rate": len(ok) / len(per_task) if per_task else 0.0,
        "total_attempts": sum((t["attempts"] or 0) for t in per_task.values()),
        "total_calls": sum(t["calls"] for t in per_task.values()),
        "total_tokens": sum(
            t["prompt_tokens"] + t["completion_tokens"] for t in per_task.values()
        ),
        "total_cost_usd": round(
            sum(t["ledger_cost_usd"] for t in per_task.values()), 6
        ),
        # the headline honesty metrics: how often the gate FIRED (a
        # suite-passing fix failed an issue-implied edge) vs how often
        # it SKIPPED (generation quality refused to judge)
        "gate_fires": sum(
            t["gate_events"]["agent_tests_failed"] for t in per_task.values()
        ),
        "gate_passes": sum(
            t["gate_events"]["agent_tests_passed"] for t in per_task.values()
        ),
        "gate_skips": sum(
            t["gate_events"]["agent_tests_skip"] for t in per_task.values()
        ),
        "tests_generated": sum(t["generated_files_total"] for t in per_task.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="harness.ablation_agenttests")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument(
        "--arm",
        choices=("on", "off"),
        default=None,
        help="run only one arm (default: both sequentially)",
    )
    ap.add_argument(
        "--tasks",
        choices=("fixtures", "multirepo"),
        default="fixtures",
        help="task set: the 5 fixture bugs / the 5 REAL OSS "
        "multi-repo bugs (where the gate has room to fire)",
    )
    ap.add_argument("--max-wallclock-s", type=float, default=2400.0)
    ap.add_argument("--max-step-turns", type=int, default=6)
    ap.add_argument(
        "--out",
        default=None,
        help="results dir (default logs/agent-tests-ablation/<ts>)",
    )
    args = ap.parse_args()

    from runtime.fsutil import atomic_write_json
    from runtime.scheduler import run as scheduler_run

    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "agent-tests-ablation" / ts)
    logs_root = out / "tasklogs"
    repos_root = out / "repos"
    out.mkdir(parents=True, exist_ok=True)

    if args.tasks == "multirepo":
        from runtime.multirepo_tasks import build_all as build_mr

        baked = build_mr(repos_root)
        bugs: List[Dict[str, Any]] = _multirepo_bugs(repos_root)
        print(f"baked {len(baked)} multirepo tasks under {repos_root}")
    else:
        bugs = list(_fixture_tasks())
        for b in bugs:
            b["fixture"] = str(FIXTURES / b["fixture"])

    key = os.environ.get(EXPENSIVE_TIER["api_key_env"], "")
    if not key:
        raise SystemExit(f"missing env {EXPENSIVE_TIER['api_key_env']}")

    common: Dict[str, Any] = {
        # harness knobs (identical across arms except agent_tests)
        "test_command": "python -m pytest -q",
        "verify_timeout_s": 300,
        "max_step_turns": args.max_step_turns,
        "max_retries": 2,
        "budget_cap_usd": 3.0,
        "max_wallclock_s": args.max_wallclock_s,
        "command_timeout_s": 300,
        # self_critique OFF in BOTH arms: this round measures the
        # AGENT-TESTS gate; the critique gate is a separate mechanism
        # with its own sc8 ablation (this round builds on it, doesn't
        # re-measure it — one confound at a time)
        "self_critique": False,
        # runtime knobs
        "crash_retries": 1,
        "resume": True,
        "hang_heartbeat_stale_s": 1200.0,
        "log_root": str(logs_root),
        # routing pinned OFF (same model every call)
        "adaptive_routing": False,
        **_tier(EXPENSIVE_TIER),
        # NO max_completion_tokens: measured on this endpoint
        # (probe_logs/atgate-mct-*.json + the litellm probe), the model
        # burns ~29k HIDDEN reasoning tokens before any visible content;
        # a cap of 4000 forces finish_reason=length with content_len=0
        # (the first fixtures run's 5/5 empty generation replies). The
        # endpoint default works but is SLOW (~150-345s/call) — the
        # wallclock is sized for that instead.
    }

    report: Dict[str, Any] = {
        "ts": ts,
        "ablation": "agent-written-edge-case-tests",
        "n_tasks": len(bugs),
        "task_set": args.tasks,
        "concurrency": args.concurrency,
        "pinned_model": EXPENSIVE_TIER["model"],
        "arms": {},
        "honesty_notes": [
            "Both arms run the REAL full stack (scheduler -> worker "
            "subprocesses -> harness.core.run_task -> Docker sandbox + "
            "verify) with the SAME pinned model every call (adaptive "
            "routing OFF) — arms differ in exactly one key: agent_tests.",
            "self_critique is pinned OFF in both arms: the critique gate "
            "is a separate mechanism already ablated under sc8-*; this "
            "run measures the agent-tests gate alone (one confound at "
            "a time).",
            "Costs are proxy prices for a free-tier endpoint (see "
            "runtime/ablation.py's standing honesty notes); token counts "
            "are raw ledger measurements.",
            f"{len(bugs)} tasks x 1 rep — directional, not "
            "benchmark-grade (same caveat class as every prior "
            "ablation in this repo).",
            "gate_fires (agent_tests_failed events) is the 'did it "
            "catch anything' metric: each fire is a fix that passed the "
            "whole existing suite yet failed an issue-implied edge — "
            "the incompleteness class a later human review would have "
            "had to find. gate_skips are generation-quality refusals "
            "to judge (never overturn a verified fix).",
        ],
    }

    arms = [args.arm] if args.arm else ["off", "on"]
    for arm in arms:
        run_id = f"atgate-{ts}-{arm}"
        tasks = build_tasks(arm, bugs, common)
        t0 = time.time()
        results = scheduler_run(
            tasks, concurrency=args.concurrency, logs_root=str(logs_root), run_id=run_id
        )
        wall_s = round(time.time() - t0, 1)
        stats = collect(logs_root, [t.task_id for t in tasks], results, arm)
        stats["wall_clock_s"] = wall_s
        stats["run_id"] = run_id
        report["arms"][arm] = stats
        print(
            f"[arm={arm}] success={stats['success_rate']:.0%} "
            f"attempts={stats['total_attempts']} calls={stats['total_calls']} "
            f"tokens={stats['total_tokens']} cost=${stats['total_cost_usd']:.4f} "
            f"gen={stats['tests_generated']} fires={stats['gate_fires']} "
            f"gate_pass={stats['gate_passes']} skips={stats['gate_skips']} "
            f"wall={wall_s}s"
        )

    # Merge into an existing summary (separate-invocation accumulate,
    # the runtime.ablation convention).
    summary_path = out / "summary.json"
    if summary_path.exists():
        try:
            prev = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = None
        if isinstance(prev, dict) and isinstance(prev.get("arms"), dict):
            for arm_name, arm_stats in prev["arms"].items():
                report["arms"].setdefault(arm_name, arm_stats)
            report.setdefault("arm_runs", []).extend(prev.get("arm_runs", []))
    report.setdefault("arm_runs", []).extend({"arm": arm, "ts": ts} for arm in arms)

    if "off" in report["arms"] and "on" in report["arms"]:
        off, on = report["arms"]["off"], report["arms"]["on"]
        report["delta"] = {
            "attempts_on_minus_off": on["total_attempts"] - off["total_attempts"],
            "calls_on_minus_off": on["total_calls"] - off["total_calls"],
            "cost_on_minus_off_usd": round(
                on["total_cost_usd"] - off["total_cost_usd"], 6
            ),
            "gate_fires_on": on["gate_fires"],
            "gate_passes_on": on["gate_passes"],
            "gate_skips_on": on["gate_skips"],
            "success_off_minus_on": round(off["success_rate"] - on["success_rate"], 3),
        }
        print("\ndelta:", json.dumps(report["delta"], indent=2))

    atomic_write_json(summary_path, report)
    print(f"\nresults: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
