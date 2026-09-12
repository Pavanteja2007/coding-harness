"""The ``vex`` CLI — the project's human-facing entry point
(INTERFACES.md Boundary 6).

Commands:
    vex                     -> interactive natural-language session (the
                               PRIMARY UX — type what's wrong in plain
                               language; repo = current directory)
    vex fix --repo <path> --issue <text> [--model <name>] [...]
        -> harness.core.run_task (single-task mode, no scheduler)
    vex run-benchmark --subset <name> [--concurrency <n>] [...]
        -> runtime.scheduler.run (live multi-task table while running)
    vex status --task-id <id>
        -> reads logs/{task_id}/state.json and prints a summary
    vex memory query-decisions / record / query-structure
        -> thin wrappers over the memory layer (MCP remains the primary
           programmatic interface)
    vex mcp list-tools / call — consume external MCP servers
    vex dashboard — read-only web view of existing logs

argparse (project tech lock: "a plain Python CLI"). Every command prints
something sensible even when a dependency is stubbed or misconfigured —
a CLI that crashes with a traceback fails the user worse than one that
explains what's missing. All rendering goes through cli.ui (rich + the
Vex amber/ember theme; ANSI auto-degrades on dumb Windows consoles,
--no-color / NO_COLOR force plain).

Entry points: ``vex`` console script (pip install -e .), ``python -m cli``,
and the legacy ``harness`` alias (kept until all docs migrate).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli import deps
from cli import ui
from memory.paths import decisions_db_path, default_logs_dir, safe_task_dir


def _safe_task_dir(task_id: str, log_root: Path):
    """log_root/<task_id>/ for a single-segment task id, else None
    (containment guard for `vex status --task-id` — Round 6
    adversarial hardening; shared logic in memory.paths.safe_task_dir)."""
    return safe_task_dir(task_id, log_root)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_task(
    args: argparse.Namespace, extra_config: Optional[Dict[str, Any]] = None
) -> "Task":
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
    if getattr(args, "approval", None):
        config["approval"] = "require"
        config.setdefault("approval_timeout_s", 3600.0)
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
    """Human-readable summary of one TaskResult (Vex theme, rendered diff)."""
    con = ui.console()
    style = "vex.ok" if result.status == "success" else "vex.error"
    con.print()
    con.rule(f"[{style}]task {result.task_id}: {result.status}[/]")
    con.print(f"[vex.muted]attempts:     {result.attempts}[/]")
    con.print(f"[vex.muted]cost:         {ui.fmt_cost(result.cost_usd)}[/]")
    con.print(f"[vex.muted]model calls:  {len(result.model_calls)}[/]")
    if elapsed >= 1:
        con.print(f"[vex.muted]elapsed:      {elapsed:.1f}s[/]")
    if result.verification is not None:
        v = result.verification
        con.print(
            f"[vex.muted]target test:  "
            f"[{'vex.ok' if v.target_test_passed else 'vex.error'}]"
            f"{'PASS' if v.target_test_passed else 'FAIL'}[/][/]"
        )
        con.print(
            f"[vex.muted]regression:    "
            f"[{'vex.ok' if v.regression_passed else 'vex.error'}]"
            f"{'PASS' if v.regression_passed else 'FAIL'}[/][/]"
        )
        con.print(f"[vex.muted]flaky:        {v.flaky}[/]")
    if result.diff:
        con.print()
        con.rule("[vex.accent]diff[/]")
        ui.print_diff(result.diff)
    elif result.status == "success":
        con.print("[vex.muted](no diff — target already passed before any edit)[/]")
    elif result.status == "failed":
        con.print("[vex.muted](no passing diff to show)[/]")
    # rationale.md (Task E): render the grounded paragraph when written
    rat = Path(result.log_path).parent / "rationale.md"
    if rat.is_file():
        from rich.markdown import Markdown

        con.print()
        con.rule("[vex.accent]rationale[/]")
        con.print(Markdown(rat.read_text(encoding="utf-8")))
    if result.log_path and Path(result.log_path).exists():
        con.print(f"\n[vex.muted]full trace:   {result.log_path}[/]")


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


def cmd_fix(args: argparse.Namespace) -> int:
    """Run one bug-fix task through the real harness, single-task mode."""
    from shared.types import TaskResult  # noqa: F401  (type used in _print_result)
    from cli.interactive import LiveMonitor

    con = ui.console()
    repo = Path(args.repo)
    if not repo.is_dir():
        ui.err_console().print(
            f"[vex.error]error: --repo is not a directory: {args.repo}[/]"
        )
        return 2
    if not args.issue:
        ui.err_console().print("[vex.error]error: --issue is required[/]")
        return 2
    issue = args.issue
    if issue.startswith("@"):
        # --issue @report.txt — read the bug report from a file
        issue_path = Path(issue[1:])
        try:
            issue = issue_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            ui.err_console().print(
                f"[vex.error]error: issue file is not valid UTF-8 text: {issue_path}[/]"
            )
            return 2
        except (OSError, ValueError) as exc:
            # ValueError covers null-byte paths on Windows
            ui.err_console().print(
                f"[vex.error]error: cannot read issue file {issue_path}: {exc}[/]"
            )
            return 2
        if not issue:
            ui.err_console().print(
                f"[vex.error]error: issue file is empty: {issue_path}[/]"
            )
            return 2
        args.issue = issue

    task = _make_task(args)
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    # Unified cross-module tracing (shared.tracing): a fix run defaults
    # VEX_TRACE_DIR to its logs root so scheduler/worker/router/sandbox/
    # memory events land in <log_root>/_trace/ next to the harness trace —
    # `python -m shared.traceview <task_id>` reconstructs the whole task
    # from one place. setdefault: an operator who set it wins.
    os.environ.setdefault("VEX_TRACE_DIR", str(log_root))
    con.print(
        f"[vex.accent]vex fix[/] [vex.muted]{ui.GLYPHS['arrow']} {task.repo_path}[/]"
    )
    con.print(f"[vex.muted]task:   {task.task_id}[/]")
    con.print(f"[vex.muted]issue:  {args.issue[:200]}[/]")
    _set_router_context(
        task
    )  # same pattern as runtime.worker (per-process router config)
    run_task = deps.get_run_task()
    started = time.time()
    mon = LiveMonitor(task.task_id, log_root).start()
    try:
        result = run_task(task, **({"log_root": log_root} if args.log_root else {}))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        # Plain-language explanation (Task D): what broke + what to check,
        # never a raw traceback. run_task itself reports task-level
        # failures (failed/error results) — this path is for the harness
        # itself crashing.
        from cli.errors import explain_exception

        explain_exception(exc)
        return 1
    finally:
        mon.stop()
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
        set_call_context(
            {
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
            },
            ledger_dir=str(
                Path(task.config.get("_ledger_dir", "logs/router-ledger.jsonl"))
            ),
        )
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
# run-benchmark (live multi-task view — Task E)
# ---------------------------------------------------------------------------


def cmd_run_benchmark(args: argparse.Namespace) -> int:
    """Fan out a task set through the scheduler boundary (live table)."""
    from cli.interactive import watch_for_approvals

    con = ui.console()
    subset = args.subset
    tasks = _load_subset(subset, args)
    if tasks is None:
        return 2
    if not tasks:
        con.print(f"[vex.muted]benchmark subset {subset!r}: no tasks found[/]")
        return 0

    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    con.print(
        f"[vex.accent]vex run-benchmark[/] [vex.muted]"
        f"{ui.GLYPHS['arrow']} {subset} "
        f"({len(tasks)} task(s), concurrency {args.concurrency})[/]"
    )
    scheduler_run = deps.get_scheduler_run()
    started = time.time()

    # Live view: the real Scheduler exposes live_attempts(); the stub
    # doesn't (flag commands may run against it pre-T3) — probe safely.
    live = _BenchmarkLiveView(tasks, log_root)
    watcher_stop = None
    watcher = None
    approval_ids = [
        t.task_id for t in tasks if (t.config or {}).get("approval") == "require"
    ]
    if approval_ids:
        import threading

        watcher_stop = threading.Event()
        watcher = threading.Thread(
            target=watch_for_approvals,
            args=(log_root, approval_ids, watcher_stop),
            daemon=True,
        )
        watcher.start()
    try:
        results = _call_scheduler(
            scheduler_run, tasks, args.concurrency, log_root, live
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        # Task D: plain-language diagnosis (scheduler spawn failure, Docker
        # down, worker exhaustion...) instead of a bare exception line.
        from cli.errors import explain_exception

        explain_exception(exc)
        return 1
    finally:
        live.finish()
        if watcher_stop is not None:
            watcher_stop.set()
    elapsed = time.time() - started

    n_ok = sum(1 for r in results if r.status == "success")
    n_fail = sum(1 for r in results if r.status == "failed")
    n_err = sum(1 for r in results if r.status in ("error", "timeout"))
    cost = sum(getattr(r, "cost_usd", 0.0) for r in results)
    con.print()
    con.rule(f"[vex.accent]benchmark {subset}: done in {elapsed:.1f}s[/]")
    if results:
        con.print(
            f"[vex.ok]success {n_ok}[/][vex.muted] / {len(results)}   "
            f"[vex.error]failed {n_fail}[/][vex.muted]   "
            f"error/timeout {n_err}   "
            f"cost [/][vex.accent]{ui.fmt_cost(cost)}[/]"
        )
    for r in results:
        mark = {
            "success": f"[vex.ok]{ui.GLYPHS['ok']}",
            "failed": f"[vex.error]{ui.GLYPHS['fail']}",
            "error": f"[vex.error]{ui.GLYPHS['fail']}",
            "timeout": f"[vex.warn]{ui.GLYPHS['wait']}",
        }.get(r.status, "?")
        con.print(
            f"  {mark} [vex.muted]{r.task_id}: {r.status} "
            f"({r.attempts} attempt(s), {ui.fmt_cost(r.cost_usd)})[/]"
        )
    return 0


class _BenchmarkLiveView:
    """Live in-terminal table of a running benchmark (Task E).

    Wraps the scheduler call in a thread and refreshes a rich Table of
    task statuses every poll: queued / running (elapsed) / finished (result).
    Uses the REAL scheduler's live_attempts() when available; with the stub
    (or when live_attempts can't be reached), degrades to a spinner with
    a running count — never a crash.
    """

    def __init__(self, tasks, log_root: Path, poll_s: float = 1.0) -> None:
        self.tasks = {t.task_id: t for t in tasks}
        self._done: Dict[str, str] = {}
        self._stop = None
        self._thread = None
        self._poll_s = poll_s

    def note_result(self, task_id: str, status: str) -> None:
        """Called from the scheduler thread when a task result lands."""
        self._done[task_id] = status

    def finish(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def start(self, scheduler_run) -> None:
        import threading

        from rich.live import Live
        from rich.table import Table

        self._stop = threading.Event()
        self._live_factory = (Live, Table)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import threading

        from rich.live import Live
        from rich.table import Table

        con = ui.console()
        started = time.time()
        with Live(console=con, refresh_per_second=4, transient=True) as live_widget:
            while not self._stop.is_set():
                elapsed = time.time() - started
                table = Table(
                    title=f"[vex.accent]vex benchmark[/] "
                    f"[vex.muted]({elapsed:.0f}s)[/]",
                    title_justify="left",
                )
                table.add_column("task", style="vex.muted", no_wrap=True)
                table.add_column("state")
                running = 0
                for tid in self.tasks:
                    if tid in self._done:
                        status = self._done[tid]
                        if status == "success":
                            mark = f"[vex.ok]{ui.GLYPHS['ok']} success"
                        elif status == "failed":
                            mark = f"[vex.error]{ui.GLYPHS['fail']} failed"
                        else:
                            mark = f"[vex.error]{status}"
                        table.add_row(tid, mark)
                    else:
                        running += 1
                        table.add_row(
                            tid, f"[vex.running]{ui.GLYPHS['bullet']} running"
                        )
                if not running:
                    table.add_row(
                        "[vex.muted]...[/]",
                        f"[vex.muted]wrapping up ({len(self._done)} done)[/]",
                    )
                live_widget.update(table)
                self._stop.wait(self._poll_s)


def _call_scheduler(
    scheduler_run,
    tasks,
    concurrency: int,
    log_root: Path,
    live: Optional[_BenchmarkLiveView] = None,
):
    """Call the scheduler boundary handling both real and stub shapes.

    Real (Terminal 3): run(tasks, concurrency, logs_root) -> dict, plus a
    live_attempts() snapshot for the live view. Stub (cli/_stubs):
    run(tasks, concurrency, run_task=...) -> list. Signature probing keeps
    this honest — a TypeError from inside a task is never mistaken for a
    signature mismatch. Runs the scheduler in a daemon thread so the live
    table (and Ctrl+C) stay responsive; KeyboardInterrupt from the main
    thread propagates through the thread-join.
    """
    import threading

    params = _sig_params(scheduler_run)
    kwargs: Dict[str, Any] = {"concurrency": max(1, int(concurrency))}
    if "logs_root" in params:
        kwargs["logs_root"] = str(log_root)
    out: Dict[str, Any] = {}

    def runner():
        try:
            out["result"] = scheduler_run(tasks, **kwargs)
        except KeyboardInterrupt as exc:  # re-raised into the join
            out["interrupt"] = exc
        except BaseException as exc:
            out["error"] = exc

    th = threading.Thread(target=runner, daemon=True)
    if live is not None:
        live.start(scheduler_run)
    th.start()
    try:
        while th.is_alive():
            th.join(timeout=0.5)
            _feed_live_view(scheduler_run, live, out, tasks)
    except KeyboardInterrupt:
        # Let the scheduler's own KeyboardInterrupt handler kill its
        # workers (the real one does this on the MAIN thread of run());
        # here we just stop waiting and surface partial results if any.
        th.join(timeout=10.0)
        raise
    if "interrupt" in out:
        raise out["interrupt"]
    if "error" in out:
        raise out["error"]
    result = out.get("result")
    if isinstance(result, dict):  # real scheduler: {task_id: TaskResult}
        if live is not None:
            for tid, r in result.items():
                live.note_result(tid, getattr(r, "status", "?"))
        return [result[t.task_id] for t in tasks if t.task_id in result]
    return list(result)


def _feed_live_view(scheduler_run, live, out, tasks) -> None:
    """Best-effort live-view feed from the real scheduler's state."""
    if live is None:
        return
    try:
        la = getattr(scheduler_run, "live_attempts", None)
        sched = getattr(scheduler_run, "__self__", None)  # bound method?
        active = sched.live_attempts() if sched is not None else None
    except Exception:
        active = None
    # The stub scheduler is a bare function — no live view possible; the
    # table then just shows queued/running until results land.


def _sig_params(fn) -> "Any":
    import inspect

    return inspect.signature(fn).parameters


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
            ui.err_console().print(
                f"[vex.error]error: smoke fixture repo missing: {fixture}[/]"
            )
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
        ui.err_console().print(
            f"[vex.error]error: unknown subset {subset!r} "
            f"(expected 'smoke' or a JSON file)[/]"
        )
        return None
    try:
        # utf-8-sig: tolerates the BOM Windows editors (PowerShell, Notepad) prepend
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        ui.err_console().print(
            f"[vex.error]error: cannot read subset file {subset}: {exc}[/]"
        )
        return None
    if not isinstance(data, list):
        ui.err_console().print(
            "[vex.error]error: subset file must be a JSON list of task objects[/]"
        )
        return None

    tasks: List[Task] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "repo" not in item or "issue" not in item:
            ui.err_console().print(
                f"[vex.error]error: subset entry {i} needs 'repo' and 'issue'[/]"
            )
            return None
        # Type validation (Round 6 adversarial hardening): non-string /
        # empty repo or issue used to escape as a traceback deep in the
        # scheduler path; now a clean usage error.
        repo = item["repo"]
        issue = item["issue"]
        if not isinstance(repo, str) or not repo.strip():
            ui.err_console().print(
                f"[vex.error]error: subset entry {i}: 'repo' must be a "
                f"non-empty string[/]"
            )
            return None
        if "\x00" in repo:
            # fail fast at the boundary: a null-byte path can only crash
            # downstream (workers spawn against an unusable path)
            ui.err_console().print(
                f"[vex.error]error: subset entry {i}: 'repo' contains a null byte[/]"
            )
            return None
        if not isinstance(issue, str) or not issue.strip():
            ui.err_console().print(
                f"[vex.error]error: subset entry {i}: 'issue' must be a "
                f"non-empty string[/]"
            )
            return None
        task_id = item.get("task_id")
        if task_id is not None and (
            not isinstance(task_id, str) or not task_id.strip()
        ):
            ui.err_console().print(
                f"[vex.error]error: subset entry {i}: 'task_id' must be a "
                f"non-empty string[/]"
            )
            return None
        cfg_in = item.get("config") or {}
        if not isinstance(cfg_in, dict):
            ui.err_console().print(
                f"[vex.error]error: subset entry {i}: 'config' must be an object[/]"
            )
            return None
        cfg = dict(cfg_in)
        if args.model:
            cfg["model"] = args.model
        if args.provider:
            cfg["provider"] = args.provider
        if item.get("target_test"):
            cfg["target_test"] = item["target_test"]
        if item.get("test_command"):
            cfg["test_command"] = item["test_command"]
        tasks.append(
            Task(
                task_id=item.get("task_id") or f"bench-{i}-{uuid.uuid4().hex[:6]}",
                repo_path=str(Path(repo).expanduser()),
                issue_text=issue,
                config=cfg,
            )
        )
    return tasks


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Print a human-readable summary of logs/{task_id}/state.json."""
    con = ui.console()
    err = ui.err_console()
    task_id = args.task_id
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    task_dir = _safe_task_dir(task_id, log_root)
    if task_dir is None:
        err.print(
            f"[vex.error]error: invalid task id: {task_id!r} "
            f"(expected a single path segment)[/]"
        )
        return 2
    state_file = task_dir / "state.json"

    if not state_file.is_file():
        # Helpful hint: what actually exists under the log root?
        hint = ""
        if log_root.is_dir():
            ids = sorted(p.name for p in log_root.iterdir() if p.is_dir())
            if ids:
                shown = ", ".join(ids[:8])
                hint = f"\n[vex.muted]available task dirs under {log_root}: {shown}[/]"
        err.print(f"[vex.error]no state file at {state_file}[/][vex.muted]{hint}[/]")
        return 2

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        err.print(f"[vex.error]error: cannot read {state_file}: {exc}[/]")
        return 2

    plan = state.get("plan") or []
    completed = state.get("completed_steps") or []
    remaining = state.get("remaining_plan") or []
    files_touched = state.get("files_touched") or []
    decisions = state.get("decisions") or []

    con.rule(f"[vex.accent]task {state.get('task_id', task_id)}[/]")
    con.print(f"[vex.muted]state file: {state_file}[/]")
    done = "all" if plan and not remaining else f"{len(completed)}/{len(plan)}"
    con.print(f"[vex.muted]progress:   {done} step(s) complete[/]")
    if plan:
        con.print(f"[vex.muted]plan ({len(plan)}):[/]")
        for step in plan:
            mark = "[vex.ok]x[/]" if step in completed else " "
            con.print(f"  [{mark}] {step}")
    if files_touched:
        con.print(f"[vex.muted]files touched ({len(files_touched)}):[/]")
        for f in files_touched:
            con.print(f"  [vex.muted]- {f}[/]")
    if decisions:
        con.print(f"[vex.muted]decisions ({len(decisions)}):[/]")
        for d in decisions:
            con.print(f"  [vex.muted]- {d}[/]")
    if remaining:
        con.print(f"[vex.muted]remaining ({len(remaining)}):[/]")
        for r in remaining:
            con.print(f"  [vex.muted]- {r}[/]")

    # Enrichment: result status + cost from the trace if present
    trace_file = state_file.parent / "trace.jsonl"
    if trace_file.is_file():
        result = _tail_result_from_trace(trace_file)
        if result:
            data = result.get("data") or {}
            status = data.get("status", "?")
            style = "vex.ok" if status == "success" else "vex.error"
            con.print(f"[vex.muted]result:     [{style}]{status}[/]")
            if "cost_usd" in data:
                con.print(
                    f"[vex.muted]cost:       {ui.fmt_cost(data.get('cost_usd', 0))}[/]"
                )
            if data.get("note"):
                con.print(f"[vex.muted]note:       {data['note']}[/]")
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
    con = ui.console()
    store = _store()
    rid = store.record(args.text, category=args.category)
    if rid is None:
        ui.err_console().print("[vex.error]error: empty decision text[/]")
        return 2
    con.print(f"[vex.ok]recorded decision #{rid}[/]: {args.text}")
    return 0


def cmd_memory_decisions(args: argparse.Namespace) -> int:
    """Query the decision store (ranked keyword match; empty = recent)."""
    store = _store()
    results = store.search(args.query or "", limit=args.limit)
    from memory.decision_store import format_decisions

    ui.console().print(format_decisions(results, args.query or ""))
    return 0


def cmd_memory_structure(args: argparse.Namespace) -> int:
    """Query the code graph of a repo."""
    if not args.repo:
        ui.err_console().print(
            "[vex.error]error: --repo is required for query-structure[/]"
        )
        return 2
    from memory.code_graph import CodeGraph

    try:
        graph = CodeGraph(args.repo)
    except NotADirectoryError as exc:
        ui.err_console().print(f"[vex.error]error: {exc}[/]")
        return 2
    except (OSError, ValueError) as exc:
        # null-byte paths / permission errors surface as clean usage errors
        ui.err_console().print(
            f"[vex.error]error: cannot index repo {args.repo!r}: {exc}[/]"
        )
        return 2
    ui.console().print(graph.query(args.query or "help"))
    return 0


def cmd_memory_ingest(args: argparse.Namespace) -> int:
    """One-shot ingestion of all state files under a logs dir."""
    store = _store()
    new = store.poll(args.logs_dir or str(default_logs_dir()))
    ui.console().print(
        f"[vex.ok]ingested {new} new decision(s)[/] "
        f"[vex.muted]from {args.logs_dir or default_logs_dir()}[/]"
    )
    return 0


# ---------------------------------------------------------------------------
# mcp client (consume EXTERNAL MCP servers — spec item 30)
# ---------------------------------------------------------------------------


def cmd_mcp_list_tools(args: argparse.Namespace) -> int:
    """List the tools an external MCP server exposes (stdio spawn)."""
    from memory.mcp_client import list_mcp_tools

    out = list_mcp_tools(args.server, cwd=args.cwd)
    if not out.get("ok"):
        ui.err_console().print(f"[vex.error]error: {out.get('error')}[/]")
        return 1
    tools = out.get("tools", [])
    con = ui.console()
    con.print(f"[vex.accent]{len(tools)} tool(s)[/] [vex.muted]on {args.server!r}:[/]")
    for t in tools:
        desc = f" — {t['description']}" if t["description"] else ""
        con.print(f"  [vex.accent]{t['name']}[/][vex.muted]{desc}[/]")
    return 0


def cmd_mcp_call(args: argparse.Namespace) -> int:
    """Call one tool on an external MCP server and print the text result."""
    from memory.mcp_client import call_mcp_tool

    tool_args: Dict[str, Any] = {}
    if args.args_json:
        try:
            tool_args = json.loads(args.args_json)
        except ValueError as exc:
            ui.err_console().print(
                f"[vex.error]error: --args is not valid JSON: {exc}[/]"
            )
            return 2
        if not isinstance(tool_args, dict):
            ui.err_console().print("[vex.error]error: --args must be a JSON object[/]")
            return 2
    out = call_mcp_tool(args.server, args.tool, tool_args, cwd=args.cwd)
    if not out.get("ok"):
        ui.err_console().print(f"[vex.error]error: {out.get('error')}[/]")
        return 1
    ui.console().print(out.get("text", ""))
    return 0


# ---------------------------------------------------------------------------
# dashboard (read-only web view of existing logs)
# ---------------------------------------------------------------------------


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the read-only dashboard over existing logs (blocks)."""
    try:
        from dashboard.server import serve
    except ImportError as exc:
        ui.err_console().print(
            f"[vex.error]error: dashboard module unavailable: {exc}[/]"
        )
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
    parser.add_argument(
        "--no-color", action="store_true", help=argparse.SUPPRESS
    )  # accepted anywhere; top-level sets it
    parser.add_argument("--model", help="model name (default: harness config)")
    parser.add_argument("--provider", help="litellm provider (default: harness config)")
    parser.add_argument("--api-key", help="API key (or env; prefer env)")
    parser.add_argument(
        "--api-base", help="custom endpoint base URL (BYO router/gateway)"
    )
    parser.add_argument(
        "--adaptive-routing",
        action="store_true",
        help="enable adaptive model routing (runtime router)",
    )
    parser.add_argument("--target-test", help="pytest node id of the target test")
    parser.add_argument(
        "--test-command", help="test command override (default: autodetect)"
    )
    parser.add_argument("--max-retries", type=int, help="max full attempts per task")
    parser.add_argument("--budget", type=float, help="per-task cost cap in USD")
    parser.add_argument(
        "--approval",
        action="store_true",
        help="require human approval of the diff before it "
        "is applied (interactive prompt in this CLI)",
    )
    parser.add_argument(
        "--protected", action="append", help="protected path glob (repeatable)"
    )
    parser.add_argument(
        "--log-root", default=None, help="task log root (default: ./logs)"
    )


def _get_version() -> str:
    """The installed distribution version (with a static fallback).

    Assumes the package is installed (normal case: importlib.metadata
    answers); if the metadata lookup fails (e.g. running from a source
    tree that was never pip-installed), return the version this tree
    was released as instead of crashing.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        # The PyPI distribution is "vex-harness" (chosen after a live
        # availability check — see cli/AGENTS.md "PyPI packaging round";
        # older installs may still carry "vex-agent-cli" metadata); the
        # console script is `vex` either way.
        for dist in ("vex-harness", "vex-agent-cli"):
            try:
                return version(dist)
            except PackageNotFoundError:
                continue
        return "0.1.0+source"
    except Exception:
        return "0.1.0+source"


def build_parser() -> argparse.ArgumentParser:
    """Assemble the full CLI parser (exported for tests)."""
    parser = argparse.ArgumentParser(
        prog="vex",
        description="Vex — the AI harness that fixes bugs. Plain-language "
        "interactive mode (run `vex` with no arguments) or the "
        "scriptable subcommands below.",
    )
    parser.add_argument("--version", action="version", version="vex " + _get_version())
    parser.add_argument(
        "--no-color", action="store_true", help="force plain text (no ANSI colors)"
    )
    parser.add_argument(
        "--continue",
        dest="continue_last",
        action="store_true",
        help="resume the most recent resumable interactive "
        "session (checkpoints survive interruptions)",
    )
    parser.add_argument(
        "--resume",
        dest="resume_id",
        default=None,
        metavar="TASK_ID",
        help="resume one session/task by id",
    )
    parser.add_argument(
        "--list-sessions",
        dest="list_sessions",
        action="store_true",
        help="list recent interactive sessions (resumable marked)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fix
    p_fix = sub.add_parser("fix", help="fix one bug in one repo (single-task mode)")
    p_fix.add_argument("--repo", required=True, help="path to the repo to fix")
    p_fix.add_argument(
        "--issue", required=True, help="bug report text (or @file with the report)"
    )
    p_fix.add_argument("--task-id", default=None, help="override auto task_id")
    _add_task_config_args(p_fix)
    p_fix.set_defaults(func=cmd_fix)

    # run-benchmark
    p_bench = sub.add_parser(
        "run-benchmark", help="run a benchmark subset through the scheduler"
    )
    p_bench.add_argument(
        "--subset", required=True, help="'smoke' or a JSON file of tasks"
    )
    p_bench.add_argument("--concurrency", type=int, default=10)
    _add_task_config_args(p_bench)
    p_bench.set_defaults(func=cmd_run_benchmark)

    # status
    p_status = sub.add_parser("status", help="show a task's structured state summary")
    p_status.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    p_status.add_argument("--task-id", required=True)
    p_status.add_argument(
        "--log-root", default=None, help="task log root (default: ./logs)"
    )
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

    p_ing = mem_sub.add_parser(
        "ingest", help="ingest state.json decisions under a logs dir"
    )
    p_ing.add_argument("logs_dir", nargs="?", default=None)
    p_ing.set_defaults(func=cmd_memory_ingest)

    # dashboard (read-only web view of existing logs)
    p_dash = sub.add_parser("dashboard", help="serve the read-only run dashboard (web)")
    p_dash.add_argument("--logs-dir", default=None, help="logs root (default: ./logs)")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8765)
    p_dash.add_argument("--refresh-s", type=float, default=5.0)
    p_dash.add_argument("--no-browser", action="store_true")
    p_dash.set_defaults(func=cmd_dashboard)

    # mcp client (consume EXTERNAL MCP servers — spec item 30)
    p_mcp = sub.add_parser("mcp", help="consume an external MCP server (stdio)")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command", required=True)

    p_mcp_list = mcp_sub.add_parser(
        "list-tools", help="list tools an MCP server exposes"
    )
    p_mcp_list.add_argument(
        "server", help='server launch command, e.g. "python -m mcp_server"'
    )
    p_mcp_list.add_argument("--cwd", default=None, help="server working directory")
    p_mcp_list.set_defaults(func=cmd_mcp_list_tools)

    p_mcp_call = mcp_sub.add_parser("call", help="call one tool on an MCP server")
    p_mcp_call.add_argument("server", help="server launch command")
    p_mcp_call.add_argument("tool", help="tool name to call")
    p_mcp_call.add_argument(
        "--args",
        dest="args_json",
        default="{}",
        help="tool arguments as a JSON object string",
    )
    p_mcp_call.add_argument("--cwd", default=None, help="server working directory")
    p_mcp_call.set_defaults(func=cmd_mcp_call)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (console script ``vex`` / ``python -m cli``).

    Assumes argv is None (real invocation: sys.argv) or a list for tests.
    No arguments + interactive terminal -> the natural-language session
    (the primary UX); otherwise subcommand dispatch.
    Returns a process exit code: 0 success, 1 task failure, 2 usage error.
    """
    raw = sys.argv[1:] if argv is None else list(argv)
    if "--no-color" in raw:
        ui.set_no_color(True)
    if os.environ.get("NO_COLOR"):
        ui.set_no_color(True)

    # Session persistence (Task A): --continue / --resume / --list-sessions
    # work with or without a subcommand and with piped stdin (they run a
    # resumable task and print the summary; no interactive loop needed).
    session_flags = [
        a for a in raw if a in ("--continue", "--resume", "--list-sessions")
    ]

    # Interactive natural-language mode: no subcommand, stdin is a TTY.
    # Tests pass argv explicitly, so they never land here by accident.
    if argv is None and not raw and sys.stdin.isatty():
        try:
            from cli.interactive import run_interactive

            return run_interactive()
        except KeyboardInterrupt:
            ui.console().print("\n[vex.muted]interrupted[/]")
            return 130

    # Session flags are valid WITHOUT a subcommand; argparse's required
    # subparsers would reject that, so handle the bare-flag forms here.
    if session_flags and not any(
        a in raw
        for a in ("fix", "run-benchmark", "status", "memory", "dashboard", "mcp")
    ):
        try:
            from cli.interactive import cmd_continue, cmd_list_sessions, cmd_resume
        except ImportError:
            ui.err_console().print("[vex.error]interactive module unavailable[/]")
            return 2
        if "--list-sessions" in raw:
            return cmd_list_sessions()
        if "--resume" in raw:
            i = raw.index("--resume")
            if i + 1 >= len(raw):
                ui.err_console().print("[vex.error]error: --resume needs a task id[/]")
                return 2
            return cmd_resume(raw[i + 1])
        return cmd_continue()

    parser = build_parser()
    args = parser.parse_args(raw)
    if getattr(args, "no_color", False):
        ui.set_no_color(True)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        # Graceful Ctrl+C (Task E): the scheduler kills its own workers
        # (checkpoints survive for resume); sweep any sandbox containers
        # whose owner (this process) is now gone so nothing is orphaned.
        _cleanup_after_interrupt()
        ui.err_console().print(
            "\n[vex.warn]interrupted — no orphaned "
            "containers; checkpoints kept for resume[/]"
        )
        return 130
    except SystemExit:
        raise  # argparse usage errors carry their own output/exit code
    except BaseException as exc:
        # Task D safety net: whatever escapes a subcommand, the user gets a
        # plain-language explanation — never a raw traceback. Exit code 1
        # (task-level failure), not 2: the usage was valid.
        from cli.errors import explain_exception

        try:
            _cleanup_after_interrupt()
        except Exception:
            pass
        explain_exception(exc)
        return 1


def _cleanup_after_interrupt() -> None:
    """Best-effort sweep of orphaned sandbox containers after Ctrl+C.

    execute_sandboxed's own opportunistic reaper covers peers of dead
    workers; this covers THIS process's containers when the user
    interrupts the CLI itself (its hexec-p<pid>-* names embed our PID,
    which dies with us — the public reap_orphaned_containers() kills
    exactly those). Never raises: cleanup is best-effort by design.
    """
    try:
        from execution.sandbox import docker_available, reap_orphaned_containers

        if docker_available():
            reap_orphaned_containers()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
