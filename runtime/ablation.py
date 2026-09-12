"""The Phase-5 ablation runner (Round 2: 5 fixture bugs; Round 3: expanded
task set): real harness.core.run_task, real scheduler workers, REAL model
calls — once with adaptive routing ON, once OFF (always-expensive).

Arms:
  OFF  "always-expensive": provider/model pinned to the expensive tier for
       every call — the baseline the spec's Phase 5 ablation compares
       against ("similar success rate, much lower cost ... vs. always
       using the expensive model").
  ON   "adaptive": per-call difficulty predicted from the message content
       (runtime.difficulty, ingress v1: issues vs step sessions); easy /
       medium -> cheap tier, hard -> expensive tier.
  ENSEMBLE (Improvement Round 2, runtime/ensemble.py): task-level hint
       predicted once from the issue text (same v2 predictor + planner
       message shape the per-call ingress scores). easy/medium run
       exactly like the ON arm; hard tasks generate TWO cheap-tier
       candidate fixes in parallel (full verifier-gated runs pinned to
       cheap) and escalate to ONE expensive run only if BOTH candidates
       miss. The original single-attempt mechanism is untouched — this
       is an additional, separately-ablated mode.

Task sets:
  default  Terminal 1's 5 fixture bugs (Round 2 set)
  extra    + runtime.ablation_tasks' 11 synthesized repos (Round 3 Task B):
           ~5 easy one-liners, ~4 medium recipes, and 2 deliberately scary
           texts (stack trace / race+intermittent wording) over trivial
           one-token fixes — the false-escalation probe. `python -m
           runtime.ablation_tasks --check` self-verifies that set.
  multirepo  Round 6 Task A: the 5 REAL unfamiliar OSS repos from
           runtime.multirepo_tasks (more-itertools, arrow, inflect,
           semver 3.0.4, boltons — pinned SHAs, one introduced genuine
           bug each, per-repo suite pins). `python -m
           runtime.multirepo_tasks --check` self-verifies that set on
           the host (fails pre-fix, target+suite green post-fix).

Real backends used on this machine (both free-tier routers, BYO-key per
INTERFACES.md; bring-your-own-key semantics — different providers, so the
tier wiring incl. per-tier api_key/api_base is genuinely exercised):
  cheap     : stepfun-3.7-flash  @ https://router.bynara.id/v1
              (Round 2..4 used qwen3.8-27b; that endpoint exhausted its
              credits mid-project — see the CHEAP_TIER note below. The
              first replacement probe, longcat-2.0-free, was REJECTED
              after a real smoke run: it replies with pseudo-XML tool
              calls the bash-only harness cannot execute)
  expensive : z-ai/glm-5.3-free @ https://api.tokenrouter.com/v1

Honesty notes (also written into the results file):
  - Token counts and model-choice data are RAW measurements; cost_usd
    comes from litellm when it reports one, else the price-table fallback.
    Neither router reports cost, so costs use documented proxy rates for
    comparable model classes (see model_router._PRICES): the cost DELTA is
    a price-model delta, not an actual bill (both endpoints are free-tier).
  - Round 3: 16 tasks x 1 rep (bigger than Round 2's 5, still not
    benchmark-grade): success-rate deltas remain directional only;
    Phase 6 should re-run on SWE-bench subsets with real paid tiers.

Usage: python -m runtime.ablation [--concurrency 5]
        [--estimator heuristic] [--max-step-turns 6] [--max-retries 2]
        [--out logs/ablations/<ts>] [--arm off|on|ensemble]
        [--max-wallclock-s 600] [--tasks fixtures|extra|all|multirepo]
        [--bug <substr>]
Run-time is dominated by model calls + real pytest verification.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from runtime.fsutil import atomic_write_json
from runtime.scheduler import run as scheduler_run
from shared.types import Task

# The 5 real fixture bugs (tests/fixtures/bug0{1..5}_*) — same repos and
# issue texts Terminal 1's e2e suite fixes; issue texts are the natural
# user descriptions (NOT containing the fix — the model must find it).
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

BUGS: List[Dict[str, str]] = [
    {
        "slug": "bug01-boundary",
        "fixture": "bug01_wrap",
        "issue": (
            "wrap() drops the final line of the text when that line is "
            "shorter than the wrap width. The last line should still be "
            "returned, not silently lost."
        ),
    },
    {
        "slug": "bug02-offbyone",
        "fixture": "bug02_mean",
        "issue": (
            "mean() returns the wrong average: it divides by len(values) - 1 "
            "(sample-style) instead of len(values). All test cases get the "
            "wrong expected values."
        ),
    },
    {
        "slug": "bug03-guard",
        "fixture": "bug03_stack",
        "issue": (
            "pop() on an empty stack raises IndexError, but the library "
            "promises StackEmptyError for that case. Reproduce: create a "
            "Stack(), call pop() with no items pushed."
        ),
    },
    {
        "slug": "bug04-nameerror",
        "fixture": "bug04_nameerror",
        "issue": (
            "days_in_month() crashes with NameError: name '_DAYS_PER_MONTHS' "
            "is not defined. Something references a typo'd constant name; "
            "the function should work for any valid month."
        ),
    },
    {
        "slug": "bug05-mutabledefault",
        "fixture": "bug05_cart",
        "issue": (
            "price_report() leaks state between calls: lines added in one "
            "call show up again in later calls. Likely a mutable default "
            "argument being mutated Ã¢â‚¬â€ each call must start from a clean "
            "report."
        ),
    },
]

# ---- endpoints / price model ---------------------------------------------
# Both are free-tier BYO routers. Proxy prices = $/1M tokens (in, out),
# published rates for comparable model classes; mirrored in
# model_router._PRICES so the ledger's fallback cost uses the same numbers.
#
# Round 6 endpoint note (2026-09-09): the Round-2..4 cheap tier
# (qwen3.8-27b @ router.bynara.id) DIED mid-project ("Insufficient
# credits" on every call — free-tier credit exhaustion, the endpoint-
# variance hazard runtime/AGENTS.md documents). Probed replacements at
# the same router this round, live-audited for the bash-only harness:
# longcat-2.0-free replies with pseudo-tool-call XML markup that bash
# can't run (burned a whole 6-turn session on syntax errors — verified
# in a real smoke run, logs/ablations/mr6-smoke); nemotron-3.5-
# lightning-free gateway-flakes on longer prompts. stepfun-3.7-flash
# (small open model class) is the new cheap tier: correct bash
# commands (fenced, which harness.tools strips by design), 24-190s/call
# (slow — the run pins max_wallclock_s accordingly), credits live.
# The expensive tier (z-ai/glm-5.3-free @ tokenrouter) is UNCHANGED and
# alive — the arms' DELTA still isolates routing; only the cheap model
# under the ON arm changed (documented in the run's summary.json).
CHEAP_TIER: Dict[str, str] = {
    "provider": "openai",
    "model": "stepfun-3.7-flash",
    "api_key_env": "NARAROUTER_API_KEY",
    "api_base": "https://router.bynara.id/v1",
    "proxy_price_per_mtok": (0.20, 0.60),
}
EXPENSIVE_TIER: Dict[str, str] = {
    "provider": "openai",
    "model": "z-ai/glm-5.3-free",
    "api_key_env": "TOKENROUTER_API_KEY",
    "api_base": "https://api.tokenrouter.com/v1",
    "proxy_price_per_mtok": (0.60, 2.20),
}

CHEAP_MODEL = CHEAP_TIER["model"]
EXPENSIVE_MODEL = EXPENSIVE_TIER["model"]

# Issue-text style classes in the Round-3 expanded set (for reporting how
# the predictor's inputs were distributed — the ON arm's routing decisions
# are text-driven, so the mix matters for interpreting the result).
STYLE_LABELS = {
    "easy": "easy one-liner text",
    "medium": "medium reproduce-recipe text",
    "scary": "scary text (stack trace / race wording) over a trivial bug",
}


def _fixture_bugs() -> List[Dict[str, str]]:
    """Round-2 set: T1's 5 fixture bugs with natural issue texts."""
    return [
        {
            "slug": "bug01-boundary",
            "fixture": "bug01_wrap",
            "style": "easy",
            "issue": (
                "wrap() drops the final line of the text when that line is "
                "shorter than the wrap width. The last line should still be "
                "returned, not silently lost."
            ),
        },
        {
            "slug": "bug02-offbyone",
            "fixture": "bug02_mean",
            "style": "easy",
            "issue": (
                "mean() returns the wrong average: it divides by len(values) - 1 "
                "(sample-style) instead of len(values). All test cases get the "
                "wrong expected values."
            ),
        },
        {
            "slug": "bug03-guard",
            "fixture": "bug03_stack",
            "style": "medium",
            "issue": (
                "pop() on an empty stack raises IndexError, but the library "
                "promises StackEmptyError for that case. Reproduce: create a "
                "Stack(), call pop() with no items pushed."
            ),
        },
        {
            "slug": "bug04-nameerror",
            "fixture": "bug04_nameerror",
            "style": "medium",
            "issue": (
                "days_in_month() crashes with NameError: name '_DAYS_PER_MONTHS' "
                "is not defined. Something references a typo'd constant name; "
                "the function should work for any valid month."
            ),
        },
        {
            "slug": "bug05-mutabledefault",
            "fixture": "bug05_cart",
            "style": "medium",
            "issue": (
                "price_report() leaks state between calls: lines added in one "
                "call show up again in later calls. Likely a mutable default "
                "argument being mutated — each call must start from a clean "
                "report."
            ),
        },
    ]


def _extra_bugs(repos_root: Path) -> List[Dict[str, str]]:
    """Round-3 set additions from runtime.ablation_tasks (style-labeled)."""
    from runtime.ablation_tasks import EXTRA_TASKS

    style_by_slug = {
        "max3-middle": "easy",
        "truncate-short": "easy",
        "parse-comma": "medium",
        "path-slash": "easy",
        "backoff-race": "scary",
        "slugify-case": "scary",
        "median-odd": "easy",
        "lookup-default": "medium",
        "average-floor": "easy",
        "drop-negatives": "medium",
        "interval-end": "easy",
    }
    out: List[Dict[str, str]] = []
    for t in EXTRA_TASKS:
        out.append(
            {
                "slug": t["slug"],
                "fixture": str(repos_root / t["slug"]),  # materialized path
                "style": style_by_slug[t["slug"]],
                "issue": t["issue"],
            }
        )
    return out


def _multirepo_bugs(repos_root: Path) -> List[Dict[str, str]]:
    """Round-6 set: the five REAL OSS repo bugs (runtime.multirepo_tasks).

    Assumes the repos were baked under repos_root/<slug>/ by
    multirepo_tasks.build_all. Per-repo quirks ride along per task:
    test_command (suite pin incl. '-o addopts=' where the repo's ini
    addopts need dev-only plugins) and target_test (the regression
    node) — the fixture/extra sets need neither (uniform autodetect).
    """
    from runtime.multirepo_tasks import MULTIREPO_TASKS

    out: List[Dict[str, str]] = []
    for t in MULTIREPO_TASKS:
        out.append(
            {
                "slug": t["slug"],
                "fixture": str(repos_root / t["slug"]),
                "style": t.get("style", "medium"),
                "issue": t["issue"],
                "target_test": t["target"],
                "test_command": t["suite"],
            }
        )
    return out


BUGS: List[Dict[str, str]] = []  # resolved per-run from task-set choice


def _tier(t: Dict[str, str]) -> Dict[str, str]:
    key = os.environ.get(t["api_key_env"], "")
    if not key:
        raise SystemExit(f"missing env {t['api_key_env']} for model {t['model']}")
    return {
        "provider": t["provider"],
        "model": t["model"],
        "api_key": key,
        "api_base": t["api_base"],
    }


def build_tasks(arm: str, common: Dict[str, Any]) -> List[Task]:
    """The task-set tasks for one ablation arm. Assumes arm is 'on' or
    'off'; common carries shared harness/runtime knobs (log_root etc.).
    A bug entry may carry per-repo overrides (target_test/test_command —
    the Round-6 multi-repo set); they win over the common dict."""
    tasks: List[Task] = []
    for bug in BUGS:
        cfg = dict(common)
        for k in ("target_test", "test_command"):
            if bug.get(k):
                cfg[k] = bug[k]
        if arm == "on":
            cfg.update(
                {
                    "adaptive_routing": True,
                    "model_tiers": {
                        "easy": _tier(CHEAP_TIER),
                        "medium": _tier(CHEAP_TIER),
                        "hard": _tier(EXPENSIVE_TIER),
                    },
                }
            )
        else:
            cfg.update(
                {
                    "adaptive_routing": False,
                    **_tier(EXPENSIVE_TIER),  # pinned: always the expensive model
                }
            )
        tasks.append(
            Task(
                task_id=f"abl-{arm}-{bug['slug']}",
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


def collect(
    logs_root: Path, task_ids: List[str], results: Dict[str, Any]
) -> Dict[str, Any]:
    """Per-task + per-arm stats from scheduler results + on-disk ledgers."""
    per_task: Dict[str, Any] = {}
    for tid in task_ids:
        res = results.get(tid)
        ledger = _ledger_for(tid, logs_root)
        models: Dict[str, int] = {}
        hints: Dict[str, int] = {}
        for rec in ledger:
            models[rec["model"]] = models.get(rec["model"], 0) + 1
            h = rec.get("difficulty_hint")
            if h:
                hints[h] = hints.get(h, 0) + 1
        # escalation: a task whose call sequence moved UP the tier ladder
        # (cheap -> expensive) at some point after starting on cheap.
        seen_cheap = False
        escalations = 0
        for rec in ledger:
            if rec["model"] == CHEAP_MODEL:
                seen_cheap = True
            elif rec["model"] == EXPENSIVE_MODEL and seen_cheap:
                escalations += 1
                seen_cheap = False  # count runs of cheap->expensive moves
        per_task[tid] = {
            "status": getattr(res, "status", None)
            or results.get(tid, {}).get("status"),
            "attempts": getattr(res, "attempts", None),
            "result_cost_usd": getattr(res, "cost_usd", None),
            "calls": len(ledger),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in ledger),
            "completion_tokens": sum(r.get("completion_tokens", 0) for r in ledger),
            "ledger_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in ledger), 6),
            "models": models,
            "difficulty_hints": hints,
            "escalations_cheap_to_expensive": escalations,
        }
    ok = [t for t in per_task.values() if t["status"] == "success"]
    return {
        "per_task": per_task,
        "success_rate": len(ok) / len(per_task) if per_task else 0.0,
        "statuses": {
            s: sum(1 for t in per_task.values() if t["status"] == s)
            for s in ("success", "failed", "error", "timeout")
        },
        "total_calls": sum(t["calls"] for t in per_task.values()),
        "total_tokens": sum(
            t["prompt_tokens"] + t["completion_tokens"] for t in per_task.values()
        ),
        "total_cost_usd": round(
            sum(t["ledger_cost_usd"] for t in per_task.values()), 6
        ),
        "model_mix": {
            m: sum(t["models"].get(m, 0) for t in per_task.values())
            for t in per_task.values()
            for m in t["models"]
        },
        "total_escalations": sum(
            t["escalations_cheap_to_expensive"] for t in per_task.values()
        ),
        "tasks_with_escalation": sum(
            1 for t in per_task.values() if t["escalations_cheap_to_expensive"] > 0
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="runtime.ablation")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--estimator", default="heuristic", choices=("heuristic", "llm"))
    ap.add_argument("--max-step-turns", type=int, default=6)
    ap.add_argument("--max-retries", type=int, default=2)
    ap.add_argument("--max-wallclock-s", type=float, default=900.0)
    ap.add_argument(
        "--out", default=None, help="results dir (default logs/ablations/<ts>)"
    )
    ap.add_argument(
        "--arm",
        choices=("off", "on", "ensemble"),
        default=None,
        help="run only one arm (default: both; 'ensemble' adds "
        "the Improvement-Round-2 multi-candidate arm)",
    )
    ap.add_argument(
        "--tasks",
        choices=("fixtures", "extra", "all", "multirepo"),
        default="fixtures",
        help="task set: T1's 5 fixtures / the 11 synthesized "
        "extra repos / all 16 (Round 3) / the 5 REAL OSS "
        "multi-repo bugs (Round 6)",
    )
    ap.add_argument(
        "--bug",
        default=None,
        help="substring filter on bug slug (smoke runs, e.g. bug02)",
    )
    args = ap.parse_args()

    global BUGS
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "ablations" / ts)
    logs_root = out / "tasklogs"
    repos_root = out / "repos"  # synthesized extra repos live here
    out.mkdir(parents=True, exist_ok=True)

    bugs = _fixture_bugs()
    for b in bugs:  # fixture entries carry the repo DIR NAME — resolve
        b["fixture"] = str(FIXTURES / b["fixture"])
    if args.tasks in ("extra", "all"):
        # Materialize the synthesized repos fresh (build_repo wipes any
        # stale copy under this out dir — prior-run edits must not leak).
        from runtime.ablation_tasks import build_all

        build_all(repos_root)
        extra = _extra_bugs(repos_root)
        if args.tasks == "extra":
            bugs = extra
        else:
            bugs = bugs + extra
    elif args.tasks == "multirepo":
        # Round 6: five REAL unfamiliar OSS repos (pinned SHAs, genuine
        # introduced bugs, per-repo suite pins). Baked fresh under the
        # run's own out dir; per-task test_command/target_test come from
        # the task set spec (multirepo repo quirks are NOT uniform).
        from runtime.multirepo_tasks import build_all as build_mr

        build_mr(repos_root)
        bugs = _multirepo_bugs(repos_root)
    if args.bug:
        bugs = [b for b in bugs if args.bug in b["slug"]]
        if not bugs:
            raise SystemExit(f"no bug matches {args.bug!r}")
    BUGS = bugs
    style_mix = {
        s: sum(1 for b in bugs if b.get("style") == s)
        for s in ("easy", "medium", "scary")
    }

    common: Dict[str, Any] = {
        # harness knobs (same for both arms — only routing differs)
        "test_command": "python -m pytest -q",
        "verify_timeout_s": 300,
        "max_step_turns": args.max_step_turns,
        "max_retries": args.max_retries,
        "budget_cap_usd": 3.0,
        "max_wallclock_s": args.max_wallclock_s,
        # runtime knobs
        "crash_retries": 1,
        "resume": True,
        # cheap-tier endpoint (nararouter) measured 90-240s/call under load
        # (2026-09-07); re-measured 2026-09-08 at ~9s/call. Widen
        # per-command timeout so a slow cheap-model reply isn't mistaken
        # for a hang by the bash tool.
        "command_timeout_s": 300,
        # scheduler hang check: real harness writes state.json per STEP, but
        # one step can legitimately run minutes (model + pytest), so the
        # stale threshold sits above any healthy single-step duration.
        "hang_heartbeat_stale_s": 1200.0,
        "log_root": str(logs_root),
        # router knobs shared by both arms
        "difficulty_estimator": args.estimator,
    }

    arms = [args.arm] if args.arm else ["off", "on"]

    report: Dict[str, Any] = {
        "ts": ts,
        "task_set": args.tasks,
        "n_tasks": len(bugs),
        "issue_style_mix": style_mix,
        "estimator": args.estimator,
        "concurrency": args.concurrency,
        "arms": {},
        "tier_endpoints": {
            "cheap": {k: v for k, v in CHEAP_TIER.items() if k != "api_key_env"},
            "expensive": {
                k: v for k, v in EXPENSIVE_TIER.items() if k != "api_key_env"
            },
        },
        "honesty_notes": [
            "Token counts and model-choice data are raw measurements from "
            "per-call JSONL ledgers; costs use proxy price rates (free-tier "
            "endpoints report no cost) - cost deltas are price-model deltas.",
            (
                "Round 6 (multirepo): 5 REAL OSS repos (pinned SHAs) x 1 rep "
                "- the bugs are INTRODUCED by us (single genuine edits, "
                "each self-checked fails-pre-fix/green-post-fix on the host), "
                "upstream code otherwise; suite pins are per-repo SUBSET pins "
                "where dev-extra test deps would not install (documented per "
                "task in runtime/multirepo_tasks.py)."
            )
            if args.tasks == "multirepo"
            else (
                "Round 3: 16 tasks x 1 repetition - bigger than Round 2's 5, "
                "still directional rather than benchmark-grade; success-rate "
                "deltas remain directional only."
            ),
            "The 11 extra repos are synthesized (runtime/ablation_tasks.py, "
            "self-checked: each fails its target pre-fix and passes the "
            "full suite post-fix) - NOT hand-collected OSS bugs. "
            "(Does not apply to the 'multirepo' set: those are real OSS "
            "repos, see the Round-6 note.)",
            (
                "Ensemble arm (Improvement Round 2): both cheap candidates "
                "always run to completion and BOTH costs count (parallel "
                "generation is a latency/resilience trade at real cost); the "
                "escalation run is a fresh attempt that does NOT see the "
                "failed candidates' transcripts (no cross-run context "
                "channel exists); easy/medium tasks run identically to the "
                "ON arm, so any ensemble-vs-on delta isolates the "
                "hard-predicted tasks' strategy."
            ),
        ],
        "bug_sources": {
            "fixtures": "Terminal 1's 5 fixture bugs (tests/fixtures/)",
            "extra": "runtime/ablation_tasks.py synthesized repos",
            "multirepo": (
                "runtime/multirepo_tasks.py — 5 real OSS repos "
                "at pinned SHAs (more-itertools ca711220a6, "
                "arrow 2224255c4a, inflect 262a247d2d, semver "
                "6adf8765f6=v3.0.4, boltons 961dcff3f4) with "
                "introduced genuine bugs + regression tests"
            ),
        }.get(args.tasks, "mixed"),
    }

    for arm in arms:
        run_id = f"ablation-{ts}-{arm}"
        t0 = time.time()
        if arm == "ensemble":
            # Improvement Round 2: the multi-candidate arm. Sub-task ids
            # are ens-{role}-{slug}; per-bug slugs map onto the other
            # arms' reporting keys inside ensemble_stats.
            from runtime.ensemble import ensemble_stats, run_ensemble

            per_bug, emeta = run_ensemble(
                BUGS,
                common,
                _tier(CHEAP_TIER),
                _tier(EXPENSIVE_TIER),
                concurrency=args.concurrency,
                logs_root=str(logs_root),
                run_id=run_id,
                estimator=args.estimator,
            )
            stats = ensemble_stats(per_bug)
            stats["wall_clock_s"] = emeta["wall_clock_s"]
            stats["run_id"] = run_id
            stats["phase2_run_id"] = emeta["phase2_run_id"]
            wall_s = emeta["wall_clock_s"]
            stats["styles"] = {
                f"abl-ensemble-{b['slug']}": b.get("style") for b in BUGS
            }
            report["arms"][arm] = stats
            print(
                f"[arm={arm}] success={stats['success_rate']:.0%} "
                f"calls={stats['total_calls']} tokens={stats['total_tokens']} "
                f"cost=${stats['total_cost_usd']:.4f} wall={wall_s}s "
                f"hard={stats['tasks_predicted_hard']} "
                f"esc={stats['ensemble_escalated_tasks']} "
                f"candwins={stats['ensemble_candidate_wins']} "
                f"models={stats['model_mix']}"
            )
            continue
        tasks = build_tasks(arm, common)
        t0 = time.time()
        results = scheduler_run(
            tasks, concurrency=args.concurrency, logs_root=str(logs_root), run_id=run_id
        )
        wall_s = round(time.time() - t0, 1)
        stats = collect(logs_root, [t.task_id for t in tasks], results)
        stats["wall_clock_s"] = wall_s
        stats["run_id"] = run_id
        # per-task style labels (interpretation aid: which texts drove
        # routing decisions in the ON arm)
        stats["styles"] = {f"abl-{arm}-{b['slug']}": b.get("style") for b in bugs}
        report["arms"][arm] = stats
        print(
            f"[arm={arm}] success={stats['success_rate']:.0%} "
            f"calls={stats['total_calls']} tokens={stats['total_tokens']} "
            f"cost=${stats['total_cost_usd']:.4f} wall={wall_s}s "
            f"escalations={stats['total_escalations']} "
            f"models={stats['model_mix']}"
        )

    # Merge into any EXISTING summary.json instead of clobbering it:
    # running arms as separate invocations with the same --out must
    # accumulate, not overwrite (the Round-3 gotcha: the second arm's
    # write replaced the first arm's stats wholesale).
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
    report.setdefault("arm_runs", []).extend(
        {"arm": arm, "run_id": report["arms"][arm].get("run_id"), "ts": ts}
        for arm in arms
    )

    if "off" in report["arms"] and "on" in report["arms"]:
        off, on = report["arms"]["off"], report["arms"]["on"]
        report["delta"] = {
            "success_rate_off_minus_on": round(
                off["success_rate"] - on["success_rate"], 3
            ),
            "cost_on_minus_off_usd": round(
                on["total_cost_usd"] - off["total_cost_usd"], 6
            ),
            "cost_ratio_off_over_on": round(
                off["total_cost_usd"] / on["total_cost_usd"], 3
            )
            if on["total_cost_usd"]
            else None,
            "tokens_on_minus_off": on["total_tokens"] - off["total_tokens"],
            "escalations_on": on["total_escalations"],
        }
        d = report["delta"]
        print(
            f"\ndelta: success off-on = {d['success_rate_off_minus_on']:+.0%}, "
            f"cost on-off = ${d['cost_on_minus_off_usd']:+.4f} "
            f"(off/on ratio {d['cost_ratio_off_over_on']}x), "
            f"tokens on-off = {d['tokens_on_minus_off']:+d}, "
            f"escalations(on) = {d['escalations_on']}"
        )

    # Three-arm comparison (Improvement Round 2): always-expensive vs
    # single-attempt adaptive vs multi-candidate ensemble, on the same
    # task set. Reported honestly either way — if the ensemble does not
    # beat the existing mechanism, that IS the finding.
    if all(a in report["arms"] for a in ("off", "on", "ensemble")):
        off, on, ens = (
            report["arms"]["off"],
            report["arms"]["on"],
            report["arms"]["ensemble"],
        )
        report["delta_three_arm"] = {
            "success": {
                a: round(report["arms"][a]["success_rate"], 3)
                for a in ("off", "on", "ensemble")
            },
            "total_cost_usd": {
                a: report["arms"][a]["total_cost_usd"]
                for a in ("off", "on", "ensemble")
            },
            "total_calls": {
                a: report["arms"][a]["total_calls"] for a in ("off", "on", "ensemble")
            },
            "cost_vs_off": {
                a: round(report["arms"][a]["total_cost_usd"] / off["total_cost_usd"], 3)
                for a in ("on", "ensemble")
            }
            if off["total_cost_usd"]
            else None,
            "ensemble_vs_on_cost": round(
                ens["total_cost_usd"] - on["total_cost_usd"], 6
            ),
            "ensemble_vs_on_calls": ens["total_calls"] - on["total_calls"],
            "ensemble_notes": {
                "tasks_predicted_hard": ens["tasks_predicted_hard"],
                "escalated_to_expensive": ens["ensemble_escalated_tasks"],
                "candidate_wins": ens["ensemble_candidate_wins"],
            },
        }
        t3 = report["delta_three_arm"]
        print("\nthree-arm (off / on / ensemble):")
        print(
            f"  success  : {t3['success']['off']:.0%} / "
            f"{t3['success']['on']:.0%} / {t3['success']['ensemble']:.0%}"
        )
        print(
            f"  calls    : {t3['total_calls']['off']} / "
            f"{t3['total_calls']['on']} / {t3['total_calls']['ensemble']}"
        )
        print(
            f"  cost     : ${t3['total_cost_usd']['off']:.4f} / "
            f"${t3['total_cost_usd']['on']:.4f} / "
            f"${t3['total_cost_usd']['ensemble']:.4f}"
        )
        print(
            f"  vs OFF   : on={t3['cost_vs_off']['on']}x "
            f"ensemble={t3['cost_vs_off']['ensemble']}x of baseline cost"
        )
        print(
            f"  ens-vs-on: cost ${t3['ensemble_vs_on_cost']:+.4f}, "
            f"calls {t3['ensemble_vs_on_calls']:+d}, "
            f"hard/esc/candwin="
            f"{t3['ensemble_notes']['tasks_predicted_hard']}/"
            f"{t3['ensemble_notes']['escalated_to_expensive']}/"
            f"{t3['ensemble_notes']['candidate_wins']}"
        )

    atomic_write_json(summary_path, report)
    print(f"\nresults: {summary_path}")
    print(f"task logs (per task: .runtime/model_ledger.jsonl): {logs_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
