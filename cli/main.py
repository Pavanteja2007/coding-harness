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
    vex analyze-history — offline cross-task analysis + difficulty
        predictor recalibration report over accumulated logs
    vex update — self-update (or --check: report the latest release)
    vex completion <shell> — print/install shell completions
    vex uninstall — remove Vex completely (config + venv + PATH)

argparse (project tech lock: "a plain Python CLI"). Every command prints
something sensible even when a dependency is stubbed or misconfigured —
a CLI that crashes with a traceback fails the user worse than one that
explains what's missing. All rendering goes through cli.ui (rich + the
Vex oxblood theme; ANSI auto-degrades on dumb Windows consoles,
--no-color / NO_COLOR force plain).

Exit codes (cli.exit_codes — the stable machine contract; scripts/CI):
0 success / 1 task-level failure / 2 usage or config error /
3 environment error (Docker, dependencies) / 4 model or network error /
130 interrupted (Ctrl+C). Legacy 0/1/2 scripts keep working.

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

from cli import deps, ui
from cli import runview as _rv
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
    # Two-tier settings (global + project) fill keys the flags didn't set:
    # flags > env (VEX_MODEL/VEX_BASE_URL/VEX_API_KEY/...) > files.
    # base_url (the settings name) normalizes onto runtime's api_base here
    # so any OpenAI-compatible router works with any model name.
    from cli.vexconfig import apply_config_defaults, normalize_runtime_keys

    config = normalize_runtime_keys(apply_config_defaults(config))
    return Task(
        task_id=getattr(args, "task_id", None) or f"fix-{uuid.uuid4().hex[:8]}",
        repo_path=str(Path(args.repo).resolve()),
        issue_text=args.issue,
        config=config,
    )


def _result_json(result: "TaskResult", elapsed: float) -> Dict[str, Any]:
    """Machine-readable view of one TaskResult (vex fix --json).

    Assumes `result` is a completed TaskResult from run_task; the shape
    mirrors the human summary (status, attempts, cost, verification
    flags, diff, trace path) plus the exit-code category so scripts can
    log WHY a run failed without parsing prose.
    """
    from cli.exit_codes import reason_for

    out: Dict[str, Any] = {
        "task_id": result.task_id,
        "status": result.status,
        "attempts": result.attempts,
        "cost_usd": result.cost_usd,
        "model_calls": len(result.model_calls),
        "elapsed_s": round(elapsed, 3),
        "diff": result.diff,
        "log_path": result.log_path,
    }
    if result.verification is not None:
        v = result.verification
        out["verification"] = {
            "target_test_passed": v.target_test_passed,
            "regression_passed": v.regression_passed,
            "flaky": v.flaky,
        }
    else:
        out["verification"] = None
    out["exit_code"] = 0 if result.status == "success" else 1
    out["exit_reason"] = reason_for(out["exit_code"])
    return out


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
    """Run one bug-fix task through the real harness, single-task mode.

    --finding <scan_id>#<n>: turn a `vex scan` finding into this task
    (Task C — the issue text and target test come from the finding).
    --json: machine-readable result on stdout (no spinner/theme), for
    scripts and CI; exit codes unchanged (0/1 by outcome, 2/3/4 by
    failure category — cli.exit_codes).
    """
    from cli.interactive import LiveMonitor
    from shared.types import TaskResult  # noqa: F401  (type used in _print_result)

    as_json = bool(getattr(args, "json", False))
    con = ui.console()
    if getattr(args, "finding", None):
        # --issue is not required when the issue comes from a finding.
        if not args.repo:
            ui.err_console().print(
                "[vex.error]error: --repo is required with --finding[/]"
            )
            return 2
        return _run_finding(args, con)
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
    # Onboarding gate: no usable model/auth in the effective config ->
    # one honest line + exit 4 (model_error). Never prompts here:
    # flag paths are scriptable and must not hang on input.
    from cli.onboard import missing_credentials_exit

    gate = missing_credentials_exit(task.config, as_json)
    if gate is not None:
        if as_json:
            print(
                json.dumps(
                    {
                        "task_id": task.task_id,
                        "status": "error",
                        "exit_code": 4,
                        "exit_reason": "model_error",
                        "error": "no model configured — run `vex login` "
                        "(or set VEX_MODEL/VEX_API_KEY)",
                    },
                    indent=2,
                )
            )
        return gate
    # Plugins round (Task C): installed plugins' tool verbs extend the
    # BATCH read-only allowlist for this run (validated + deny-listed
    # in harness.tools.extend_batch_verbs — best-effort, never raises).
    try:
        from cli.plugins import apply_tool_extensions

        apply_tool_extensions()
    except Exception:
        pass
    # Unified cross-module tracing (shared.tracing): a fix run defaults
    # VEX_TRACE_DIR to its logs root so scheduler/worker/router/sandbox/
    # memory events land in <log_root>/_trace/ next to the harness trace —
    # `python -m shared.traceview <task_id>` reconstructs the whole task
    # from one place. setdefault: an operator who set it wins.
    os.environ.setdefault("VEX_TRACE_DIR", str(log_root))
    if not as_json:
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
    mon = LiveMonitor(task.task_id, log_root).start(quiet=as_json)
    # --json: the ONLY thing on stdout is the result document. Foreign
    # printers inside the run (litellm's error banner goes to STDOUT,
    # found live) are diverted to stderr for the duration of run_task.
    saved_stdout = sys.stdout if as_json else None
    if as_json:
        sys.stdout = sys.stderr
    try:
        result = run_task(task, **({"log_root": log_root} if args.log_root else {}))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        # Plain-language explanation (Task D): what broke + what to check,
        # never a raw traceback. run_task itself reports task-level
        # failures (failed/error results) — this path is for the harness
        # itself crashing. The exit code follows the failure category
        # (environment/model vs task) — cli.exit_codes.
        from cli.errors import explain_exception
        from cli.exit_codes import classify_exit_code

        explain_exception(exc)
        return classify_exit_code(exc)
    finally:
        if saved_stdout is not None:
            sys.stdout = saved_stdout
        mon.stop()
        _clear_router_context()
    elapsed = time.time() - started
    if as_json:
        payload = _result_json(result, elapsed)
        print(json.dumps(payload, indent=2))
        return 0 if result.status == "success" else 1
    _print_result(result, elapsed)
    # Task F (interaction-polish round): the terminal bell fires when a
    # --json run would stay silent (a scripted caller owns its own
    # notification story; stderr-only here, never pollutes stdout).
    ui.bell(f"vex fix {result.status}")
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

    # Onboarding gate (real tasks only — smoke/fake-harness tasks need
    # no model): every real task's merged config must carry usable
    # model/auth, else exit 4 with one honest line (never prompt here).
    from cli.onboard import (
        _has_offline_model,
        credentials_status,
        effective_credentials,
    )

    if _has_offline_model():
        pass  # scripted-model runs (tests/demos) answer without creds
    else:
        for t in tasks:
            cfg = dict(t.config or {})
            if (
                cfg.get("use_fake_harness")
                or cfg.get("use_mock_provider")
                or cfg.get("mock_script")
            ):
                continue
            ok, reason = credentials_status(effective_credentials(cfg))
            if not ok:
                ui.err_console().print(
                    f"[vex.error]error: {reason} for task {t.task_id} — run "
                    "`vex login` to configure a model (or set "
                    "VEX_MODEL/VEX_API_KEY)[/]"
                )
                return 4

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
        # Exit code follows the failure category (environment/model vs
        # task) — cli.exit_codes.
        from cli.errors import explain_exception
        from cli.exit_codes import classify_exit_code

        explain_exception(exc)
        return classify_exit_code(exc)
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
    ui.bell(f"vex benchmark {subset}: {n_ok}/{len(results)} success")
    return 0


class _BenchmarkLiveView:
    """Live in-terminal multi-task dashboard of a running benchmark
    (Task E of the vex pass; the interaction-polish round's Task D made
    it a real dashboard): every concurrent task on ONE screen with its
    status, phase, elapsed time, accrued cost, model-call count and the
    routing tier + model it is working on — the in-terminal counterpart
    to the read-only web dashboard, not a single-task view.

    Data sources (all EXISTING records, nothing new written): the real
    scheduler's `live_attempts()` for the running set + wall start; each
    task's own `trace.jsonl` and `{task_id}.runtime/model_ledger.jsonl`
    folded by `cli.runview.read_task_progress` (the same read-only view
    discipline as the completion card); scheduler results as they land.
    Degrades honestly: without a live_attempts-capable scheduler (the
    stub, or an in-process fake) the table still fills from the trace
    files; a task with no trace yet shows queued.
    """

    def __init__(self, tasks, log_root: Path, poll_s: float = 1.0) -> None:
        self.tasks = {t.task_id: t for t in tasks}
        self._log_root = Path(log_root)
        self._done: Dict[str, str] = {}
        self._started_wall: Dict[str, float] = {}
        self._scheduler = None
        self._stop = None
        self._thread = None
        self._poll_s = poll_s
        self._t0 = time.time()

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

        self._stop = threading.Event()
        # `scheduler_run` is the Scheduler.run BOUND method when the
        # real runtime is in play — __self__ is the scheduler whose
        # live_attempts() is the documented live-set surface. The stub
        # scheduler is a plain function (no __self__): the trace-file
        # path carries the view then.
        self._scheduler = getattr(scheduler_run, "__self__", None)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # -- the render (kept separate so tests can assert the exact table) --

    def _live_map(self) -> Dict[str, float]:
        """task_id -> wall epoch for currently-running attempts (empty
        when the scheduler can't tell us)."""
        out: Dict[str, float] = {}
        sched = self._scheduler
        la = getattr(sched, "live_attempts", None) if sched is not None else None
        if not callable(la):
            return out
        try:
            for tid, att in (la() or {}).items():
                start = getattr(att, "started_epoch", None)
                out[str(tid)] = float(start) if start else self._t0
        except Exception:
            return {}
        return out

    def rows(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """One dashboard row per task: {task, state, phase, elapsed,
        calls, cost, tier}. Order = submission order (stable table)."""
        from cli import runview

        now = now if now is not None else time.time()
        live = self._live_map()
        out: List[Dict[str, Any]] = []
        for tid in self.tasks:
            try:
                prog = runview.read_task_progress(self._log_root, tid)
            except Exception:
                prog = {
                    "status": "?",
                    "phase": "",
                    "model_calls": 0,
                    "cost_usd": 0.0,
                    "tier": "",
                    "models": [],
                    "started_ts": None,
                    "elapsed_s": None,
                }
            status = self._done.get(tid) or str(prog.get("status") or "queued")
            phase = str(prog.get("phase") or "")
            if tid not in self._done and tid in live and status == "queued":
                # the scheduler says this attempt is running even though
                # the worker hasn't written a trace event yet (spawn is
                # ahead of the first write) — a queued row under a live
                # attempt is a lie; correct it to running.
                status = "running"
            if tid in self._done:
                elapsed = prog.get("elapsed_s")
            elif status in ("running", "?") or tid in live:
                # running: tick against the attempt's wall start (the
                # scheduler knows it even before the trace exists), else
                # the trace's first ts, else the moment we first saw it
                start = live.get(tid) or prog.get("started_ts")
                if start is None:
                    start = self._started_wall.setdefault(tid, now)
                else:
                    self._started_wall.setdefault(tid, start)
                elapsed = max(0.0, now - start)
                if not phase or phase in ("starting", "queued"):
                    phase = phase or "starting"
            else:
                elapsed = None
            models = prog.get("models") or []
            tier = str(prog.get("tier") or "")
            if models:
                tier = f"{tier} · {', '.join(models)}" if tier else ", ".join(models)
            out.append(
                {
                    "task": tid,
                    "state": status,
                    "phase": phase,
                    "elapsed": _rv.fmt_elapsed(elapsed),
                    "calls": int(prog.get("model_calls") or 0),
                    "cost": ui.fmt_cost(float(prog.get("cost_usd") or 0.0)),
                    "tier": tier,
                }
            )
        return out

    def render(self, now: Optional[float] = None):
        """The dashboard as a rich Table (transient Live body)."""
        from rich.table import Table

        rows = self.rows(now=now)
        n_done = sum(
            1 for r in rows if r["state"] in ("success", "failed", "error", "timeout")
        )
        table = Table(
            title=f"[vex.accent]vex benchmark[/] [vex.muted]"
            f"({n_done}/{len(rows)} done · {int((now or time.time()) - self._t0)}s)[/]",
            title_justify="left",
            expand=False,
        )
        table.add_column("task", style="vex.muted", no_wrap=True)
        table.add_column("state", no_wrap=True)
        table.add_column("phase", style="vex.muted", no_wrap=True)
        table.add_column("elapsed", justify="right", no_wrap=True)
        table.add_column("calls", justify="right", no_wrap=True)
        table.add_column("cost", justify="right", no_wrap=True)
        table.add_column("tier · model", style="vex.muted", no_wrap=True)
        for r in rows:
            state = r["state"]
            if state == "success":
                mark = f"[vex.ok]{ui.GLYPHS['ok']} success"
            elif state in ("failed", "error"):
                mark = f"[vex.error]{ui.GLYPHS['fail']} {state}"
            elif state == "timeout":
                mark = f"[vex.warn]{ui.GLYPHS['wait']} timeout"
            elif state == "running":
                mark = f"[vex.running]{ui.GLYPHS['bullet']} {r['phase'] or 'running'}"
            else:
                mark = f"[vex.muted]· {state}"
            table.add_row(
                r["task"],
                mark,
                r["phase"],
                r["elapsed"],
                str(r["calls"]) or "0",
                r["cost"],
                r["tier"],
            )
        return table

    def _loop(self) -> None:
        from rich.live import Live

        con = ui.console()
        with Live(console=con, refresh_per_second=4, transient=True) as live_widget:
            while not self._stop.is_set():
                try:
                    live_widget.update(self.render())
                except Exception:
                    pass  # a broken frame must never kill the run's view
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


def _load_status_state(task_id: str, log_root: Path):
    """(state, task_dir, error_code) for a status lookup: state is the
    parsed state.json dict or None; error_code is 0 on success, else the
    contract exit code (2 usage / not found). Shared by the human and
    --json renderers so they can never disagree about the facts."""
    task_dir = _safe_task_dir(task_id, log_root)
    if task_dir is None:
        return None, None, 2
    state_file = task_dir / "state.json"
    if not state_file.is_file():
        return None, task_dir, 2
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, task_dir, 2
    return state, task_dir, 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print a human-readable summary of logs/{task_id}/state.json.

    --json: machine-readable state + trace enrichment on stdout.
    """
    con = ui.console()
    err = ui.err_console()
    as_json = bool(getattr(args, "json", False))
    task_id = args.task_id
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()

    if not as_json:
        # keep the friendlier pre-JSON messages (invalid id / missing file
        # with available-dir hint)
        task_dir = _safe_task_dir(task_id, log_root)
        if task_dir is None:
            err.print(
                f"[vex.error]error: invalid task id: {task_id!r} "
                f"(expected a single path segment)[/]"
            )
            return 2
        state_file = task_dir / "state.json"
        if not state_file.is_file():
            hint = ""
            if log_root.is_dir():
                ids = sorted(p.name for p in log_root.iterdir() if p.is_dir())
                if ids:
                    shown = ", ".join(ids[:8])
                    hint = (
                        f"\n[vex.muted]available task dirs under {log_root}: {shown}[/]"
                    )
            err.print(
                f"[vex.error]no state file at {state_file}[/][vex.muted]{hint}[/]"
            )
            return 2
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            err.print(f"[vex.error]error: cannot read {state_file}: {exc}[/]")
            return 2
    else:
        state, task_dir, code = _load_status_state(task_id, log_root)
        if code != 0 or state is None:
            err.print(
                f"[vex.error]error: no readable state for task {task_id!r} "
                f"under {log_root}[/]"
            )
            return code or 2

    plan = state.get("plan") or []
    completed = state.get("completed_steps") or []
    remaining = state.get("remaining_plan") or []
    files_touched = state.get("files_touched") or []
    decisions = state.get("decisions") or []

    # Enrichment: result status + cost from the trace if present
    trace_file = (task_dir or log_root / task_id) / "trace.jsonl"
    result_event = None
    if trace_file.is_file():
        result_event = _tail_result_from_trace(trace_file)
    data = (result_event or {}).get("data") or {}

    if as_json:
        payload = {
            "task_id": state.get("task_id", task_id),
            "state_file": str((task_dir or log_root / task_id) / "state.json"),
            "progress": {
                "plan_steps": len(plan),
                "completed_steps": list(completed),
                "remaining_steps": list(remaining),
                "complete": bool(plan and not remaining),
            },
            "files_touched": list(files_touched),
            "decisions": list(decisions),
            "result": {
                "status": data.get("status"),
                "cost_usd": data.get("cost_usd"),
                "note": data.get("note"),
            }
            if result_event
            else None,
        }
        print(json.dumps(payload, indent=2))
        return 0

    con.rule(f"[vex.accent]task {state.get('task_id', task_id)}[/]")
    con.print(
        f"[vex.muted]state file: {(task_dir or log_root / task_id) / 'state.json'}[/]"
    )
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

    if result_event:
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
# config (two-tier settings: global + project — vex config ...)
# ---------------------------------------------------------------------------


def _config_source_of(key: str, effective: Dict[str, Any]) -> str:
    """Which tier last set an effective key (for `vex config list`)."""
    from cli import vexconfig

    for var, k in vexconfig._ENV_KEYS.items():
        if k == key and os.environ.get(var):
            return f"env:{var}"
    pd_ = vexconfig.project_settings_dir()
    if pd_ is not None:
        for label, fname in (
            ("project-local", "settings.local.toml"),
            ("project", "settings.toml"),
        ):
            data = vexconfig.load_vex_config(pd_ / fname)
            if key in data:
                return label
    if vexconfig.global_settings_path().is_file() and key in vexconfig.load_vex_config(
        vexconfig.global_settings_path()
    ):
        return "global"
    if vexconfig.legacy_settings_path().is_file() and key in vexconfig.load_vex_config(
        vexconfig.legacy_settings_path()
    ):
        return "legacy (~/.vex/config.toml)"
    return "default" if key not in effective else "unknown"


_SECRET_KEYS = {"api_key", "api_base"}
_REDACTED = {"api_key"}


def _display_value(key: str, value: Any) -> str:
    """Render a config value for display: secrets masked, plain rest."""
    from cli import vexconfig

    if key in _REDACTED:
        return vexconfig.mask_secret(value)
    return repr(value)


def cmd_config(args: argparse.Namespace) -> int:
    """`vex config <subcommand>` — view and edit the two-tier settings."""
    from cli import vexconfig

    con = ui.console()
    sub = args.config_command

    if sub == "path":
        gp = vexconfig.global_settings_path()
        con.print(f"[vex.accent]global[/][vex.muted]  {gp}[/]")
        pd_ = vexconfig.project_settings_dir()
        if pd_ is not None:
            con.print(f"[vex.accent]project[/][vex.muted] {pd_ / 'settings.toml'}[/]")
            con.print(
                f"[vex.accent]local[/][vex.muted]    {pd_ / 'settings.local.toml'}[/]"
            )
        else:
            con.print(
                "[vex.muted]project (none found — `vex config "
                "init-project` creates .vex/settings.toml)[/]"
            )
        if vexconfig.legacy_settings_path().is_file() and not gp.is_file():
            con.print(
                f"[vex.muted]legacy[/]    {vexconfig.legacy_settings_path()} "
                "(read as global fallback until the new file exists)[/]"
            )
        return 0

    if sub == "list":
        eff = vexconfig.effective_settings()
        if not eff:
            con.print("[vex.muted]no settings set (all defaults in effect)[/]")
        else:
            con.rule("[vex.accent]effective settings[/]")
            for key in sorted(eff):
                origin = _config_source_of(key, eff)
                con.print(
                    f"  [vex.accent]{key}[/] = {_display_value(key, eff[key])} "
                    f"[vex.muted]({origin})[/]"
                )
        con.print(
            "\n[vex.muted]tier files that exist on this chain (low -> high "
            "merge order):[/]"
        )
        chain = vexconfig.settings_chain()
        if chain:
            for label, p, _data in chain:
                con.print(f"  [vex.muted]{label:13} {p}[/]")
        else:
            con.print("  [vex.muted](none)[/]")
        pd_ = vexconfig.project_settings_dir()
        if (
            pd_ is not None
            and (pd_ / "settings.local.toml").is_file()
            and not vexconfig.local_is_ignored(pd_.parent)
        ):
            con.print(
                f"[vex.warn]warning: {pd_ / 'settings.local.toml'} is NOT "
                "in .gitignore — run `vex config init-project` to fix[/]"
            )
        return 0

    if sub == "get":
        eff = vexconfig.effective_settings()
        if args.key not in eff:
            con.print(f"[vex.muted]{args.key}: not set (default in effect)[/]")
            return 1
        con.print(
            f"{args.key} = {_display_value(args.key, eff[args.key])} "
            f"[vex.muted]({_config_source_of(args.key, eff)})[/]"
        )
        return 0

    if sub == "set":
        try:
            value = vexconfig.coerce_value(args.key, args.value)
        except ValueError as exc:
            ui.err_console().print(f"[vex.error]error: {exc}[/]")
            return 2
        try:
            path, created = vexconfig.set_tier_key(args.tier, args.key, value)
        except (ValueError, OSError) as exc:
            ui.err_console().print(f"[vex.error]error: {exc}[/]")
            return 2
        rel = f" ({args.tier} tier)" if args.tier != "global" else ""
        con.print(
            f"[vex.ok]set {args.key} = {_display_value(args.key, value)}[/] "
            f"[vex.muted]{rel} -> {path}[/]"
        )
        if args.tier in ("local", "project"):
            ensure = vexconfig.ensure_gitignore(tier_project_root(args.tier))
            if ensure in ("appended", "created"):
                con.print(
                    "[vex.ok]gitignore covers .vex/settings.local.toml[/] "
                    "[vex.muted](kept out of commits automatically)[/]"
                )
        return 0

    if sub == "unset":
        try:
            outcome, path = vexconfig.unset_tier_key(args.tier, args.key)
        except (ValueError, OSError) as exc:
            ui.err_console().print(f"[vex.error]error: {exc}[/]")
            return 2
        verb = "unset" if outcome == "removed" else "was not set"
        con.print(f"[vex.ok]{args.key} {verb}[/] [vex.muted]({path})[/]")
        return 0 if outcome == "removed" else 1

    if sub == "init-project":
        root = Path(args.project_root or Path.cwd()).resolve()
        if not root.is_dir():
            ui.err_console().print(f"[vex.error]error: not a directory: {root}[/]")
            return 2
        try:
            info = vexconfig.ensure_project_layout(root)
        except OSError as exc:
            ui.err_console().print(f"[vex.error]error: {exc}[/]")
            return 2
        created, path = info["settings_created"], info["path"]
        if created:
            con.print(f"[vex.ok]created[/] [vex.muted]{path}[/]")
        else:
            con.print(f"[vex.muted]already exists: {path}[/]")
        extras = [c for c in info["created"] if c != "settings.toml"]
        if extras:
            con.print(
                "[vex.muted]scaffolded "
                + ", ".join(f".vex/{c}" for c in extras)
                + "[/]"
            )
        ensure = vexconfig.ensure_gitignore(root)
        if ensure == "created":
            con.print(
                "[vex.ok]created .gitignore[/] [vex.muted](ignores "
                "settings.local.toml)[/]"
            )
        elif ensure == "appended":
            con.print("[vex.ok]added .vex/settings.local.toml to .gitignore[/]")
        elif ensure == "present":
            con.print("[vex.muted].gitignore already covers it[/]")
        elif ensure is None and (root / ".git").exists():
            con.print(
                "[vex.warn]could not update .gitignore — add "
                ".vex/settings.local.toml by hand[/]"
            )
        return 0

    ui.err_console().print(f"[vex.error]error: unknown config command {sub!r}[/]")
    return 2


def tier_project_root(tier: str) -> Path:
    """The repo root a project/local-tier write targets: the .vex dir's
    parent when found, else the CWD (the set's write view)."""
    from cli import vexconfig

    pd_ = vexconfig.project_settings_dir()
    return pd_.parent if pd_ is not None else Path.cwd()


def _cmd_login(args: argparse.Namespace) -> int:
    """`vex login` — the onboarding wizard on demand."""
    from cli.onboard import cmd_login

    return cmd_login(args)


def _cmd_logout(args: argparse.Namespace) -> int:
    """`vex logout` — strip the stored api_key."""
    from cli.onboard import cmd_logout

    return cmd_logout(args)


# ---------------------------------------------------------------------------
# mcp client (consume EXTERNAL MCP servers — spec item 30)
# ---------------------------------------------------------------------------


def cmd_mcp_list_tools(args: argparse.Namespace) -> int:
    """List the tools an external MCP server exposes (stdio spawn).

    The `server` argument is a known label (`vex mcp add`) or a raw
    launch command — labels resolve through the unified connectors
    discovery first, so `vex mcp list-tools linter` works after
    `vex mcp add linter -- ...`.
    """
    from memory.mcp_client import list_mcp_tools

    try:
        from cli import connectors as connectors_mod

        server = connectors_mod.resolve_server(args.server) or args.server
    except Exception:
        server = args.server
    out = list_mcp_tools(server, cwd=args.cwd)
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
    """Call one tool on an external MCP server and print the text result.

    Like list-tools, `server` may be a configured label or a raw
    launch command (labels resolve first).
    """
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
    try:
        from cli import connectors as connectors_mod

        server = connectors_mod.resolve_server(args.server) or args.server
    except Exception:
        server = args.server
    out = call_mcp_tool(server, args.tool, tool_args, cwd=args.cwd)
    if not out.get("ok"):
        ui.err_console().print(f"[vex.error]error: {out.get('error')}[/]")
        return 1
    ui.console().print(out.get("text", ""))
    return 0


# ---------------------------------------------------------------------------
# mcp registry (vex mcp add/remove/list/health — the unified connectors)
# ---------------------------------------------------------------------------


def cmd_mcp(args: argparse.Namespace) -> int:
    """Dispatch vex mcp add/remove/list/health (the connectors registry).

    Exposed as one callable for NIGHT-B's /mcp surface (which consumes
    these helpers without touching the slash table): add/remove/list
    go through cli.connectors; health spawns each server. All
    ConnectorErrors render cleanly + exit 2; health never tracebacks
    (per-server ok/fail lines + exit 1 when any fail).
    """
    from cli import connectors as connectors_mod

    try:
        if args.mcp_command == "add":
            cmd_parts = list(getattr(args, "command", []) or [])
            if cmd_parts and cmd_parts[0] == "--":
                cmd_parts = cmd_parts[1:]
            if not cmd_parts:
                ui.err_console().print(
                    "[vex.error]error: `vex mcp add <label> -- <cmd...>` "
                    "needs a command after `--`[/]"
                )
                return 2
            import shlex

            command = " ".join(shlex.quote(p) for p in cmd_parts)
            label = connectors_mod.add_server(args.label, command)
            ui.console().print(
                f"[vex.ok]added MCP server {label}[/] "
                f"[vex.muted](global settings; "
                f"{connectors_mod.mask_command(command)})[/]"
            )
            return 0
        if args.mcp_command == "remove":
            label = connectors_mod.remove_server(args.label)
            ui.console().print(f"[vex.ok]removed MCP server {label}[/]")
            return 0
        if args.mcp_command == "list":
            servers = connectors_mod.list_servers()
            if not servers:
                ui.console().print(
                    "[vex.muted]no MCP servers configured "
                    "(vex mcp add <label> -- <cmd...>)[/]"
                )
                return 0
            ui.console().print(
                f"[vex.accent]{len(servers)} MCP server(s)[/] "
                "[vex.muted](global over project over local; labels resolve in "
                "list-tools/call)[/]"
            )
            for s in servers:
                ui.console().print(
                    f"  [vex.accent]{s['label']}[/] "
                    f"[vex.muted]({s['source']}) {s['command']}[/]"
                )
            return 0
        if args.mcp_command == "health":
            results = connectors_mod.check_health()
            if not results:
                ui.console().print(
                    "[vex.muted]no MCP servers configured "
                    "(vex mcp add <label> -- <cmd...>; plugins may also "
                    "provide servers)[/]"
                )
                return 0
            failed = 0
            for r in results:
                if r.get("ok"):
                    n = len(r.get("tools") or [])
                    ui.console().print(
                        f"  [vex.ok]ok[/] [vex.accent]{r['label']}[/] "
                        f"[vex.muted]({r['source']}) {n} tool(s)[/]"
                    )
                else:
                    failed += 1
                    ui.console().print(
                        f"  [vex.error]fail[/] [vex.accent]{r['label']}[/] "
                        f"[vex.muted]({r['source']}) {r.get('error')}[/]"
                    )
            return 1 if failed else 0
        ui.err_console().print("[vex.error]unknown mcp command[/]")
        return 2
    except connectors_mod.ConnectorError as exc:
        ui.err_console().print(f"[vex.error]error: {exc}[/]")
        return 2


# ---------------------------------------------------------------------------
# skills (vex skills list/show — read-only view over the skill scan)
# ---------------------------------------------------------------------------


def list_skills_for_cli(
    repo_path: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Skills for CLI/NIGHT-B display as [{name, origin, description}].

    Assumes repo_path is the session's repo (None = CWD). Sorted by
    name (the discovery's stable order). Never raises — a broken scan
    degrades to [].
    """
    try:
        from harness import skills as skills_mod

        found = skills_mod.discover_skills(repo_path=repo_path)
        return [
            {"name": s.name, "origin": s.origin, "description": s.description}
            for s in found
        ]
    except Exception:
        return []


def show_skill_for_cli(
    name: str, repo_path: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """One skill's full body for CLI/NIGHT-B display (None when unknown).

    Assumes name is a skill name (exact match). Never raises.
    """
    try:
        from harness import skills as skills_mod

        for s in skills_mod.discover_skills(repo_path=repo_path):
            if s.name == name:
                return {
                    "name": s.name,
                    "origin": s.origin,
                    "description": s.description,
                    "body": s.body,
                    "source": s.source,
                }
        return None
    except Exception:
        return None


def cmd_skills(args: argparse.Namespace) -> int:
    """Dispatch vex skills list/show.

    `list` prints name + origin + description head (one line each);
    `show <name>` prints the full skill body on demand. Exposed for
    NIGHT-B's /skills surface (which consumes list_skills_for_cli /
    show_skill_for_cli without touching the slash table).
    """
    repo = getattr(args, "repo", None) or str(Path.cwd())
    if args.skills_command == "list":
        skills = list_skills_for_cli(repo)
        if not skills:
            ui.console().print(
                "[vex.muted]no skills installed "
                "(.vex/skills/, ~/.config/vex/skills/, or a plugin)[/]"
            )
            return 0
        for s in skills:
            head = (s["description"] or "").strip().splitlines()
            desc = head[0][:100] if head else "(no description)"
            ui.console().print(
                f"  [vex.accent]{s['name']}[/] [vex.muted]({s['origin']}) {desc}[/]"
            )
        return 0
    if args.skills_command == "show":
        found = show_skill_for_cli(args.name, repo)
        if found is None:
            ui.err_console().print(f"[vex.error]error: no skill named {args.name!r}[/]")
            return 2
        ui.console().print(
            f"[vex.accent]{found['name']}[/] [vex.muted]({found['origin']})[/]"
        )
        if found.get("description"):
            ui.console().print(f"[vex.muted]{found['description']}[/]")
        ui.console().print(found["body"])
        return 0
    ui.err_console().print("[vex.error]unknown skills command[/]")
    return 2


# ---------------------------------------------------------------------------
# plugin subcommands (Plugins round, Task C)
# ---------------------------------------------------------------------------


def cmd_plugin(args: argparse.Namespace) -> int:
    """Dispatch vex plugin install/list/remove/enable/disable."""
    from cli import plugins as plugins_mod

    try:
        if args.plugin_command == "install":
            name = plugins_mod.install(args.source, name_override=args.name)
            con = ui.console()
            con.print(f"[vex.ok]installed plugin {name}[/]")
            entry = next(
                (e for e in plugins_mod.list_plugins() if e.get("name") == name),
                {},
            )
            con.print(f"[vex.muted]dir:      {entry.get('dir', '?')}[/]")
            skills = entry.get("skills_on_disk") or []
            commands = entry.get("commands_on_disk") or []
            tools = (entry.get("tools") or {}).get("verbs") or []
            mcp = entry.get("mcp_servers") or {}
            con.print(
                f"[vex.muted]skills:   {len(skills)}"
                f"{' — ' + ', '.join(skills) if skills else ''}[/]"
            )
            con.print(
                f"[vex.muted]commands: {len(commands)}"
                f"{' — ' + ', '.join('/' + c for c in commands) if commands else ''}[/]"
            )
            if tools:
                con.print(f"[vex.muted]tools:    BATCH verbs: {', '.join(tools)}[/]")
            if mcp:
                con.print(
                    "[vex.muted]mcp:      "
                    + ", ".join(f"{k} ({v})" for k, v in mcp.items())
                    + "[/]"
                )
            return 0
        if args.plugin_command == "list":
            con = ui.console()
            plugins = plugins_mod.list_plugins()
            if not plugins:
                con.print(
                    f"[vex.muted]no plugins installed under "
                    f"{plugins_mod.plugins_root().resolve()}[/]"
                )
                return 0
            con.print(
                f"[vex.accent]{len(plugins)} plugin(s) installed[/] "
                f"[vex.muted](source: install from a local path or git URL)[/]"
            )
            for p in plugins:
                if p.get("error"):
                    tag = " (disabled)" if p.get("enabled") is False else ""
                    con.print(
                        f"  [vex.error]{p['name']}[/] [vex.muted]— broken: "
                        f"{p['error']}{tag}[/]"
                    )
                    continue
                desc = f" — {p['description']}" if p.get("description") else ""
                # "(disabled)" in parens: a bracketed [disabled] would parse
                # as a rich style tag and vanish from the rendered line.
                state = "" if p.get("enabled", True) else " (disabled)"
                con.print(f"  [vex.accent]{p['name']}[/][vex.muted]{desc}{state}[/]")
                con.print(
                    f"[vex.muted]    {len(p.get('skills_on_disk') or [])} "
                    f"skill(s), {len(p.get('commands_on_disk') or [])} "
                    f"command(s)"
                    + (
                        f", tools: {', '.join((p.get('tools') or {}).get('verbs') or [])}"
                        if (p.get("tools") or {}).get("verbs")
                        else ""
                    )
                    + (
                        f", mcp: {', '.join((p.get('mcp_servers') or {}).keys())}"
                        if p.get("mcp_servers")
                        else ""
                    )
                    + "[/]"
                )
            return 0
        if args.plugin_command == "remove":
            name = plugins_mod.remove(args.name)
            ui.console().print(f"[vex.ok]removed plugin {name}[/]")
            return 0
        if args.plugin_command == "enable":
            name = plugins_mod.enable(args.name)
            ui.console().print(f"[vex.ok]enabled plugin {name}[/]")
            return 0
        if args.plugin_command == "disable":
            name = plugins_mod.disable(args.name)
            ui.console().print(
                f"[vex.ok]disabled plugin {name}[/] "
                "[vex.muted](stays installed; skills/commands/tools/mcp "
                "skipped until re-enabled)[/]"
            )
            return 0
        ui.err_console().print(f"[vex.error]unknown plugin command[/]")
        return 2
    except plugins_mod.PluginError as exc:
        ui.err_console().print(f"[vex.error]error: {exc}[/]")
        return 2


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
# scan (Proactive Codebase Health Scan round)
# ---------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    """Read-only codebase health scan; no bug report needed.

    Analyzes the repo (coverage gaps via the code graph, latent-bug
    smells via AST, dependency pins) and prints the ranked findings —
    only what's genuinely worth attention, with a rationale each. The
    scan never edits anything: findings, report, and the Task-C handoff
    (`vex fix --finding <scan_id>#<n>`) are the deliverables.
    """
    from harness.scan_mode import run_scan, render_report as _render

    con = ui.console()
    as_json = bool(getattr(args, "json", False))
    if getattr(args, "fix", None) is not None and as_json:
        ui.err_console().print(
            "[vex.error]error: --fix and --json are mutually exclusive[/]"
        )
        return 2
    repo = Path(args.repo)
    if not repo.is_dir():
        ui.err_console().print(
            f"[vex.error]error: --repo is not a directory: {args.repo}[/]"
        )
        return 2
    remote = bool(getattr(args, "remote", False))
    focus = getattr(args, "focus", None) or None
    if focus not in (None, "coverage", "smells", "dependencies"):
        ui.err_console().print(
            f"[vex.error]error: --focus must be coverage|smells|dependencies[/]"
        )
        return 2
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    os.environ.setdefault("VEX_TRACE_DIR", str(log_root))
    as_json = bool(getattr(args, "json", False))
    if not as_json:
        con.print(
            f"[vex.accent]vex scan[/] [vex.muted]"
            f"{ui.GLYPHS['arrow']} {repo.resolve()}"
            f"{' (remote deps check)' if remote else ''}[/]"
        )
    started = time.time()
    scan = run_scan(
        str(repo),
        config={},
        log_root=log_root,
        remote=remote,
        focus=focus,
        max_findings=args.max_findings,
    )
    if scan["status"] == "error":
        ui.err_console().print(
            f"[vex.error]error: scan failed: {scan['notes'][0] if scan['notes'] else 'unknown'}[/]"
        )
        return 1
    elapsed = time.time() - started
    if as_json:
        # --json: one JSON document on stdout, nothing else (the same
        # contract as `vex fix --json`).
        payload = dict(scan)
        print(json.dumps(payload, indent=2))
        return 0
    shown = scan["findings"][: scan["shown"]]

    # `--fix N`: close the loop immediately on the Nth finding (Task C)
    fix_n = getattr(args, "fix", None)
    if fix_n is not None:
        ns = argparse.Namespace(**args.__dict__)
        ns.finding = f"{scan['scan_id']}#{fix_n}"
        ns.repo = str(repo.resolve())
        ns.json = False
        return _run_finding(ns, con)

    sev_style = {"high": "vex.error", "medium": "vex.warn", "low": "vex.muted"}
    for f in shown:
        loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
        con.print()
        con.print(
            f"[vex.accent2]{f['index']}.[/] "
            f"[{sev_style[f['severity']]}]{f['severity']}[/] "
            f"[vex.muted]{f['kind']}[/] {f['title']}"
        )
        con.print(f"[vex.muted]  location: {loc}[/]")
        if f.get("evidence"):
            con.print(f"[vex.muted]  evidence: {f['evidence']}[/]")
        for line in f["rationale"].split("\n"):
            if line.strip():
                con.print(f"  {line}")
        con.print(
            f"[vex.muted]  fix as a task:[/] "
            f"vex fix --finding {scan['scan_id']}#{f['index']}"
        )
    if not shown:
        con.print("[vex.ok]no findings worth attention[/]")
    if scan["suppressed"]:
        con.print(
            f"[vex.muted]({scan['suppressed']} lower-value finding(s) suppressed — "
            f"--max-findings to resurface; full list in {scan['scan_path']})[/]"
        )
    for n in scan["notes"]:
        con.print(f"[vex.muted]note: {n}[/]")
    con.print()
    counts = scan["counts"].get("by_kind") or {}
    con.print(
        f"[vex.muted]scan {scan['scan_id']} · {len(scan['findings'])} finding(s)"
        f" · {scan['shown']} shown · {scan['suppressed']} suppressed"
        f"{' · ' + ' / '.join(f'{k} {v}' for k, v in sorted(counts.items())) if counts else ''}"
        f" · {elapsed:.1f}s[/]"
    )
    if scan["report_path"]:
        con.print(f"[vex.muted]report: {scan['report_path']}[/]")
    return 0


def _run_finding(args: argparse.Namespace, con) -> int:
    """Shared driver for `vex fix --finding <id>#<n>` (Task C).

    Resolves the finding from its scan.json, then dispatches through
    the existing verifier-gated entries: fix_kind "fix" -> the plain
    fix loop (cmd_fix with the finding's issue/target); "build" ->
    build mode (a version-floor acceptance test genuinely fails on the
    old state). Returns a process exit code.
    """
    from harness.scan_mode import finding_task_params, resolve_finding

    log_root = (
        Path(args.log_root) if getattr(args, "log_root", None) else default_logs_dir()
    )
    scan, finding, err = resolve_finding(args.finding, log_root)
    if finding is None:
        ui.err_console().print(f"[vex.error]error: {err}[/]")
        return 2
    params = finding_task_params(finding)
    con.print()
    con.print(
        f"[vex.accent]vex fix --finding[/] [vex.muted]"
        f"{args.finding} {ui.GLYPHS['arrow']} {finding['title']}[/]"
    )
    if params["note"]:
        con.print(f"[vex.muted]{params['note']}[/]")
    if params["fix_kind"] == "build":
        return _run_finding_build(args, scan, finding, params, con)
    # Hand off to the plain fix loop with the finding's issue/target.
    # finding is cleared first — cmd_fix would re-dispatch to _run_finding.
    args.finding = None
    args.issue = params["issue_text"]
    if params["target_test"]:
        args.target_test = params["target_test"]
    args.repo = scan.get("repo_path") or args.repo
    return cmd_fix(args)


def _run_finding_build(
    args: argparse.Namespace,
    scan: Dict[str, Any],
    finding: Dict[str, Any],
    params: Dict[str, Any],
    con,
) -> int:
    """Build-mode leg of --finding (dependency bumps).

    Build mode authors its own acceptance tests; we pass the finding's
    issue text (which already demands a version-floor test) through the
    real run_build entry. Returns a process exit code.
    """
    from harness.build_mode import run_build

    cfg: Dict[str, Any] = {}
    for src, dst in (
        (args, "model"),
        (args, "provider"),
        (args, "api_key"),
        (args, "api_base"),
    ):
        v = getattr(src, dst, None)
        if v:
            cfg[dst] = v
    if getattr(args, "adaptive_routing", False):
        cfg["adaptive_routing"] = True
    if getattr(args, "budget", None) is not None:
        cfg["budget_cap_usd"] = args.budget
    from cli.vexconfig import apply_config_defaults, normalize_runtime_keys

    cfg = normalize_runtime_keys(apply_config_defaults(cfg))
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    started = time.time()
    try:
        out = run_build(
            params["issue_text"],
            scan.get("repo_path") or str(Path(args.repo).resolve()),
            config=cfg,
            log_root=log_root,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        from cli.errors import explain_exception
        from cli.exit_codes import classify_exit_code

        explain_exception(exc)
        return classify_exit_code(exc)
    elapsed = time.time() - started
    status = out.get("status")
    if out.get("already_passing"):
        con.print(
            "[vex.warn]already passing — the acceptance test passed on "
            "the current repo; nothing to build[/]"
        )
        return 0
    result = out.get("result")
    if result is not None:
        _print_result(result, elapsed)
    rc = 0 if status == "success" else 1
    if status == "error":
        ui.err_console().print(
            f"[vex.error]build failed: {out.get('note', 'internal error')}[/]"
        )
        rc = 1
    return rc


# ---------------------------------------------------------------------------
# analyze-history (offline cross-task learning — the maintenance job)
# ---------------------------------------------------------------------------


def cmd_analyze_history(args: argparse.Namespace) -> int:
    """Offline analysis over accumulated task logs + predictor report.

    Read-only over the logs tree; writes one report JSON under
    logs/analyze-history/<ts>/. --apply writes the difficulty-predictor
    calibration file ONLY when the report's held-out before/after
    actually improved (the job's recommendation; a deliberate,
    human-reviewed maintenance step, never live/online).
    """
    try:
        from runtime.analyze_history import apply_recommendation, build_report
    except ImportError as exc:
        ui.err_console().print(
            f"[vex.error]error: analyze-history module unavailable: {exc}[/]"
        )
        return 2
    logs_root = Path(args.log_root or "logs")
    if not logs_root.is_dir():
        ui.err_console().print(f"[vex.error]error: logs root not found: {logs_root}[/]")
        return 2
    if args.json:
        rep = build_report(
            logs_root,
            holdout_frac=args.holdout_frac,
            out_dir=Path(args.out) if args.out else None,
        )
        if args.apply:
            apply_recommendation(rep)
        print(json.dumps(rep, indent=2, default=str))
        return 0

    rep = build_report(
        logs_root,
        holdout_frac=args.holdout_frac,
        out_dir=Path(args.out) if args.out else None,
    )
    con = ui.console()
    con.print(
        f"analyzed [vex.accent2]{rep['n_real_tasks']}[/] real tasks "
        f"([vex.accent2]{rep['n_routed_tasks']}[/] adaptively routed)"
    )
    agg = rep["aggregate"]
    div = agg["predictor_divergence"]
    con.print()
    con.print("[vex.running]predictor divergence (routed tasks)[/]")
    con.print(
        f"  aligned: {div['aligned']['n']}   "
        f"false escalations: {div['false_escalation']['n']}   "
        f"missed escalations: {div['missed_escalation']['n']}"
    )
    strat = agg["retrieval_vs_repairs"]
    con.print()
    con.print("[vex.running]retrieval strategy vs repair attempts[/]")
    for k, v in strat.items():
        con.print(
            f"  {k}: n={v['n']} mean_attempts={v['mean_attempts']} "
            f"mean_repairs={v['mean_repairs']}"
        )
    fails = agg["failure_patterns"]
    con.print()
    con.print("[vex.running]failure patterns[/]")
    for k, v in fails.items():
        con.print(f"  {k}: {v['n']}")
    cal = rep["calibration"]
    con.print()
    if cal.get("status") == "ok":
        con.print("[vex.running]difficulty predictor recalibration[/]")
        b, a = rep["before_heldout"], rep["after_heldout"]
        con.print(
            f"  fitted bands: easy<={cal['bands']['easy_max']} "
            f"medium<{cal['bands']['hard_min']} hard>="
            f"{cal['bands']['hard_min']} "
            f"(train bugs: {cal['n_train']})"
        )
        con.print(
            f"  held-out before: acc={b['accuracy_easy_or_hard']} "
            f"missed={b['missed_escalations']} false={b['false_escalations']}"
        )
        con.print(
            f"  held-out after:  acc={a['accuracy_easy_or_hard']} "
            f"missed={a['missed_escalations']} false={a['false_escalations']}"
        )
        con.print(f"  recommendation: [vex.warn]{rep['recommendation']}[/]")
    else:
        con.print(
            "[vex.warn]calibration: insufficient routed data "
            f"({cal.get('n', 0)} usable rows)[/]"
        )
    if args.apply:
        written = apply_recommendation(rep)
        if written:
            con.print(f"calibration applied: [vex.ok]{written}[/]")
        else:
            con.print(
                "[vex.warn]--apply: recommendation is not 'apply' - nothing written[/]"
            )
    con.print()
    con.print(f"[vex.muted]report: {rep.get('_report_path')}[/]")
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
        "--api-base",
        "--base-url",
        dest="api_base",
        help="custom endpoint base URL (BYO router/gateway; also "
        "`vex config set base_url ...` or VEX_BASE_URL)",
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
        return "0.2.1+source"
    except Exception:
        return "0.2.1+source"


def build_parser() -> argparse.ArgumentParser:
    """Assemble the full CLI parser (exported for tests)."""
    from cli.completion import add_completion_parser
    from cli.selfupdate import add_update_parser
    from cli.uninstall import add_uninstall_parser

    epilog = (
        "exit codes: 0 success | 1 task failed | 2 usage/config error | "
        "3 environment error (Docker, deps) | 4 model/network error | "
        "130 interrupted\n"
        "docs: https://github.com/Pavanteja2007/coding-harness#readme"
    )
    parser = argparse.ArgumentParser(
        prog="vex",
        description="Vex — the AI harness that fixes bugs. Plain-language "
        "interactive mode (run `vex` with no arguments) or the "
        "scriptable subcommands below.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        # Usage/help list the PUBLIC commands only (hidden machinery
        # like `__completions` still dispatches via sub.choices).
        metavar="{fix,run-benchmark,config,login,logout,status,memory,plugin,skills,dashboard,"
        "mcp,analyze-history,update,completion,uninstall}",
    )

    # fix
    p_fix = sub.add_parser("fix", help="fix one bug in one repo (single-task mode)")
    p_fix.add_argument("--repo", required=True, help="path to the repo to fix")
    p_fix.add_argument(
        "--issue",
        required=False,
        default=None,
        help="bug report text (or @file with the report); optional with --finding",
    )
    p_fix.add_argument(
        "--finding",
        default=None,
        metavar="SCAN#N",
        help="turn a vex scan finding into this task "
        "(e.g. scan-ab12cd34#2; issue/target come from the finding)",
    )
    p_fix.add_argument("--task-id", default=None, help="override auto task_id")
    p_fix.add_argument(
        "--json",
        action="store_true",
        help="print the final result as JSON on stdout (machine-readable)",
    )
    _add_task_config_args(p_fix)
    p_fix.set_defaults(func=cmd_fix)

    # scan (Proactive Codebase Health Scan round: read-only analysis,
    # no bug report needed)
    p_scan = sub.add_parser(
        "scan",
        help="proactive read-only codebase health scan (no bug report needed)",
    )
    p_scan.add_argument("--repo", required=True, help="path to the repo to scan")
    p_scan.add_argument(
        "--remote",
        action="store_true",
        help="check dependency pins against PyPI (network; offline by default)",
    )
    p_scan.add_argument(
        "--focus",
        default=None,
        choices=("coverage", "smells", "dependencies"),
        help="run one detector only (default: all)",
    )
    p_scan.add_argument(
        "--max-findings",
        dest="max_findings",
        type=int,
        default=None,
        help="findings to show (default: 8; scan.json keeps all)",
    )
    p_scan.add_argument(
        "--json",
        action="store_true",
        help="print the scan result as JSON on stdout (machine-readable)",
    )
    p_scan.add_argument(
        "--fix",
        dest="fix",
        type=int,
        default=None,
        metavar="N",
        help="immediately turn finding N into a fix task (Task C)",
    )
    p_scan.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    p_scan.add_argument(
        "--log-root", default=None, help="task log root (default: ./logs)"
    )
    p_scan.set_defaults(func=cmd_scan)

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

    # config (two-tier settings: global + project)
    p_cfg = sub.add_parser(
        "config",
        help="view / edit the two-tier settings (global + project)",
    )
    cfg_sub = p_cfg.add_subparsers(dest="config_command", required=True)

    p_cfg_path = cfg_sub.add_parser(
        "path", help="show the settings file locations for this tier setup"
    )
    p_cfg_path.set_defaults(func=cmd_config)

    p_cfg_list = cfg_sub.add_parser(
        "list", help="effective settings + which tier each came from"
    )
    p_cfg_list.set_defaults(func=cmd_config)

    p_cfg_get = cfg_sub.add_parser("get", help="print one effective value")
    p_cfg_get.add_argument("key")
    p_cfg_get.set_defaults(func=cmd_config)

    p_cfg_set = cfg_sub.add_parser(
        "set", help="set one key (writes TOML in place; preserves comments)"
    )
    p_cfg_set.add_argument("key")
    p_cfg_set.add_argument("value")
    p_cfg_set.add_argument(
        "--tier",
        choices=("global", "project", "local"),
        default="global",
        help="which file to write (default: global)",
    )
    p_cfg_set.set_defaults(func=cmd_config)

    p_cfg_unset = cfg_sub.add_parser("unset", help="remove one key from a tier")
    p_cfg_unset.add_argument("key")
    p_cfg_unset.add_argument(
        "--tier",
        choices=("global", "project", "local"),
        default="global",
    )
    p_cfg_unset.set_defaults(func=cmd_config)

    p_cfg_init = cfg_sub.add_parser(
        "init-project",
        help="create <repo>/.vex/settings.toml (committable) and "
        "git-ignore settings.local.toml",
    )
    p_cfg_init.add_argument(
        "--project-root",
        default=None,
        help="repo root (default: current directory)",
    )
    p_cfg_init.set_defaults(func=cmd_config)

    # login / logout (first-run onboarding, cli/onboard.py)
    p_login = sub.add_parser(
        "login",
        help="configure a model endpoint (interactive wizard, saved globally)",
    )
    p_login.add_argument(
        "--tier",
        choices=("global", "project"),
        default="global",
        help="where to pin the model name (default: global; "
        "api_key/base_url always go global, never the project file)",
    )
    p_login.set_defaults(func=_cmd_login)

    p_logout = sub.add_parser(
        "logout", help="remove the stored api_key (keeps model/base_url)"
    )
    p_logout.set_defaults(func=_cmd_logout)

    # status
    p_status = sub.add_parser("status", help="show a task's structured state summary")
    p_status.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    p_status.add_argument("--task-id", required=True)
    p_status.add_argument(
        "--log-root", default=None, help="task log root (default: ./logs)"
    )
    p_status.add_argument(
        "--json",
        action="store_true",
        help="print the state summary as JSON on stdout (machine-readable)",
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

    # plugin (Plugins round, Task C: install/list/remove bundles of
    # skills + commands + tool/MCP extensions)
    p_plug = sub.add_parser(
        "plugin", help="manage plugins (bundles of skills + commands)"
    )
    plug_sub = p_plug.add_subparsers(dest="plugin_command", required=True)

    p_install = plug_sub.add_parser(
        "install", help="install a plugin from a local path or git URL"
    )
    p_install.add_argument(
        "source", help="local plugin directory or git URL (https://...)"
    )
    p_install.add_argument("--name", default=None, help="override the plugin name")
    p_install.set_defaults(func=cmd_plugin)

    p_plist = plug_sub.add_parser("list", help="list installed plugins")
    p_plist.set_defaults(func=cmd_plugin)

    p_rm = plug_sub.add_parser("remove", help="remove an installed plugin")
    p_rm.add_argument("name", help="installed plugin name")
    p_rm.set_defaults(func=cmd_plugin)

    p_enable = plug_sub.add_parser(
        "enable", help="re-enable a disabled plugin (discovery picks it up again)"
    )
    p_enable.add_argument("name", help="installed plugin name")
    p_enable.set_defaults(func=cmd_plugin)

    p_disable = plug_sub.add_parser(
        "disable", help="disable a plugin (stays installed, skipped by discovery)"
    )
    p_disable.add_argument("name", help="installed plugin name")
    p_disable.set_defaults(func=cmd_plugin)

    # analyze-history (offline cross-task learning — maintenance job)
    p_ah = sub.add_parser(
        "analyze-history",
        help="offline analysis of accumulated task logs + difficulty "
        "predictor recalibration report",
    )
    p_ah.add_argument(
        "--log-root",
        default=None,
        help="logs root to analyze (default: ./logs)",
    )
    p_ah.add_argument(
        "--holdout-frac",
        type=float,
        default=0.25,
        help="fraction of bugs held out of calibration fitting (default 0.25)",
    )
    p_ah.add_argument(
        "--out",
        default=None,
        help="report output dir (default: <logs>/analyze-history/<ts>)",
    )
    p_ah.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON on stdout (machine-readable)",
    )
    p_ah.add_argument(
        "--apply",
        action="store_true",
        help="ALSO write the difficulty calibration file — only takes "
        "effect when the report's held-out before/after improved "
        "(the job's recommendation gate)",
    )
    p_ah.set_defaults(func=cmd_analyze_history)

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
        "server", help='server label or launch command, e.g. "python -m mcp_server"'
    )
    p_mcp_list.add_argument("--cwd", default=None, help="server working directory")
    p_mcp_list.set_defaults(func=cmd_mcp_list_tools)

    p_mcp_call = mcp_sub.add_parser("call", help="call one tool on an MCP server")
    p_mcp_call.add_argument("server", help="server label or launch command")
    p_mcp_call.add_argument("tool", help="tool name to call")
    p_mcp_call.add_argument(
        "--args",
        dest="args_json",
        default="{}",
        help="tool arguments as a JSON object string",
    )
    p_mcp_call.add_argument("--cwd", default=None, help="server working directory")
    p_mcp_call.set_defaults(func=cmd_mcp_call)

    p_mcp_add = mcp_sub.add_parser(
        "add", help="register an MCP server label (stored in global settings)"
    )
    p_mcp_add.add_argument("label", help="server label (letters/digits/dots/dashes)")
    p_mcp_add.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="launch command after `--`, e.g. vex mcp add linter -- python -m mcp_server",
    )
    p_mcp_add.set_defaults(func=cmd_mcp)

    p_mcp_rm = mcp_sub.add_parser(
        "remove", help="remove an MCP server label from global settings"
    )
    p_mcp_rm.add_argument("label", help="server label")
    p_mcp_rm.set_defaults(func=cmd_mcp)

    p_mcp_ls = mcp_sub.add_parser(
        "list", help="list configured MCP servers (global < project < local)"
    )
    p_mcp_ls.set_defaults(func=cmd_mcp)

    p_mcp_health = mcp_sub.add_parser(
        "health", help="spawn each MCP server and report ok/fail"
    )
    p_mcp_health.set_defaults(func=cmd_mcp)

    # skills (read-only view over the skill scan: project/global/plugin)
    p_skills = sub.add_parser(
        "skills", help="list installed skills / show one skill body"
    )
    skills_sub = p_skills.add_subparsers(dest="skills_command", required=True)

    p_skills_list = skills_sub.add_parser(
        "list", help="list skills (name + origin + description)"
    )
    p_skills_list.add_argument(
        "--repo", default=None, help="repo for project skills (default: CWD)"
    )
    p_skills_list.set_defaults(func=cmd_skills)

    p_skills_show = skills_sub.add_parser("show", help="print one skill's full body")
    p_skills_show.add_argument("name", help="skill name")
    p_skills_show.add_argument(
        "--repo", default=None, help="repo for project skills (default: CWD)"
    )
    p_skills_show.set_defaults(func=cmd_skills)

    # self-update (Task E — CLI citizenship)
    add_update_parser(sub)

    # shell completions (Task D) + the hidden __completions backend
    add_completion_parser(sub)

    # clean uninstall (Task F)
    add_uninstall_parser(sub)

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
    # Full-screen TUI (textual) when the terminal supports it; the rich
    # REPL stays as the fallback (no TTY / VEX_TUI=0 / textual missing).
    if argv is None and not raw and sys.stdin.isatty():
        try:
            from cli.tui import can_run_tui

            if can_run_tui():
                from cli.tui import run_tui

                return run_tui(version=_get_version())
            from cli.interactive import run_interactive

            return run_interactive()
        except KeyboardInterrupt:
            ui.console().print("\n[vex.muted]interrupted[/]")
            return 130

    # Session flags are valid WITHOUT a subcommand; argparse's required
    # subparsers would reject that, so handle the bare-flag forms here.
    if session_flags and not any(
        a in raw
        for a in (
            "fix",
            "scan",
            "run-benchmark",
            "status",
            "memory",
            "dashboard",
            "mcp",
            "plugin",
            "skills",
            "config",
            "analyze-history",
            "update",
            "completion",
            "uninstall",
        )
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
        # plain-language explanation — never a raw traceback. Exit code
        # follows the failure category (task vs environment vs model) —
        # cli.exit_codes — so scripts can distinguish them.
        from cli.errors import explain_exception
        from cli.exit_codes import classify_exit_code

        try:
            _cleanup_after_interrupt()
        except Exception:
            pass
        explain_exception(exc)
        return classify_exit_code(exc)


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
