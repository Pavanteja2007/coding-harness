"""First REAL ablation (Phase 5): Terminal 1's 5 fixture bugs through the
full stack Ã¢â‚¬â€ real harness.core.run_task, real scheduler workers, REAL
model calls Ã¢â‚¬â€ once with adaptive routing ON, once OFF (always-expensive).

Arms:
  OFF  "always-expensive": provider/model pinned to the expensive tier for
       every call Ã¢â‚¬â€ the baseline the spec's Phase 5 ablation compares
       against ("similar success rate, much lower cost ... vs. always
       using the expensive model").
  ON   "adaptive": per-call difficulty predicted from the message content
       (runtime.difficulty, ingress v1: issues vs step sessions); easy /
       medium -> cheap tier, hard -> expensive tier.

Real backends used on this machine (both free-tier routers, BYO-key per
INTERFACES.md; bring-your-own-key semantics Ã¢â‚¬â€ different providers, so the
tier wiring incl. per-tier api_key/api_base is genuinely exercised):
  cheap     : qwen3.8-27b        @ https://router.bynara.id/v1
  expensive : z-ai/glm-5.3-free  @ https://api.tokenrouter.com/v1

Honesty notes (also written into the results file):
  - Token counts and model-choice data are RAW measurements; cost_usd
    comes from litellm when it reports one, else the price-table fallback.
    Neither router reports cost, so costs use documented proxy rates for
    comparable model classes (see model_router._PRICES): the cost DELTA is
    a price-model delta, not an actual bill (both endpoints are free-tier).
  - 5 tasks x 1 repetition is small-scale by design: the point is a real
    data point on real bugs, not a benchmark number. Success-rate deltas
    here are directional only.

Usage: python -m runtime.ablation [--concurrency 5]
       [--estimator heuristic] [--max-step-turns 6] [--max-retries 2]
       [--out logs/ablations/<ts>] [--arm on|off] [--max-wallclock-s 600]
Run-time is dominated by model calls + real pytest verification.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from runtime.fsutil import read_json_or_none
from runtime.scheduler import run as scheduler_run
from shared.types import Task

# The 5 real fixture bugs (tests/fixtures/bug0{1..5}_*) Ã¢â‚¬â€ same repos and
# issue texts Terminal 1's e2e suite fixes; issue texts are the natural
# user descriptions (NOT containing the fix Ã¢â‚¬â€ the model must find it).
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

BUGS: List[Dict[str, str]] = [
    {"slug": "bug01-boundary", "fixture": "bug01_wrap",
     "issue": ("wrap() drops the final line of the text when that line is "
               "shorter than the wrap width. The last line should still be "
               "returned, not silently lost.")},
    {"slug": "bug02-offbyone", "fixture": "bug02_mean",
     "issue": ("mean() returns the wrong average: it divides by len(values) - 1 "
               "(sample-style) instead of len(values). All test cases get the "
               "wrong expected values.")},
    {"slug": "bug03-guard", "fixture": "bug03_stack",
     "issue": ("pop() on an empty stack raises IndexError, but the library "
               "promises StackEmptyError for that case. Reproduce: create a "
               "Stack(), call pop() with no items pushed.")},
    {"slug": "bug04-nameerror", "fixture": "bug04_nameerror",
     "issue": ("days_in_month() crashes with NameError: name '_DAYS_PER_MONTHS' "
               "is not defined. Something references a typo'd constant name; "
               "the function should work for any valid month.")},
    {"slug": "bug05-mutabledefault", "fixture": "bug05_cart",
     "issue": ("price_report() leaks state between calls: lines added in one "
               "call show up again in later calls. Likely a mutable default "
               "argument being mutated Ã¢â‚¬â€ each call must start from a clean "
               "report.")},
]

# ---- endpoints / price model ---------------------------------------------
# Both are free-tier BYO routers. Proxy prices = $/1M tokens (in, out),
# published rates for comparable model classes; mirrored in
# model_router._PRICES so the ledger's fallback cost uses the same numbers.
CHEAP_TIER: Dict[str, str] = {
    "provider": "openai", "model": "qwen3.8-27b",
    "api_key_env": "NARAROUTER_API_KEY",
    "api_base": "https://router.bynara.id/v1",
    "proxy_price_per_mtok": (0.20, 0.60),
}
EXPENSIVE_TIER: Dict[str, str] = {
    "provider": "openai", "model": "z-ai/glm-5.3-free",
    "api_key_env": "TOKENROUTER_API_KEY",
    "api_base": "https://api.tokenrouter.com/v1",
    "proxy_price_per_mtok": (0.60, 2.20),
}


def _tier(t: Dict[str, str]) -> Dict[str, str]:
    key = os.environ.get(t["api_key_env"], "")
    if not key:
        raise SystemExit(f"missing env {t['api_key_env']} for model {t['model']}")
    return {"provider": t["provider"], "model": t["model"],
            "api_key": key, "api_base": t["api_base"]}


def _slower_arm_first(arms: List[str]) -> List[str]:
    """Run the cheap-tier-dependent arm last when the cheap endpoint is
    degraded (observed tonight: nararouter latency 90-240s/call under
    load). Both arms are independent; this just reduces wall-clock risk
    of the OFF arm's results timing out while we wait on the ON arm."""
    return arms


def build_tasks(arm: str, common: Dict[str, Any]) -> List[Task]:
    """The 5 fixture tasks for one ablation arm. Assumes arm is 'on' or
    'off'; common carries shared harness/runtime knobs (log_root etc.)."""
    tasks: List[Task] = []
    for bug in BUGS:
        cfg = dict(common)
        if arm == "on":
            cfg.update({
                "adaptive_routing": True,
                "model_tiers": {"easy": _tier(CHEAP_TIER),
                                "medium": _tier(CHEAP_TIER),
                                "hard": _tier(EXPENSIVE_TIER)},
            })
        else:
            cfg.update({
                "adaptive_routing": False,
                **_tier(EXPENSIVE_TIER),  # pinned: always the expensive model
            })
        tasks.append(Task(
            task_id=f"abl-{arm}-{bug['slug']}",
            repo_path=str(FIXTURES / bug["fixture"]),
            issue_text=bug["issue"],
            config=cfg,
        ))
    return tasks


def _ledger_for(task_id: str, logs_root: Path) -> List[Dict[str, Any]]:
    p = logs_root / f"{task_id}.runtime" / "model_ledger.jsonl"
    if not p.exists():
        return []
    try:
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip()]
    except OSError:
        return []


def collect(logs_root: Path, task_ids: List[str],
            results: Dict[str, Any]) -> Dict[str, Any]:
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
        per_task[tid] = {
            "status": getattr(res, "status", None) or results.get(tid, {}).get("status"),
            "attempts": getattr(res, "attempts", None),
            "result_cost_usd": getattr(res, "cost_usd", None),
            "calls": len(ledger),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in ledger),
            "completion_tokens": sum(r.get("completion_tokens", 0) for r in ledger),
            "ledger_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in ledger), 6),
            "models": models,
            "difficulty_hints": hints,
        }
    ok = [t for t in per_task.values() if t["status"] == "success"]
    return {
        "per_task": per_task,
        "success_rate": len(ok) / len(per_task) if per_task else 0.0,
        "statuses": {s: sum(1 for t in per_task.values() if t["status"] == s)
                     for s in ("success", "failed", "error", "timeout")},
        "total_calls": sum(t["calls"] for t in per_task.values()),
        "total_tokens": sum(t["prompt_tokens"] + t["completion_tokens"]
                            for t in per_task.values()),
        "total_cost_usd": round(sum(t["ledger_cost_usd"] for t in per_task.values()), 6),
        "model_mix": {m: sum(t["models"].get(m, 0) for t in per_task.values())
                      for t in per_task.values() for m in t["models"]},
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="runtime.ablation")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--estimator", default="heuristic",
                    choices=("heuristic", "llm"))
    ap.add_argument("--max-step-turns", type=int, default=6)
    ap.add_argument("--max-retries", type=int, default=2)
    ap.add_argument("--max-wallclock-s", type=float, default=600.0)
    ap.add_argument("--out", default=None,
                    help="results dir (default logs/ablations/<ts>)")
    ap.add_argument("--arm", choices=("on", "off"), default=None,
                    help="run only one arm (default: both)")
    ap.add_argument("--bug", default=None,
                    help="substring filter on bug slug (smoke runs, e.g. bug02)")
    args = ap.parse_args()

    global BUGS
    if args.bug:
        BUGS = [b for b in BUGS if args.bug in b["slug"]]
        if not BUGS:
            raise SystemExit(f"no bug matches {args.bug!r}")

    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "ablations" / ts)
    logs_root = out / "tasklogs"
    out.mkdir(parents=True, exist_ok=True)

    common: Dict[str, Any] = {
        # harness knobs (same for both arms — only routing differs)
        "test_command": "python -m pytest -q",
        "verify_timeout_s": 180,
        "max_step_turns": args.max_step_turns,
        "max_retries": args.max_retries,
        "budget_cap_usd": 3.0,
        "max_wallclock_s": args.max_wallclock_s,
        # runtime knobs
        "crash_retries": 1,
        "resume": True,
        # cheap-tier endpoint (nararouter) measured 90-240s/call under load
        # tonight (2026-09-07): widen per-command timeout so a slow cheap-
        # model reply isn't mistaken for a hang by the bash tool.
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
        "estimator": args.estimator,
        "concurrency": args.concurrency,
        "arms": {},
        "tier_endpoints": {
            "cheap": {k: v for k, v in CHEAP_TIER.items() if k != "api_key_env"},
            "expensive": {k: v for k, v in EXPENSIVE_TIER.items() if k != "api_key_env"},
        },
        "honesty_notes": [
            "Token counts and model-choice data are raw measurements from "
            "per-call JSONL ledgers; costs use proxy price rates (free-tier "
            "endpoints report no cost) - cost deltas are price-model deltas.",
            "5 tasks x 1 repetition: small-scale by design; success-rate "
            "deltas are directional only.",
        ],
    }

    for arm in arms:
        run_id = f"ablation-{ts}-{arm}"
        tasks = build_tasks(arm, common)
        t0 = time.time()
        results = scheduler_run(tasks, concurrency=args.concurrency,
                                logs_root=str(logs_root), run_id=run_id)
        wall_s = round(time.time() - t0, 1)
        stats = collect(logs_root, [t.task_id for t in tasks], results)
        stats["wall_clock_s"] = wall_s
        stats["run_id"] = run_id
        report["arms"][arm] = stats
        print(f"[arm={arm}] success={stats['success_rate']:.0%} "
              f"calls={stats['total_calls']} tokens={stats['total_tokens']} "
              f"cost=${stats['total_cost_usd']:.4f} wall={wall_s}s "
              f"models={stats['model_mix']}")

    if "off" in report["arms"] and "on" in report["arms"]:
        off, on = report["arms"]["off"], report["arms"]["on"]
        report["delta"] = {
            "success_rate_off_minus_on":
                round(off["success_rate"] - on["success_rate"], 3),
            "cost_on_minus_off_usd":
                round(on["total_cost_usd"] - off["total_cost_usd"], 6),
            "cost_ratio_off_over_on":
                round(off["total_cost_usd"] / on["total_cost_usd"], 3)
                if on["total_cost_usd"] else None,
            "tokens_on_minus_off":
                on["total_tokens"] - off["total_tokens"],
        }
        d = report["delta"]
        print(f"\ndelta: success off-on = {d['success_rate_off_minus_on']:+.0%}, "
              f"cost on-off = ${d['cost_on_minus_off_usd']:+.4f} "
              f"(off/on ratio {d['cost_ratio_off_over_on']}x), "
              f"tokens on-off = {d['tokens_on_minus_off']:+d}")

    (out / "summary.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nresults: {out / 'summary.json'}")
    print(f"task logs (per task: .runtime/model_ledger.jsonl): {logs_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


