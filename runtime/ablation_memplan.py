"""Round-2 Task B ablation: memory-informed planning ON vs OFF.

Measures whether querying decision memory at PLANNING time actually helps,
on a task set where relevant prior decisions exist (the precondition the
round instructions demand).

Design (honest, mirroring runtime/ablation.py's conventions):
- Task set: Terminal 1's 5 fixture bugs (the same repos the harness's own
  e2e DoD uses). Prior decisions for each repo are SEEDED from genuinely
  mined prior-run traces (v4/v3 ablation runs under logs/ablations/) plus
  the fixture repos' own shape — recorded through DecisionStore.record
  (source "manual", the exact path memory/AGENTS.md describes for facts
  not in state files). Each seeded row carries repo_path so the planner's
  repo-scoped query finds it and other repos' rows stay out.
- Both arms: the REAL full stack (real scheduler -> real worker
  subprocesses -> real harness.core.run_task -> real Docker sandbox and
  verify) with the SAME pinned model for every call (z-ai/glm-5.3-free
  via tokenrouter — adaptive routing OFF: this ablation measures MEMORY,
  and a routing confound would make the delta unattributable).
- Arms differ in exactly one config key: plan_with_memory True/False.
- Metrics: per-task status, attempts, model calls, tokens/cost (ledger),
  verify failures, mistake recurrence (the bare-pytest import-error class
  that genuinely burned turns in prior runs), and which seeded decisions
  the planner actually saw (the decision_memory trace event).

Usage:
  python -m runtime.ablation_memplan --seed-only   # just write the store
  python -m runtime.ablation_memplan --arm on|off [--concurrency N]
  (default: both arms sequentially; summary merges into --out/summary.json
  like runtime.ablation does — separate invocations accumulate.)

Env: TOKENROUTER_API_KEY for the model endpoint. The decision store is a
DEDICATED db under the run's out dir (HARNESS_MEMPLAN_DB when workers run
— set automatically here), NOT the production store: the seeded rows are
ablation scaffolding and the production memory must not be polluted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from shared.types import Task

from runtime.ablation import EXPENSIVE_TIER, _tier  # endpoint conventions


FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _fixture_tasks() -> List[Dict[str, str]]:
    """The 5 fixture bugs with their natural issue texts (mirrors
    runtime/ablation.py's fixture set so results are comparable)."""
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


# Genuinely-mined per-repo gotchas + conventions (see module docstring):
# - the bare-pytest import-error class burned real turns in prior runs
#   (v4 abl-off-bug01 attempt 1's trace shows bare `pytest` exit=2, then
#   a `PYTHONPATH=$PWD` retry; the same class recurs in bug02/bug05 traces)
# - sed-with-quotes bash breakage shows up in bug03/bug05 prior traces
# - the suite-command and edit-strategy facts are what prior SUCCESSFUL
#   fixes actually did (read from the same traces' winning commands).
# These are decision-log facts, NOT fix cheat-sheets: no row says "change
# X to Y" — they say what bit and what worked, the same vocabulary
# state.json decisions use.
SEEDED_DECISIONS: List[Dict[str, Any]] = [
    {
        "fixture": "bug01_wrap",
        "text": (
            "running bare `pytest` in this repo fails with ImportError "
            "(the wrapwrap package is not installed); run the suite via "
            "`python -m pytest -q` instead"
        ),
    },
    {
        "fixture": "bug01_wrap",
        "text": (
            "in wrapwrap, an earlier fix used a python pathlib rewrite to "
            "replace an exact multi-line code block, then verified with "
            "the suite in the same command"
        ),
    },
    {
        "fixture": "bug02_mean",
        "text": (
            "in numlib, plain `pytest tests/test_mathutil.py` has failed "
            "collection in past runs; `python -m pytest -q` from the repo "
            "root is the reliable invocation"
        ),
    },
    {
        "fixture": "bug02_mean",
        "text": (
            "in numlib, past fixes were single-token edits applied with "
            "sed before re-running the failing test"
        ),
    },
    {
        "fixture": "bug03_stack",
        "text": (
            "in stacklib, complex sed expressions with embedded quotes "
            "broke in past attempts (bash syntax errors); prefer a small "
            "python script over chained sed for multi-line edits"
        ),
    },
    {
        "fixture": "bug03_stack",
        "text": (
            "stacklib defines StackEmptyError in stacklib/stack.py and "
            "the empty-pop contract is documented in the docstrings"
        ),
    },
    {
        "fixture": "bug04_nameerror",
        "text": (
            "in datelib, days_in_month() references a typo'd plural "
            "constant (_DAYS_PER_MONTHS) while the module defines the "
            "singular _DAYS_PER_MONTH table at the top of the module"
        ),
    },
    {
        "fixture": "bug04_nameerror",
        "text": (
            "in datelib, run the suite with `python -m pytest -q` from "
            "the repo root; bare pytest has failed collection before in "
            "this repo shape"
        ),
    },
    {
        "fixture": "bug05_cart",
        "text": (
            "cartlib's price_report state leak is the classic shared "
            "mutable default (a module-level list); fixes that created "
            "the list inside the call verified clean"
        ),
    },
    {
        "fixture": "bug05_cart",
        "text": (
            "in cartlib, `python -m pytest -q` from the repo root ran "
            "the suite reliably in past runs"
        ),
    },
    # Cross-cutting convention rows (NO repo) — kept to PROVE the
    # planner's repo filter drops them from the prompt.
    {
        "fixture": None,
        "text": "global convention row that must never appear in any repo-scoped query",
    },
]


def seed_store(db_path: Path) -> int:
    """Write the mined decisions into the ablation's dedicated store.

    Each row is recorded with repo_path = the fixture's absolute path
    (matching Task.repo_path at run time, so the planner-time repo-scoped
    query matches). Returns the number of rows written. Assumes a fresh
    db per run (the driver owns its out dir).
    """
    from memory.decision_store import DecisionStore

    store = DecisionStore(str(db_path))
    n = 0
    try:
        for row in SEEDED_DECISIONS:
            if row["fixture"]:
                repo = str((FIXTURES / row["fixture"]).resolve())
                rid = store.record(
                    row["text"], category="convention", source="manual", repo_path=repo
                )
            else:
                rid = store.record(row["text"], category="convention", source="manual")
            if rid is not None:
                n += 1
    finally:
        store.close()
    return n


def build_tasks(arm: str, common: Dict[str, Any]) -> List[Task]:
    """Fixture tasks for one arm. plan_with_memory is the ONLY difference
    between arms; the model is PINNED for every call (adaptive_routing
    off) so no routing confound can masquerade as a memory delta."""
    tasks: List[Task] = []
    for bug in _fixture_tasks():
        cfg = dict(common)
        cfg["target_test"] = bug["target_test"]
        cfg["plan_with_memory"] = arm == "on"
        tasks.append(
            Task(
                task_id=f"mp-{arm}-{bug['slug']}",
                repo_path=str(FIXTURES / bug["fixture"]),
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


def _trace_events(logs_root: Path, task_id: str, kind: str) -> List[Dict[str, Any]]:
    p = logs_root / task_id / "trace.jsonl"
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev.get("kind") == kind:
                out.append(ev.get("data", {}))
    except OSError:
        pass
    return out


# Known past-mistake signatures (from the mined traces): if these appear
# in a task's tool outputs, the run re-tripped a documented gotcha.
_MISTAKE_SIGS = [
    "ImportError while importing test module",
    "attempted relative import with no known parent package",
    "No module named",
    "command not found",
]


def collect(
    logs_root: Path, task_ids: List[str], results: Dict[str, Any], arm: str
) -> Dict[str, Any]:
    """Per-arm stats: attempts, calls, cost, plus the memory-specific
    signals (decision_memory trace events, mistake recurrence).

    Call counts are HARNESS-level (trace model_request events), NOT
    ledger entries: the router's ledger also records its internal
    empty-response retries (run1 measured 5 sub-second flake entries on
    one task), which would inflate a task's count without any extra
    planning work. The ledger's per-completion rows still drive the
    token/cost totals (retry attempts carry few completion tokens)."""
    per_task: Dict[str, Any] = {}
    for tid in task_ids:
        res = results.get(tid)
        ledger = _ledger_for(tid, logs_root)
        requests = _trace_events(logs_root, tid, "model_request")
        mem_events = _trace_events(logs_root, tid, "decision_memory")
        verify_events = _trace_events(logs_root, tid, "final_verify")
        tool_results = _trace_events(logs_root, tid, "tool_result")
        mistake_hits = sum(
            1
            for r in tool_results
            if any(sig in str(r.get("output", "")) for sig in _MISTAKE_SIGS)
        )
        verify_fails = sum(
            1 for v in verify_events if not v.get("target_passed", False)
        )
        per_task[tid] = {
            "status": getattr(res, "status", None),
            "attempts": getattr(res, "attempts", None),
            "calls": len(requests),
            "ledger_calls": len(ledger),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in ledger),
            "completion_tokens": sum(r.get("completion_tokens", 0) for r in ledger),
            "ledger_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in ledger), 6),
            "verify_fails": verify_fails,
            "past_mistake_recurrences": mistake_hits,
            "memory_events": mem_events[:3],
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
        "total_verify_fails": sum(t["verify_fails"] for t in per_task.values()),
        "total_mistake_recurrences": sum(
            t["past_mistake_recurrences"] for t in per_task.values()
        ),
        "tasks_with_mistake_recurrence": sum(
            1 for t in per_task.values() if t["past_mistake_recurrences"] > 0
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="runtime.ablation_memplan")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument(
        "--arm",
        choices=("on", "off"),
        default=None,
        help="run only one arm (default: both sequentially)",
    )
    ap.add_argument(
        "--seed-only",
        action="store_true",
        help="seed the decision store and exit (no tasks run)",
    )
    ap.add_argument("--max-wallclock-s", type=float, default=1200.0)
    ap.add_argument("--max-step-turns", type=int, default=6)
    ap.add_argument(
        "--out", default=None, help="results dir (default logs/memplan-ablations/<ts>)"
    )
    args = ap.parse_args()

    from runtime.scheduler import run as scheduler_run
    from runtime.fsutil import atomic_write_json

    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "memplan-ablations" / ts)
    logs_root = out / "tasklogs"
    db_path = out / "memplan-decisions.db"
    out.mkdir(parents=True, exist_ok=True)

    n_seeded = seed_store(db_path)
    if args.seed_only:
        print(f"seeded {n_seeded} decisions into {db_path}")
        return 0

    key = os.environ.get(EXPENSIVE_TIER["api_key_env"], "")
    if not key:
        raise SystemExit(f"missing env {EXPENSIVE_TIER['api_key_env']}")

    common: Dict[str, Any] = {
        # harness knobs (identical across arms except plan_with_memory)
        "test_command": "python -m pytest -q",
        "verify_timeout_s": 300,
        "max_step_turns": args.max_step_turns,
        "max_retries": 2,
        "budget_cap_usd": 3.0,
        "max_wallclock_s": args.max_wallclock_s,
        "command_timeout_s": 300,
        # runtime knobs
        "crash_retries": 1,
        "resume": True,
        "hang_heartbeat_stale_s": 1200.0,
        "log_root": str(logs_root),
        # MEMORY ablation: routing pinned OFF (same model every call —
        # the memory delta must not ride a routing confound)
        "adaptive_routing": False,
        **_tier(EXPENSIVE_TIER),
    }

    report: Dict[str, Any] = {
        "ts": ts,
        "ablation": "memory-informed-planning",
        "n_tasks": len(_fixture_tasks()),
        "task_set": "fixtures",
        "decisions_db": str(db_path),
        "seeded_decisions": n_seeded,
        "concurrency": args.concurrency,
        "pinned_model": EXPENSIVE_TIER["model"],
        "arms": {},
        "honesty_notes": [
            "Prior decisions are SEEDED from genuinely mined prior-run "
            "traces (logs/ablations v4 + v3-expanded-fixed) and the "
            "fixture repos' own shape — recorded via DecisionStore.record "
            "(source 'manual'), the documented path for facts not in "
            "state files. They are convention/gotcha facts, not fix "
            "cheat-sheets.",
            "5 tasks x 1 rep — directional, not benchmark-grade "
            "(same caveat class as every prior ablation in this repo).",
            "Both arms pin the SAME model for every call (adaptive "
            "routing OFF) so the memory delta cannot ride a routing "
            "confound; costs are proxy prices for a free-tier endpoint.",
            "The seeded store is a DEDICATED db under this run's out dir, "
            "NOT the production decisions.db — the ablation never "
            "pollutes production memory.",
        ],
    }

    # Workers resolve the store through memory.paths.decisions_db_path(),
    # which reads HARNESS_DECISIONS_DB — pin it for the scheduler's child
    # processes (in-process seeding used the same path).
    env_db = str(db_path.resolve())
    os.environ["HARNESS_DECISIONS_DB"] = env_db

    arms = [args.arm] if args.arm else ["off", "on"]
    for arm in arms:
        run_id = f"memplan-{ts}-{arm}"
        tasks = build_tasks(arm, common)
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
            f"verify_fails={stats['total_verify_fails']} "
            f"mistake_recurrences={stats['total_mistake_recurrences']} "
            f"wall={wall_s}s"
        )

    # Merge into an existing summary (separate-invocation accumulate, the
    # runtime.ablation convention).
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
            "verify_fails_on_minus_off": on["total_verify_fails"]
            - off["total_verify_fails"],
            "mistake_recurrences_on_minus_off": (
                on["total_mistake_recurrences"] - off["total_mistake_recurrences"]
            ),
            "cost_on_minus_off_usd": round(
                on["total_cost_usd"] - off["total_cost_usd"], 6
            ),
        }
        print("\ndelta:", json.dumps(report["delta"], indent=2))

    atomic_write_json(summary_path, report)
    print(f"\nresults: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
