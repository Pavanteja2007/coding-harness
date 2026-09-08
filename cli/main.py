"""The ``harness`` CLI — Terminal 4's human-facing entry point
(INTERFACES.md Boundary 6).

Commands:
    harness fix --repo <path> --issue <text> [--model <name>] [...]
        -> harness.core.run_task (single-task mode, no scheduler)
    harness run-benchmark --subset <name> [--concurrency <n>] [...]
        -> runtime.scheduler.run (stub until Terminal 3 lands it)
    harness status --task-id <id>
        -> reads logs/{task_id}/state.json and prints a summary
    harness memory query-decisions / record / query-structure
        -> thin wrappers over the memory layer (nice for demos + smoke
           tests; MCP remains the primary programmatic interface)

argparse (project tech lock: "a plain Python CLI"). Every command prints
something sensible even when a dependency is stubbed or misconfigured —
a CLI that crashes with a traceback fails the user worse than one that
explains what's missing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli import deps
from memory.paths import decisions_db_path, default_logs_dir


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_task(args: argparse.Namespace, extra_config: Optional[Dict[str, Any]] = None) -> "Task":
    """Build the Task for a fix run (Boundary 3's input).

    Assumes --repo exists (validated by the subcommand) and that model /
    provider / test knobs from the CLI map straight into Task.config.
    """
    from shared.types import Task

    config: Dict[str, Any] = {}
    if getattr(args, "model", None):
        config["model"] = args.model
    if getattr(args, "provider", None):
        config["provider"] = args.provider
    if getattr(args, "target_test", None):
        config["target_test"] = args.target_test
    if getattr(args, "test_command", None):
        config["test_command"] = args.test_command
    if getattr(args, "max_retries", None) is not None:
        config["max_retries"] = args.max_retries
    if getattr(args, "budget", None) is not None:
        config["budget_cap_usd"] = args.budget
    if getattr(args, "api_key", None):
        config["api_key"] = args.api_key
    if getattr(args, "api_base", None):
        config["api_base"] = args.api_base
    if getattr(args, "adaptive_routing", None):
        config["adaptive_routing"] = True
    if getattr(args, "protected", None):
        config["protected_paths"] = list(args.protected)
    config.update(extra_config or {})
    return Task(
        task_id=getattr(args, "task_id", None) or f"fix-{uuid.uuid4().hex[:8]}",
        repo_path=str(Path(args.repo).resolve()),
        issue_text=args.issue,
        config=config,
    )


def _print_result(result: "TaskResult", elapsed: float) -> None:
    """Human-readable summary of one TaskResult."""
    print(f"\n=== task {result.task_id}: {result.status} ===")
    print(f"attempts:      {result.attempts}")
    print(f"cost (USD):    {result.cost_usd:.6f}")
    print(f"model calls:   {len(result.model_calls)}")
    print(f"log path:      {result.log_path}")
    if elapsed >= 1:
        print(f"elapsed:       {elapsed:.1f}s")
    if result.verification is not None:
        v = result.verification
        print(f"target test:   {'PASS' if v.target_test_passed else 'FAIL'}")
        print(f"regression:   {'PASS' if v.regression_passed else 'FAIL'}")
        print(f"flaky:         {v.flaky}")
    if result.diff:
        print("\n--- diff ---")
        print(result.diff)
    elif result.status == "success":
        print("(no diff — target already passed before any edit)")
    elif result.status == "failed":
        print("(no passing diff to show)")
    if result.log_path and Path(result.log_path).exists():
        print(f"\nfull trace:    {result.log_path}")


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------

def cmd_fix(args: argparse.Namespace) -> int:
    """Run one bug-fix task through the real harness, single-task mode."""
    from shared.types import TaskResult  # noqa: F401  (type used in _print_result)

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: --repo is not a directory: {args.repo}", file=sys.stderr)
        return 2
    if not args.issue:
        print("error: --issue is required", file=sys.stderr)
        return 2
    issue = args.issue
    if issue.startswith("@"):
        # --issue @report.txt — read the bug report from a file
        issue_path = Path(issue[1:])
        try:
            issue = issue_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"error: cannot read issue file {issue_path}: {exc}", file=sys.stderr)
            return 2
        if not issue:
            print(f"error: issue file is empty: {issue_path}", file=sys.stderr)
            return 2
        args.issue = issue

    task = _make_task(args)
    print(f"fixing {task.repo_path}")
    print(f"task_id: {task.task_id}")
    print(f"issue:   {args.issue[:200]}")
    _set_router_context(task)  # same pattern as runtime.worker (per-process router config)
    run_task = deps.get_run_task()
    started = time.time()
    try:
        result = run_task(task, **({"log_root": Path(args.log_root)} if args.log_root else {}))
    except Exception as exc:
        print(f"\nerror: run_task crashed: {exc}", file=sys.stderr)
        print("(if this mentions litellm/API keys: fix runs need a working "
              "model backend — set --api-key or see harness/AGENTS.md)", file=sys.stderr)
        return 1
    finally:
        _clear_router_context()
    elapsed = time.time() - started
    _print_result(result, elapsed)
    return 0 if result.status == "success" else 1


def _set_router_context(task: "Task") -> None:
    """Install the router context for an in-process fix run.

    runtime.worker does exactly this in its subprocess before calling
    run_task; the CLI's in-process path needs the same so config like
    api_base / adaptive_routing / model_tiers actually reaches the model
    router. Best-effort: if runtime's router is unavailable (stub phase),
    the call proceeds without a context.
    """
    try:
        from runtime.model_router import set_call_context

        cfg = task.config
        set_call_context({
            "adaptive_routing": cfg.get("adaptive_routing", False),
            "model_tiers": cfg.get("model_tiers"),
            "difficulty_estimator": cfg.get("difficulty_estimator", "heuristic"),
            "difficulty_llm": cfg.get("difficulty_llm"),
            "provider": cfg.get("provider"),
            "model": cfg.get("model"),
            "api_key": cfg.get("api_key"),
            "api_base": cfg.get("api_base"),
            "use_mock_provider": cfg.get("use_mock_provider", False),
            "rate_limit_retries": cfg.get("rate_limit_retries", 4),
            "rate_limit_backoff_s": cfg.get("rate_limit_backoff_s", 15.0),
        }, ledger_dir=str(Path(task.config.get("_ledger_dir", "logs/router-ledger.jsonl"))))
    except ImportError:
        pass


def _clear_router_context() -> None:
    """Drop the router context after the run (keep the process clean)."""
    try:
        from runtime.model_router import set_call_context

        set_call_context(None)
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# run-benchmark
# ---------------------------------------------------------------------------

def cmd_run_benchmark(args: argparse.Namespace) -> int:
    """Fan out a task set through the scheduler boundary."""
    subset = args.subset
    tasks = _load_subset(subset, args)
    if tasks is None:
        return 2
    if not tasks:
        print(f"benchmark subset {subset!r}: no tasks found")
        return 0

    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    print(f"benchmark: {subset}  ({len(tasks)} task(s), concurrency {args.concurrency})")
    scheduler_run = deps.get_scheduler_run()
    started = time.time()
    try:
        results = _call_scheduler(scheduler_run, tasks, args.concurrency, log_root)
    except Exception as exc:
        print(f"error: scheduler crashed: {exc}", file=sys.stderr)
        return 1
    elapsed = time.time() - started

    n_ok = sum(1 for r in results if r.status == "success")
    n_fail = sum(1 for r in results if r.status == "failed")
    n_err = sum(1 for r in results if r.status in ("error", "timeout"))
    cost = sum(getattr(r, "cost_usd", 0.0) for r in results)
    print(f"\n=== benchmark {subset}: done in {elapsed:.1f}s ===")
    print(f"success {n_ok} / {len(results)}   failed {n_fail}   error/timeout {n_err}")
    if results:
        print(f"total cost: ${cost:.4f}")
    for r in results:
        print(f"  {r.task_id}: {r.status} ({r.attempts} attempt(s), ${r.cost_usd:.4f})")
    return 0


def _call_scheduler(scheduler_run, tasks, concurrency: int, log_root: Path):
    """Call the scheduler boundary handling both real and stub shapes.

    Real (Terminal 3): run(tasks, concurrency, logs_root) -> dict.
    Stub (cli/_stubs): run(tasks, concurrency, run_task=...) -> list.
    Signature probing keeps this honest — a TypeError from inside a task
    is never mistaken for a signature mismatch.
    """
    import inspect

    params = inspect.signature(scheduler_run).parameters
    kwargs: Dict[str, Any] = {"concurrency": max(1, int(concurrency))}
    if "logs_root" in params:
        kwargs["logs_root"] = str(log_root)
    result = scheduler_run(tasks, **kwargs)
    if isinstance(result, dict):  # real scheduler: {task_id: TaskResult}
        return [result[t.task_id] for t in tasks if t.task_id in result]
    return list(result)


def _load_subset(subset: str, args: argparse.Namespace) -> Optional[List["Task"]]:
    """Materialize benchmark tasks for a named subset.

    Phase 1 supports two subset kinds (no SWE-bench yet — deferred per spec):
    - ``smoke``: one trivial Task against a bundled fixture repo with the
      mock model, for end-to-end plumbing checks.
    - a JSON file path: [{"repo": ..., "issue": ..., "target_test": ...}, ...]
    """
    from shared.types import Task

    if subset == "smoke":
        fixture = Path(__file__).parent / "fixtures" / "smoke_repo"
        if not fixture.is_dir():
            print(f"error: smoke fixture repo missing: {fixture}", file=sys.stderr)
            return None
        cfg: Dict[str, Any] = {
            # Offline plumbing smoke: Terminal 3's worker honors
            # use_fake_harness for subprocess-isolated scheduler runs.
            "use_fake_harness": True,
            "fake_steps": ["plan", "edit", "verify"],
            "fake_step_delay_s": 0.05,
            "max_wallclock_s": 60.0,
            "crash_retries": 0,
        }
        if args.model:
            cfg["model"] = args.model
        if args.provider:
            cfg["provider"] = args.provider
        return [
            Task(
                task_id=f"smoke-{uuid.uuid4().hex[:8]}",
                repo_path=str(fixture.resolve()),
                issue_text="The mean() function in mathutil.py returns the "
                           "sum instead of the arithmetic mean. Fix it so "
                           "tests/test_mathutil.py::test_mean passes.",
                config=cfg,
            )
        ]

    p = Path(subset)
    if not p.is_file():
        print(f"error: unknown subset {subset!r} (expected 'smoke' or a JSON file)", file=sys.stderr)
        return None
    try:
        # utf-8-sig: tolerates the BOM Windows editors (PowerShell, Notepad) prepend
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read subset file {subset}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, list):
        print(f"error: subset file must be a JSON list of task objects", file=sys.stderr)
        return None

    tasks: List[Task] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "repo" not in item or "issue" not in item:
            print(f"error: subset entry {i} needs 'repo' and 'issue'", file=sys.stderr)
            return None
        cfg = dict(item.get("config") or {})
        if args.model:
            cfg["model"] = args.model
        if args.provider:
            cfg["provider"] = args.provider
        if item.get("target_test"):
            cfg["target_test"] = item["target_test"]
        if item.get("test_command"):
            cli_cfg_key = item["test_command"]
            cfg["test_command"] = cli_cfg_key
        tasks.append(
            Task(
                task_id=item.get("task_id") or f"bench-{i}-{uuid.uuid4().hex[:6]}",
                repo_path=str(Path(item["repo"]).expanduser()),
                issue_text=item["issue"],
                config=cfg,
            )
        )
    return tasks


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    """Print a human-readable summary of logs/{task_id}/state.json."""
    task_id = args.task_id
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    state_file = log_root / task_id / "state.json"

    if not state_file.is_file():
        # Helpful hint: what actually exists under the log root?
        hint = ""
        if log_root.is_dir():
            ids = sorted(p.name for p in log_root.iterdir() if p.is_dir())
            if ids:
                shown = ", ".join(ids[:8])
                hint = f"\navailable task dirs under {log_root}: {shown}"
        print(f"no state file at {state_file}{hint}", file=sys.stderr)
        return 2

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {state_file}: {exc}", file=sys.stderr)
        return 2

    plan = state.get("plan") or []
    completed = state.get("completed_steps") or []
    remaining = state.get("remaining_plan") or []
    files_touched = state.get("files_touched") or []
    decisions = state.get("decisions") or []

    print(f"=== task {state.get('task_id', task_id)} ===")
    print(f"state file: {state_file}")
    done = "all" if plan and not remaining else f"{len(completed)}/{len(plan)}"
    print(f"progress:  {done} step(s) complete")
    print(f"plan ({len(plan)}):")
    for step in plan:
        mark = "x" if step in completed else " "
        print(f"  [{mark}] {step}")
    if files_touched:
        print(f"files touched ({len(files_touched)}):")
        for f in files_touched:
            print(f"  - {f}")
    if decisions:
        print(f"decisions ({len(decisions)}):")
        for d in decisions:
            print(f"  - {d}")
    if remaining:
        print(f"remaining ({len(remaining)}):")
        for r in remaining:
            print(f"  - {r}")

    # Enrichment: result status + cost from the trace if present
    trace_file = state_file.parent / "trace.jsonl"
    if trace_file.is_file():
        result = _tail_result_from_trace(trace_file)
        if result:
            data = result.get("data") or {}
            print(f"result:    {data.get('status', '?')}")
            if "cost_usd" in data:
                print(f"cost:      ${data.get('cost_usd', 0):.4f}")
            if data.get("note"):
                print(f"note:      {data['note']}")
    return 0


def _tail_result_from_trace(trace_file: Path) -> Optional[Dict[str, Any]]:
    """Last 'result' or 'task_end' event from a trace.jsonl (small files:
    full read is fine; traces can be large, so scan from the end)."""
    try:
        text = trace_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("kind") in ("result", "task_end"):
            return obj
    return None


# ---------------------------------------------------------------------------
# memory subcommands (thin wrappers — MCP is the primary interface)
# ---------------------------------------------------------------------------

def _store():
    from memory.decision_store import DecisionStore

    return DecisionStore(str(decisions_db_path()))


def cmd_memory_record(args: argparse.Namespace) -> int:
    """Record a decision into the persistent store."""
    store = _store()
    rid = store.record(args.text, category=args.category)
    if rid is None:
        print("error: empty decision text", file=sys.stderr)
        return 2
    print(f"recorded decision #{rid}: {args.text}")
    return 0


def cmd_memory_decisions(args: argparse.Namespace) -> int:
    """Query the decision store (ranked keyword match; empty = recent)."""
    store = _store()
    results = store.search(args.query or "", limit=args.limit)
    from memory.decision_store import format_decisions

    print(format_decisions(results, args.query or ""))
    return 0


def cmd_memory_structure(args: argparse.Namespace) -> int:
    """Query the code graph of a repo."""
    if not args.repo:
        print("error: --repo is required for query-structure", file=sys.stderr)
        return 2
    from memory.code_graph import CodeGraph

    try:
        graph = CodeGraph(args.repo)
    except NotADirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(graph.query(args.query or "help"))
    return 0


def cmd_memory_ingest(args: argparse.Namespace) -> int:
    """One-shot ingestion of all state files under a logs dir."""
    store = _store()
    new = store.poll(args.logs_dir or str(default_logs_dir()))
    print(f"ingested {new} new decision(s) from {args.logs_dir or default_logs_dir()}")
    return 0


# ---------------------------------------------------------------------------
# dashboard (read-only, stretch item 40)
# ---------------------------------------------------------------------------

def cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the read-only dashboard over existing logs (blocks)."""
    try:
        from dashboard.server import serve
    except ImportError as exc:
        print(f"error: dashboard module unavailable: {exc}", file=sys.stderr)
        return 2
    serve(
        logs_dir=args.logs_dir or str(default_logs_dir()),
        host=args.host,
        port=args.port,
        refresh_s=args.refresh_s,
        open_browser=not args.no_browser,
    )
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _add_task_config_args(parser: argparse.ArgumentParser) -> None:
    """CLI flags that map into Task.config (project convention: config
    values ride on the Task, never hardcoded constants)."""
    parser.add_argument("--model", help="model name (default: harness config)")
    parser.add_argument("--provider", help="litellm provider (default: harness config)")
    parser.add_argument("--api-key", help="API key (or env; prefer env)")
    parser.add_argument("--api-base", help="custom endpoint base URL (BYO router/gateway)")
    parser.add_argument("--adaptive-routing", action="store_true",
                        help="enable adaptive model routing (runtime router)")
    parser.add_argument("--target-test", help="pytest node id of the target test")
    parser.add_argument("--test-command", help="test command override (default: autodetect)")
    parser.add_argument("--max-retries", type=int, help="max full attempts per task")
    parser.add_argument("--budget", type=float, help="per-task cost cap in USD")
    parser.add_argument("--protected", action="append", help="protected path glob (repeatable)")
    parser.add_argument("--log-root", default=None, help="task log root (default: ./logs)")


def build_parser() -> argparse.ArgumentParser:
    """Assemble the full CLI parser (exported for tests)."""
    parser = argparse.ArgumentParser(
        prog="harness",
        description="AI coding agent harness — fix bugs, run benchmarks, "
                    "query memory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fix
    p_fix = sub.add_parser("fix", help="fix one bug in one repo (single-task mode)")
    p_fix.add_argument("--repo", required=True, help="path to the repo to fix")
    p_fix.add_argument("--issue", required=True, help="bug report text (or @file with the report)")
    p_fix.add_argument("--task-id", default=None, help="override auto task_id")
    _add_task_config_args(p_fix)
    p_fix.set_defaults(func=cmd_fix)

    # run-benchmark
    p_bench = sub.add_parser("run-benchmark", help="run a benchmark subset through the scheduler")
    p_bench.add_argument("--subset", required=True,
                         help="'smoke' or a JSON file of tasks")
    p_bench.add_argument("--concurrency", type=int, default=10)
    _add_task_config_args(p_bench)
    p_bench.set_defaults(func=cmd_run_benchmark)

    # status
    p_status = sub.add_parser("status", help="show a task's structured state summary")
    p_status.add_argument("--task-id", required=True)
    p_status.add_argument("--log-root", default=None, help="task log root (default: ./logs)")
    p_status.set_defaults(func=cmd_status)

    # memory
    p_mem = sub.add_parser("memory", help="decision/structure memory utilities")
    mem_sub = p_mem.add_subparsers(dest="memory_command", required=True)

    p_rec = mem_sub.add_parser("record", help="record a decision")
    p_rec.add_argument("text", help="the decision/fact to remember")
    p_rec.add_argument("--category", default="general")
    p_rec.set_defaults(func=cmd_memory_record)

    p_dec = mem_sub.add_parser("query-decisions", help="query decision memory")
    p_dec.add_argument("query", nargs="?", default="")
    p_dec.add_argument("--limit", type=int, default=20)
    p_dec.set_defaults(func=cmd_memory_decisions)

    p_str = mem_sub.add_parser("query-structure", help="query a repo's code graph")
    p_str.add_argument("--repo", required=True)
    p_str.add_argument("query", nargs="?", default="help")
    p_str.set_defaults(func=cmd_memory_structure)

    p_ing = mem_sub.add_parser("ingest", help="ingest state.json decisions under a logs dir")
    p_ing.add_argument("logs_dir", nargs="?", default=None)
    p_ing.set_defaults(func=cmd_memory_ingest)

    # dashboard (read-only web view of existing logs — stretch item 40)
    p_dash = sub.add_parser("dashboard", help="serve the read-only run dashboard (web)")
    p_dash.add_argument("--logs-dir", default=None, help="logs root (default: ./logs)")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8765)
    p_dash.add_argument("--refresh-s", type=float, default=5.0)
    p_dash.add_argument("--no-browser", action="store_true")
    p_dash.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (console script ``harness`` / ``python -m cli``).

    Assumes argv is None (real invocation: sys.argv) or a list for tests.
    Returns a process exit code: 0 success, 1 task failure, 2 usage error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
