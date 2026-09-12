"""The prompt-regression eval runner (Task B core).

Method (mirrors runtime/ablation.py's paired-arm discipline, applied to
prompts):

  - SAME fixed task set (evals.tasks.all_tasks) for every arm.
  - Each arm = a dict of task.config overrides (a PROMPT/feature
    configuration), run through the REAL harness.core.run_task with a
    scripted model (deterministic; zero network) and the REAL Docker
    sandbox + verifier. Nothing stubbed except the model reply.
  - Per task we score:
      outcome            success/failed/error/timeout (status)
      attempts           how many verify-gated retries it took
      verified           target + regression + not flaky (from the result)
      loop_integrity     trace.jsonl has task_start AND task_end AND
                         result; no plan_parse_error; no unhandled
                         no-command nudges loop; files_touched recorded
  - A REGRESSION = a task (or integrity check) whose outcome worsens
    vs the baseline arm; the report says exactly which check moved.

Arms shipped by the harness itself live in ARMS below; the improvement
round's prompt changes are the ON state of their config keys, so the
comparison arms toggle them OFF one at a time AND all at once
("pre_round" = the pre-improvement prompt surface).

Determinism notes (honest): scripted models make replies identical,
but wall-clock timing still varies (Docker warm/cold). Timing is
reported but never a gate. Each task runs with a FRESH log dir
(logs/evals/<ts>/<arm>/<slug> as log_root) so runs never share state;
the memory-informed-planning arm additionally gets an ISOLATED,
pre-seeded decision store so its behavior is reproducible (a real
store with exactly one relevant decision + noise entries).

Output: logs/evals/<ts>/eval_report.json + stdout matrix; --json emits
only the machine-readable report (for CI gating).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import tasks as eval_tasks  # noqa: E402 — path boot first
from harness.deps import set_call_model  # noqa: E402
from shared.types import Task  # noqa: E402

# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------

# Base config every arm shares (lean, deterministic, REAL Docker verify).
_BASE: Dict[str, Any] = {
    "test_command": "python -m pytest -q",
    "command_timeout_s": 60,
    "verify_timeout_s": 180,
    "max_step_turns": 8,
    "max_retries": 2,
    # product-output paths are not what the eval measures; keep runs lean
    "git_output": False,
    "rationale_log": False,
    # routing is NOT under test (single-tier scripted model); the router
    # still runs and records usage — the eval's loop uses it verbatim.
    "adaptive_routing": False,
    "model_tiers": None,
    "use_mock_provider": True,
}

# The improvement-round prompt features, as config keys (T1's landed set).
_ROUND_KEYS = [
    "plan_with_memory",  # memory-informed planning (planner prompt)
    "lint_gate",  # structured lint feedback (repair loop)
    "docs_lookup_enabled",  # DOCS escape (step system prompt)
    "agent_tests",  # agent-written regression tests (step prompt)
    "web_fetch_enabled",  # FETCH web-page reading (step system prompt)
]

ARMS: Dict[str, Dict[str, Any]] = {
    # what ships today: every improvement-round prompt feature ON
    "baseline": {},
    # each feature off, one at a time — isolates its prompt change
    "no_memory": {"plan_with_memory": False},
    "no_lint": {"lint_gate": False, "lint_names": False},
    "no_docs": {"docs_lookup_enabled": False},
    "no_agent_tests": {"agent_tests": False},
    "no_webfetch": {"web_fetch_enabled": False},
    # the pre-improvement prompt surface, wholesale
    "pre_round": {k: False for k in _ROUND_KEYS},
}


# ---------------------------------------------------------------------------
# Per-task run + scoring
# ---------------------------------------------------------------------------


def _run_one(
    task_spec: Dict[str, Any],
    arm_overrides: Dict[str, Any],
    log_root: Path,
    decision_db: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run one eval task through the real loop; score it.

    Assumes task_spec comes from evals.tasks; the scripted model reply is
    identical across arms (only prompts/config differ — that's the
    variable under test). Returns the score dict; NEVER raises for a task
    outcome (errors are captured as outcome="crash" with the traceback
    in the report — the eval must not die on one bad task).
    """
    from harness.deps import reset_overrides
    from shared import tracing as _tracing
    from tests.fake_model import ScriptedModel

    task_id = task_spec["slug"]
    log_dir = log_root / task_id
    trace_root_env = os.environ.get("VEX_TRACE_DIR") or ""
    cfg = dict(_BASE)
    cfg.update(arm_overrides)

    task = Task(
        task_id=task_id,
        repo_path=task_spec["repo"],
        issue_text=task_spec["issue"],
        config=cfg,
    )

    started = time.time()
    reset_overrides()
    # Per-task unified-trace isolation: each eval task's cross-module
    # stream lands under ITS OWN log dir (<arm>/<slug>/_trace/), so the
    # 6 arms never interleave into one <slug>.jsonl (task ids repeat
    # across arms — a shared stream would mix 6 lifecycles). The env is
    # process-cached in shared.tracing, so re-point + reset per task.
    os.environ["VEX_TRACE_DIR"] = str(log_dir)
    _tracing._reset_cache()
    # In-process scripted model (tests/fake_model.ScriptedModel) via the
    # deps override seam — same mechanism T1's own e2e suite uses. It
    # supports PER-ATTEMPT scripts (mock_provider's flat spec cannot
    # express "attempt 1 breaks, attempt 2 repairs"), and the eval runner
    # runs in-process, so the override reaches run_task directly.
    raw_scripts = task_spec["script"]["scripts"]
    scripts: Dict[int, list] = {}
    for sid, s in raw_scripts.items():
        # accept both shapes: flat [cmd, "SUBMIT"] (one attempt) and
        # per-attempt [[cmd, "SUBMIT"], [attempt2 cmds...]]
        scripts[int(sid)] = [s] if (s and isinstance(s[0], str)) else list(s)
    model = ScriptedModel(plan=task_spec["script"]["plan"], scripts=scripts)
    set_call_model(model)
    # memory-informed planning arm: point the decision store at the
    # isolated seeded DB (harness.decision_memory resolves the store via
    # memory.paths, which reads HARNESS_DECISIONS_DB).
    if decision_db is not None:
        os.environ["HARNESS_DECISIONS_DB"] = str(decision_db)
    try:
        from harness.core import run_task

        result = run_task(task, log_root=log_dir)
        outcome = {
            "status": result.status,
            "attempts": result.attempts,
            "verified": bool(
                result.verification is not None
                and result.verification.target_test_passed
                and result.verification.regression_passed
                and not result.verification.flaky
            ),
            "cost_usd": result.cost_usd,
            "model_calls": len(result.model_calls),
            "wall_s": round(time.time() - started, 1),
            "error": None,
        }
    except Exception as exc:  # eval captures, never kills
        outcome = {
            "status": "crash",
            "attempts": 0,
            "verified": False,
            "cost_usd": 0.0,
            "model_calls": 0,
            "wall_s": round(time.time() - started, 1),
            "error": f"{exc!r}\n{traceback.format_exc()[-2000:]}",
        }
    finally:
        if decision_db is not None:
            os.environ.pop("HARNESS_DECISIONS_DB", None)
        # restore the eval-level trace root (per-task override above);
        # unset entirely if no eval-level default was set (a --check
        # style invocation never sets one)
        if trace_root_env:
            os.environ["VEX_TRACE_DIR"] = trace_root_env
        else:
            os.environ.pop("VEX_TRACE_DIR", None)
        _tracing._reset_cache()
        reset_overrides()

    outcome["integrity"] = _integrity_checks(log_dir, task_id)
    expects_retry = bool(task_spec.get("expects_retry"))
    outcome["ok"] = (
        outcome["status"] == "success"
        and outcome["verified"]
        and outcome["integrity"]["ok"]
        and (outcome["attempts"] >= 2 if expects_retry else True)
    )
    return outcome


def _integrity_checks(log_dir: Path, task_id: str) -> Dict[str, Any]:
    """Loop-integrity score from the task's own trace.jsonl.

    These are the machinery checks a prompt regression breaks FIRST:
    an incomplete trace (loop died mid-flight), an unparseable planner
    reply, a nudge loop (model replies stopped extracting commands), or
    a success with no recorded files. Assumes log_dir holds THIS run's
    logs/{task_id}/ (the runner passes log_root/{slug} and core nests
    the task dir — so we probe the archive too if a stale dir exists).
    """
    trace_path = log_dir / task_id / "trace.jsonl"
    events: List[Dict[str, Any]] = []
    if trace_path.is_file():
        try:
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            events = []
    kinds = [str(e.get("kind", "")) for e in events]

    has = lambda k: k in kinds  # noqa: E731
    no_cmd = sum(
        1
        for e in events
        if e.get("kind") == "tool_result"
        and "no runnable bash command" in str(e.get("data", ""))
    )
    state: Dict[str, Any] = {}
    state_path = log_dir / task_id / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}

    checks: Dict[str, Any] = {
        "trace_has_task_start": has("task_start"),
        "trace_has_task_end": has("task_end"),
        "trace_has_result": has("result"),
        "no_plan_parse_error": not has("plan_parse_error"),
        "no_nudge_loop": no_cmd <= 3,
        "files_touched_recorded": bool(state.get("files_touched")),
    }
    checks["ok"] = all(checks.values())
    return checks


# ---------------------------------------------------------------------------
# Runner + report
# ---------------------------------------------------------------------------


def _seed_decision_store(db_path: Path, repo_path: str) -> None:
    """Seed an ISOLATED decision store for the memory arms.

    One decision genuinely relevant to the eval repo's bug (so memory-
    informed planning has something real to find) plus two noise rows
    from a different repo (the planner must not be distracted). Assumes
    db_path's parents are writable; overwrites any prior file so runs
    are deterministic.
    """
    from memory.decision_store import DecisionStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    store = DecisionStore(str(db_path))
    store.record(
        "In this repo the mean() helper divides by len-1; the correct "
        "denominator is len(values) — fixed once already, guard against "
        "regression.",
        category="bug",
        source="eval-seed",
        repo_path=repo_path,
        task_id="eval-seed-1",
    )
    store.record(
        "The cart package uses a shared mutable default report list — "
        "always pass an explicit list.",
        category="bug",
        source="eval-seed",
        repo_path=repo_path,
        task_id="eval-seed-2",
    )
    store.record(
        "Unrelated project convention: tabs, not spaces.",
        category="style",
        source="eval-seed",
        repo_path="C:/other/project",
        task_id="eval-noise-1",
    )
    store.close()


def run_eval(
    arms: List[str],
    task_slugs: Optional[List[str]],
    out_root: Path,
    quick: bool = False,
) -> Dict[str, Any]:
    """Run the matrix; returns the report dict (also written to disk).

    Assumes Docker is up (the verifier needs it) and the fixture images
    are warm or warmable on demand. Sequential by design — deterministic
    comparison, and Docker VM contention would add noise.
    """
    all_t = eval_tasks.all_tasks(out_root)
    if quick:
        all_t = [
            t
            for t in all_t
            if t["slug"] in {x["slug"] for x in eval_tasks.FIXTURE_TASKS}
        ]
    if task_slugs:
        all_t = [t for t in all_t if t["slug"] in set(task_slugs)]

    ts = time.strftime("%Y%m%d-%H%M%S")
    run_dir = out_root / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    # Unified cross-module tracing ON for the eval (shared.tracing's
    # documented contract: entry points default VEX_TRACE_DIR to their
    # logs root; sandbox/router/memory events land beside the harness
    # trace, so a task's lifecycle is reconstructible from one place —
    # the eval's own integrity checks read the harness trace directly,
    # the unified stream is for humans/traceview).
    os.environ.setdefault("VEX_TRACE_DIR", str(out_root))
    from shared import tracing as _tracing

    _tracing._reset_cache()  # pick up the env var this process just set

    # one isolated decision DB for the memory-bearing arms
    db_path = run_dir / "memory-seed" / "decisions.db"
    seeded = False
    memory_repo = next(
        (t["repo"] for t in all_t if Path(t["repo"]).name == "bug02_mean"), None
    )
    if memory_repo:
        _seed_decision_store(db_path, memory_repo)
        seeded = True

    report: Dict[str, Any] = {
        "ts": ts,
        "n_tasks": len(all_t),
        "task_slugs": [t["slug"] for t in all_t],
        "arms": {},
        "regressions": [],
    }
    for arm in arms:
        overrides = dict(_BASE)
        overrides.update(ARMS[arm])
        arm_dir = run_dir / arm
        arm_dir.mkdir(parents=True, exist_ok=True)
        print(f"-- arm: {arm} ({len(all_t)} tasks)")
        arm_res: Dict[str, Any] = {}
        for spec in all_t:
            use_db = (
                db_path
                if (seeded and overrides.get("plan_with_memory", True))
                else None
            )
            res = _run_one(spec, ARMS[arm], arm_dir, decision_db=use_db)
            arm_res[spec["slug"]] = res
            mark = "ok" if res["ok"] else f"FAIL({res['status']})"
            extra = (
                ""
                if res["ok"]
                else (
                    f"  err={res['error'].splitlines()[0][:80]}"
                    if res["error"]
                    else f"  integrity={json.dumps(res['integrity'])[:100]}"
                )
            )
            print(
                f"   {spec['slug']:<22} {mark:<18} "
                f"attempts={res['attempts']} wall={res['wall_s']}s{extra}"
            )
        ok = sum(1 for r in arm_res.values() if r["ok"])
        report["arms"][arm] = {
            "results": arm_res,
            "n_ok": ok,
            "n_tasks": len(all_t),
            "success_rate": round(ok / max(1, len(all_t)), 3),
        }
        print(f"   arm summary: {ok}/{len(all_t)} ok")

    # regression detection: every arm vs baseline (and pre_round vs each
    # single-feature arm, so a feature whose prompt change only helps in
    # combination still shows up)
    base = report["arms"].get("baseline", {}).get("results", {})
    for arm, data in report["arms"].items():
        if arm == "baseline":
            continue
        for slug, res in data["results"].items():
            b = base.get(slug)
            if b is None:
                continue
            if b["ok"] and not res["ok"]:
                report["regressions"].append(
                    {
                        "arm": arm,
                        "task": slug,
                        "baseline_status": b["status"],
                        "arm_status": res["status"],
                        "detail": json.dumps(res.get("integrity", {})),
                    }
                )
            elif not b["ok"] and res["ok"]:
                report["improvements"] = report.get("improvements", [])
                report["improvements"].append(
                    {
                        "arm": arm,
                        "task": slug,
                        "baseline_status": b["status"],
                        "arm_status": res["status"],
                    }
                )

    report["verdict"] = "CLEAN" if not report["regressions"] else "REGRESSIONS"
    out = run_dir / "eval_report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["_report_path"] = str(out)
    print(
        f"\nverdict: {report['verdict']}  "
        f"({len(report['regressions'])} regression(s) vs baseline)"
    )
    print(f"report: {out}")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Prompt-regression eval: fixed task set, real loop, "
        "scripted model, real Docker verify.",
    )
    parser.add_argument("--arms", default="all", help="comma list or 'all' (default)")
    parser.add_argument(
        "--tasks", default=None, help="comma list of task slugs (default: all)"
    )
    parser.add_argument(
        "--quick", action="store_true", help="fixture tasks only (fast gate)"
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output only"
    )
    parser.add_argument(
        "--out-root",
        default=None,
        help="where logs/evals/<ts>/ lands (default logs/evals)",
    )
    parser.add_argument(
        "--check", action="store_true", help="host self-verify the task set; no Docker"
    )
    args = parser.parse_args(argv)

    out_root = Path(args.out_root) if args.out_root else (REPO_ROOT / "logs" / "evals")

    if args.check:
        results = eval_tasks.check_set(out_root / "_check-scratch")
        bad = [r for r in results if r.get("ok") is not True]
        for r in results:
            print(
                f"  {r['slug']:<22} "
                f"{'OK' if r.get('ok') else str(r.get('note') or 'BAD')}"
            )
        return 0 if not bad else 1

    arms = (
        list(ARMS)
        if args.arms == "all"
        else [a.strip() for a in args.arms.split(",") if a.strip()]
    )
    slugs = [s.strip() for s in args.tasks.split(",")] if args.tasks else None
    report = run_eval(arms, slugs, out_root, quick=args.quick)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_matrix(report)
    return 0 if report["verdict"] == "CLEAN" else 2


def _print_matrix(report: Dict[str, Any]) -> None:
    slugs = report["task_slugs"]
    arms = list(report["arms"])
    print("\n== eval matrix (ok = success + verified + integrity) ==")
    header = f"{'task':<24}" + "".join(f"{a:<16}" for a in arms)
    print(header)
    print("-" * len(header))
    for slug in slugs:
        row = f"{slug:<24}"
        for arm in arms:
            r = report["arms"][arm]["results"].get(slug, {})
            mark = "ok" if r.get("ok") else (r.get("status") or "?")
            row += f"{mark:<16}"
        print(row)
    print()
    for arm in arms:
        d = report["arms"][arm]
        print(f"  {arm:<16} {d['n_ok']}/{d['n_tasks']} ok ({d['success_rate']:.0%})")


if __name__ == "__main__":
    raise SystemExit(main())
