"""The ``neo`` CLI — the project's human-facing entry point
(INTERFACES.md Boundary 6).

Commands:
    neo                     -> interactive natural-language session (the
                                PRIMARY UX — type what's wrong in plain
                                language; repo = current directory)
    neo fix --repo <path> --issue <text> [--model <name>] [...]
        -> harness.core.run_task (single-task mode, no scheduler)
    neo run-benchmark --subset <name> [--concurrency <n>] [...]
        -> runtime.scheduler.run (live multi-task table while running)
    neo status --task-id <id>
        -> reads logs/{task_id}/state.json and prints a summary
    neo memory query-decisions / record / query-structure
        -> thin wrappers over the memory layer (MCP remains the primary
            programmatic interface)
    neo mcp list-tools / call — consume external MCP servers
    neo dashboard — read-only web view of existing logs
    neo analyze-history — offline cross-task analysis + difficulty
        predictor recalibration report over accumulated logs
    neo update — self-update (or --check: report the latest release)
    neo completion <shell> — print/install shell completions
    neo uninstall — remove Neo completely (config + venv + PATH)
    neo serve — the local agent server (loopback HTTP/SSE/WebSocket)
    neo acp — the Agent Client Protocol server for editors (stdio)
    neo capabilities — what THIS installation can actually do
    neo -p "sentence" — one-shot headless agent work (same JSON contract
        and exit codes as the TUI); "neo -" reads piped context

argparse (project tech lock: "a plain Python CLI"). Every command prints
something sensible even when a dependency is stubbed or misconfigured —
a CLI that crashes with a traceback fails the user worse than one that
explains what's missing. All rendering goes through cli.ui (rich + the
Neo oxblood theme; ANSI auto-degrades on dumb Windows consoles,
--no-color / NO_COLOR force plain).

Exit codes (cli.exit_codes — the stable machine contract; scripts/CI):
0 success / 1 task-level failure / 2 usage or config error /
3 environment error (Docker, dependencies) / 4 model or network error /
130 interrupted (Ctrl+C). Legacy 0/1/2 scripts keep working.

Entry points: ``neo`` console script (pip install -e .), ``python -m cli``,
and the legacy ``harness`` alias (kept until all docs migrate).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from cli import deps, ui
from cli import runview as _rv
from cli.commands import cmd_worktree
from memory.paths import decisions_db_path, default_logs_dir, safe_task_dir

if TYPE_CHECKING:
    from shared.types import Task, TaskResult


def _safe_task_dir(task_id: str, log_root: Path):
    """log_root/<task_id>/ for a single-segment task id, else None
    (containment guard for `neo status --task-id` — Round 6
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
    if getattr(args, "profile", None):
        config["provider_profile"] = args.profile
    if getattr(args, "adaptive_routing", None):
        config["adaptive_routing"] = True
    if getattr(args, "approval", None):
        config["approval"] = "require"
        config.setdefault("approval_timeout_s", 3600.0)
    if getattr(args, "protected", None):
        config["protected_paths"] = list(args.protected)
    config.update(extra_config or {})
    # Two-tier settings (global + project) fill keys the flags didn't set:
    # flags > env (NEO_MODEL/NEO_BASE_URL/NEO_API_KEY/...) > files.
    # base_url (the settings name) normalizes onto runtime's api_base here
    # so any OpenAI-compatible router works with any model name.
    from cli.neoconfig import apply_config_defaults, normalize_runtime_keys

    config = normalize_runtime_keys(
        apply_config_defaults(config, start=Path(args.repo))
    )
    # A credential connected through `/connect` lives in <neo_home>/auth.json
    # and is NEVER written into a config file. ONE additive overlay, last, and
    # only for keys the flags and the settings chain did not supply.
    try:
        from cli.auth import apply_active_credential

        config = apply_active_credential(config)
    except Exception:
        pass
    try:
        from cli.plugins import config_tool_verbs

        config["plugin_tool_verbs"] = config_tool_verbs()
    except Exception:
        pass
    return Task(
        task_id=getattr(args, "task_id", None) or f"fix-{uuid.uuid4().hex[:8]}",
        repo_path=str(Path(args.repo).resolve()),
        issue_text=args.issue,
        config=config,
    )


def _prompt_cache_json(result: "TaskResult") -> Dict[str, Any]:
    """Machine-readable prompt-cache receipt for one TaskResult.

    Folded from the run's own ledger/trace, so the numbers are the provider's
    reported cache tokens. A run whose provider never reported cache usage
    returns ``{"available": false, ...}`` — the absence is stated rather than
    rendered as a 0% hit rate that would read as a measured regression.
    """
    empty: Dict[str, Any] = {
        "available": False,
        "calls": 0,
        "hits": 0,
        "decided_calls": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_hit_rate": 0.0,
    }
    try:
        from pathlib import Path

        from cli.interactive import trace_cache_summary

        log_path = str(getattr(result, "log_path", "") or "")
        if not log_path:
            return empty
        trace = Path(log_path) / "trace.jsonl"
        ledger = Path(log_path).parent / f"{Path(log_path).name}.runtime"
        summary = trace_cache_summary(trace)
        if not summary.get("available"):
            candidate = Path(log_path).parent / "runtime" / "model_ledger.jsonl"
            if candidate.is_file():
                summary = trace_cache_summary(candidate)
            else:
                del ledger
        if not summary.get("available"):
            return empty
        return {
            "available": True,
            "calls": int(summary.get("calls") or 0),
            "hits": int(summary.get("hits") or 0),
            "decided_calls": int(summary.get("decided") or 0),
            "cached_input_tokens": int(summary.get("cached_tokens") or 0),
            "cache_creation_input_tokens": int(summary.get("creation_tokens") or 0),
            "cache_hit_rate": round(float(summary.get("hit_rate") or 0.0), 4),
            "cache_status_counts": dict(summary.get("statuses") or {}),
        }
    except Exception:
        return empty


def _result_json(result: "TaskResult", elapsed: float) -> Dict[str, Any]:
    """Machine-readable view of one TaskResult (neo fix --json).

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
    out["prompt_cache"] = _prompt_cache_json(result)
    out["effort"] = _effort_json(result)
    out["exit_code"] = 0 if result.status == "success" else 1
    out["exit_reason"] = reason_for(out["exit_code"])
    return out


#: How many per-call effort receipts `--json` publishes. The point of the field
#: is that a cost claim is EXPLAINABLE, which a single run-level rung is not;
#: the bound is so a 400-call run cannot turn a result document into a log.
EFFORT_RECEIPT_LIMIT = 50


def _effort_json(result: Any) -> Dict[str, Any]:
    """Return the run's effort receipt for `--json` (AGT-08).

    Two fields, and the second is the load-bearing one. `level` is what was
    requested; `sent` is the STATUS the router reported per call, so a caller
    can tell "ran at high" from "asked for high, provider has no such knob".
    Publishing only the count of model calls (the historical shape) is what
    made the claim unfalsifiable from a script.

    Reads only what `TaskResult.model_calls` already carries and never raises:
    a run whose boundary knew nothing about effort reports `supported: false`
    with an empty receipt list rather than a missing key.
    """
    calls = [
        call
        for call in (getattr(result, "model_calls", None) or [])
        if isinstance(call, dict)
    ]
    levels = sorted(
        {str(call.get("effort") or "") for call in calls if call.get("effort")}
    )
    statuses = sorted(
        {
            str(call.get("effort_status") or "")
            for call in calls
            if call.get("effort_status")
        }
    )
    parameters = sorted(
        {
            str(call.get("effort_parameter") or "")
            for call in calls
            if call.get("effort_parameter")
        }
    )
    sent = [bool(call.get("effort_sent")) for call in calls if "effort_sent" in call]
    return {
        "level": levels[0] if len(levels) == 1 else (levels or ["auto"]),
        "levels_seen": levels,
        "statuses": statuses,
        "parameters": parameters,
        "supported": bool(sent) and any(sent),
        "reported_by_calls": len(sent),
        "calls_with_effort": sum(1 for call in calls if call.get("effort")),
        "receipts": [
            {
                "step": call.get("step"),
                "call_index": call.get("call_index"),
                "model": call.get("model"),
                "effort": call.get("effort"),
                "effort_status": call.get("effort_status"),
                "effort_sent": bool(call.get("effort_sent")),
                "effort_parameter": call.get("effort_parameter"),
                "tokens": call.get("tokens"),
                "cost": call.get("cost"),
            }
            for call in calls[:EFFORT_RECEIPT_LIMIT]
        ],
        "receipts_truncated": max(0, len(calls) - EFFORT_RECEIPT_LIMIT),
    }


def _print_result(result: "TaskResult", elapsed: float) -> None:
    """Human-readable summary of one TaskResult (Neo theme, rendered diff)."""
    con = ui.console()
    style = "neo.ok" if result.status == "success" else "neo.error"
    con.print()
    ui.rule(f"[{style}]task {result.task_id}: {result.status}[/]")
    con.print(f"[neo.muted]attempts:     {result.attempts}[/]")
    con.print(f"[neo.muted]cost:         {ui.fmt_cost(result.cost_usd)}[/]")
    con.print(f"[neo.muted]model calls:  {len(result.model_calls)}[/]")
    if elapsed >= 1:
        con.print(f"[neo.muted]elapsed:      {elapsed:.1f}s[/]")
    if result.verification is not None:
        v = result.verification
        con.print(
            f"[neo.muted]target test:  "
            f"[{'neo.ok' if v.target_test_passed else 'neo.error'}]"
            f"{'PASS' if v.target_test_passed else 'FAIL'}[/]"
        )
        con.print(
            f"[neo.muted]regression:    "
            f"[{'neo.ok' if v.regression_passed else 'neo.error'}]"
            f"{'PASS' if v.regression_passed else 'FAIL'}[/]"
        )
        con.print(f"[neo.muted]flaky:        {v.flaky}[/]")
    if result.diff:
        con.print()
        ui.rule("[neo.accent]diff[/]")
        ui.print_diff(result.diff)
    elif result.status == "success":
        con.print("[neo.muted](no diff — target already passed before any edit)[/]")
    elif result.status == "failed":
        con.print("[neo.muted](no passing diff to show)[/]")
    # rationale.md (Task E): render the grounded paragraph when written
    rat = Path(result.log_path).parent / "rationale.md"
    if rat.is_file():
        from rich.markdown import Markdown

        con.print()
        ui.rule("[neo.accent]rationale[/]")
        con.print(Markdown(rat.read_text(encoding="utf-8")))
    if result.log_path and Path(result.log_path).exists():
        con.print(f"\n[neo.muted]full trace:   {result.log_path}[/]")


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


def cmd_fix(args: argparse.Namespace) -> int:
    """Run one bug-fix task through the real harness, single-task mode.

    --finding <scan_id>#<n>: turn a `neo scan` finding into this task
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
                "[neo.error]error: --repo is required with --finding[/]"
            )
            return 2
        return _run_finding(args, con)
    repo = Path(args.repo)
    if not repo.is_dir():
        ui.err_console().print(
            f"[neo.error]error: --repo is not a directory: {args.repo}[/]"
        )
        return 2
    if not args.issue:
        ui.err_console().print("[neo.error]error: --issue is required[/]")
        return 2
    issue = args.issue
    if issue.startswith("@"):
        # --issue @report.txt — read the bug report from a file
        issue_path = Path(issue[1:])
        try:
            issue = issue_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            ui.err_console().print(
                f"[neo.error]error: issue file is not valid UTF-8 text: {issue_path}[/]"
            )
            return 2
        except (OSError, ValueError) as exc:
            # ValueError covers null-byte paths on Windows
            ui.err_console().print(
                f"[neo.error]error: cannot read issue file {issue_path}: {exc}[/]"
            )
            return 2
        if not issue:
            ui.err_console().print(
                f"[neo.error]error: issue file is empty: {issue_path}[/]"
            )
            return 2
        args.issue = issue

    task = _make_task(args)
    log_root = _artifact_log_root(args.log_root, getattr(args, "repo", None))
    # --worktree NAME: run inside an isolated Git worktree so the original
    # checkout is never mutated by the run. The worktree is created BEFORE any
    # model call, and a dirty source checkout fails closed (exit 2) rather
    # than silently pinning a base commit that excludes local work.
    if str(getattr(args, "worktree", "") or ""):
        from cli.commands import WorktreeCommandError, worktree_run_config

        try:
            isolation = worktree_run_config(
                str(args.repo),
                worktree=str(args.worktree),
                base=str(getattr(args, "worktree_base", "") or ""),
                log_root=str(log_root),
            )
        except WorktreeCommandError as exc:
            ui.err_console().print(f"[neo.error]error: {exc}[/]")
            return 2
        except Exception as exc:
            ui.err_console().print(f"[neo.error]error: --worktree failed: {exc}[/]")
            return 2
        task.repo_path = str(isolation["worktree_path"])
        task.config.update(isolation)
        if not as_json:
            con.print(f"[neo.muted]worktree: {task.repo_path}[/]")
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
                        "error": "no model configured — run `neo login` "
                        "(or set NEO_MODEL/NEO_API_KEY)",
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
    # NEO_TRACE_DIR to its logs root so scheduler/worker/router/sandbox/
    # memory events land in <log_root>/_trace/ next to the harness trace —
    # `python -m shared.traceview <task_id>` reconstructs the whole task
    # from one place. setdefault: an operator who set it wins.
    os.environ.setdefault("NEO_TRACE_DIR", str(log_root))
    if not as_json:
        con.print(
            f"[neo.accent]neo fix[/] [neo.muted]{ui.GLYPHS['arrow']} {task.repo_path}[/]"
        )
        con.print(f"[neo.muted]task:   {task.task_id}[/]")
        con.print(f"[neo.muted]issue:  {args.issue[:200]}[/]")
    _set_router_context(
        task
    )  # same pattern as runtime.worker (per-process router config)
    run_task = deps.get_run_task()
    started = time.time()
    mon = LiveMonitor(task.task_id, log_root).start(quiet=as_json)
    # `neo fix --approval` is a real gate, not a config passthrough: the
    # worker parks in runtime.approval and needs an approver. run-benchmark
    # already starts one watcher per approval task (below); the single-task
    # flag path used to set config["approval"]="require" and then run with
    # nobody able to answer, so the run could only ever time out. Start the
    # same watcher here, and treat a rejected/timeout gate as NOT verified.
    approval_stop = None
    approval_thread = None
    if (task.config or {}).get("approval") == "require" and not as_json:
        import threading

        from cli.interactive import watch_for_approvals

        approval_stop = threading.Event()
        approval_thread = threading.Thread(
            target=watch_for_approvals,
            args=(log_root, [task.task_id], approval_stop),
            daemon=True,
        )
        approval_thread.start()
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
        if approval_stop is not None:
            approval_stop.set()
            approval_thread.join(timeout=2.0)
        mon.stop()
        _clear_router_context()
    elapsed = time.time() - started
    approval_state = _approval_outcome(log_root, task.task_id)
    if as_json:
        payload = _result_json(result, elapsed)
        payload["approval"] = approval_state
        # A rejected or timed-out gate is a task failure, never a success.
        if (
            approval_state.get("required")
            and approval_state.get("decision") != "approved"
        ):
            payload["status"] = "approval_required"
            payload["exit_reason"] = "approval_not_granted"
            payload["exit_code"] = 1
            print(json.dumps(payload, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if result.status == "success" else 1
    _print_result(result, elapsed)
    if approval_state.get("required") and approval_state.get("decision") != "approved":
        con.print(
            f"[neo.warn]approval:[/] {approval_state.get('decision', 'not granted')} "
            f"[neo.muted]({approval_state.get('reason', 'no decision recorded')})[/]"
        )
        con.print(
            "[neo.muted]this result is NOT verified; the gate was not "
            "approved · re-run with approval or inspect "
            f"{task.task_id}[/]"
        )
        return 1
    # Task F (interaction-polish round): the terminal bell fires when a
    # --json run would stay silent (a scripted caller owns its own
    # notification story; stderr-only here, never pollutes stdout).
    ui.bell(f"neo fix {result.status}")
    return 0 if result.status == "success" else 1


def _approval_outcome(log_root: Path, task_id: str) -> Dict[str, Any]:
    """Summarize the approval gate's OWN records for one finished task.

    Reads only the gate's review log and decision file — the structured
    record the runtime writes. Never infers approval from the run's status
    or from captured output, so a rejected or expired gate can never be
    presented as a verified result.

    The reading itself is `cli.commands.approval_gate_outcome`, shared with
    the REPL watcher and the TUI: three surfaces each reducing the same file
    themselves is three chances to report one gate three different ways.
    """
    from cli import commands as commands_mod

    gate = Path(log_root) / f"{task_id}.runtime" / "approval"
    return commands_mod.approval_gate_outcome(gate)


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
                "provider_profile": cfg.get("provider_profile"),
                "display_label": cfg.get("display_label") or cfg.get("label"),
                "source_tier": cfg.get("source_tier"),
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
        con.print(f"[neo.muted]benchmark subset {subset!r}: no tasks found[/]")
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
                    f"[neo.error]error: {reason} for task {t.task_id} — run "
                    "`neo login` to configure a model (or set "
                    "NEO_MODEL/NEO_API_KEY)[/]"
                )
                return 4

    log_root = _artifact_log_root(args.log_root, getattr(args, "repo", None))
    con.print(
        f"[neo.accent]neo run-benchmark[/] [neo.muted]"
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
    ui.rule(f"[neo.accent]benchmark {subset}: done in {elapsed:.1f}s[/]")
    if results:
        con.print(
            f"[neo.ok]success {n_ok}[/][neo.muted] / {len(results)}   "
            f"[neo.error]failed {n_fail}[/][neo.muted]   "
            f"error/timeout {n_err}   "
            f"cost [/][neo.accent]{ui.fmt_cost(cost)}[/]"
        )
    for r in results:
        mark = {
            "success": f"[neo.ok]{ui.GLYPHS['ok']}",
            "failed": f"[neo.error]{ui.GLYPHS['fail']}",
            "error": f"[neo.error]{ui.GLYPHS['fail']}",
            "timeout": f"[neo.warn]{ui.GLYPHS['wait']}",
        }.get(r.status, "?")
        con.print(
            f"  {mark} [neo.muted]{r.task_id}: {r.status} "
            f"({r.attempts} attempt(s), {ui.fmt_cost(r.cost_usd)})[/]"
        )
    ui.bell(f"neo benchmark {subset}: {n_ok}/{len(results)} success")
    return 0


class _BenchmarkLiveView:
    """Live in-terminal multi-task dashboard of a running benchmark
    (Task E of the neo pass; the interaction-polish round's Task D made
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
            title=f"[neo.accent]neo benchmark[/] [neo.muted]"
            f"({n_done}/{len(rows)} done · {int((now or time.time()) - self._t0)}s)[/]",
            title_justify="left",
            expand=False,
        )
        table.add_column("task", style="neo.muted", no_wrap=True)
        table.add_column("state", no_wrap=True)
        table.add_column("phase", style="neo.muted", no_wrap=True)
        table.add_column("elapsed", justify="right", no_wrap=True)
        table.add_column("calls", justify="right", no_wrap=True)
        table.add_column("cost", justify="right", no_wrap=True)
        table.add_column("tier · model", style="neo.muted", no_wrap=True)
        for r in rows:
            state = r["state"]
            if state == "success":
                mark = f"[neo.ok]{ui.GLYPHS['ok']} success"
            elif state in ("failed", "error"):
                mark = f"[neo.error]{ui.GLYPHS['fail']} {state}"
            elif state == "timeout":
                mark = f"[neo.warn]{ui.GLYPHS['wait']} timeout"
            elif state == "running":
                mark = f"[neo.running]{ui.GLYPHS['bullet']} {r['phase'] or 'running'}"
            else:
                mark = f"[neo.muted]· {state}"
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
                f"[neo.error]error: smoke fixture repo missing: {fixture}[/]"
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
            f"[neo.error]error: unknown subset {subset!r} "
            f"(expected 'smoke' or a JSON file)[/]"
        )
        return None
    try:
        # utf-8-sig: tolerates the BOM Windows editors (PowerShell, Notepad) prepend
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        ui.err_console().print(
            f"[neo.error]error: cannot read subset file {subset}: {exc}[/]"
        )
        return None
    if not isinstance(data, list):
        ui.err_console().print(
            "[neo.error]error: subset file must be a JSON list of task objects[/]"
        )
        return None

    tasks: List[Task] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "repo" not in item or "issue" not in item:
            ui.err_console().print(
                f"[neo.error]error: subset entry {i} needs 'repo' and 'issue'[/]"
            )
            return None
        # Type validation (Round 6 adversarial hardening): non-string /
        # empty repo or issue used to escape as a traceback deep in the
        # scheduler path; now a clean usage error.
        repo = item["repo"]
        issue = item["issue"]
        if not isinstance(repo, str) or not repo.strip():
            ui.err_console().print(
                f"[neo.error]error: subset entry {i}: 'repo' must be a "
                f"non-empty string[/]"
            )
            return None
        if "\x00" in repo:
            # fail fast at the boundary: a null-byte path can only crash
            # downstream (workers spawn against an unusable path)
            ui.err_console().print(
                f"[neo.error]error: subset entry {i}: 'repo' contains a null byte[/]"
            )
            return None
        if not isinstance(issue, str) or not issue.strip():
            ui.err_console().print(
                f"[neo.error]error: subset entry {i}: 'issue' must be a "
                f"non-empty string[/]"
            )
            return None
        task_id = item.get("task_id")
        if task_id is not None and (
            not isinstance(task_id, str) or not task_id.strip()
        ):
            ui.err_console().print(
                f"[neo.error]error: subset entry {i}: 'task_id' must be a "
                f"non-empty string[/]"
            )
            return None
        cfg_in = item.get("config") or {}
        if not isinstance(cfg_in, dict):
            ui.err_console().print(
                f"[neo.error]error: subset entry {i}: 'config' must be an object[/]"
            )
            return None
        from cli.neoconfig import apply_config_defaults, normalize_runtime_keys

        selected_profile = getattr(args, "profile", None)
        initial_config = dict(cfg_in)
        if selected_profile:
            initial_config["provider_profile"] = selected_profile
        cfg = normalize_runtime_keys(
            apply_config_defaults(initial_config, start=Path(repo).expanduser())
        )
        if item.get("target_test"):
            cfg["target_test"] = item["target_test"]
        if item.get("test_command"):
            cfg["test_command"] = item["test_command"]
        flag_values = {
            "model": getattr(args, "model", None),
            "provider": getattr(args, "provider", None),
            "api_key": getattr(args, "api_key", None),
            "api_base": getattr(args, "api_base", None),
            "provider_profile": getattr(args, "profile", None),
            "max_retries": getattr(args, "max_retries", None),
            "budget_cap_usd": getattr(args, "budget", None),
            "test_command": getattr(args, "test_command", None),
            "target_test": getattr(args, "target_test", None),
        }
        cfg.update(
            {key: value for key, value in flag_values.items() if value is not None}
        )
        if getattr(args, "adaptive_routing", None):
            cfg["adaptive_routing"] = True
        if getattr(args, "approval", None):
            cfg["approval"] = "require"
            cfg.setdefault("approval_timeout_s", 3600.0)
        if getattr(args, "protected", None):
            cfg["protected_paths"] = list(args.protected)
        cfg = normalize_runtime_keys(cfg)
        try:
            from cli.plugins import config_tool_verbs

            cfg["plugin_tool_verbs"] = config_tool_verbs()
        except Exception:
            pass
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
    from shared.security import redact_secrets

    return redact_secrets(state), task_dir, 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Follow a run's event journal until it reaches a terminal event.

    This is the surface that survives the TUI dying: it holds no process
    handle, only a byte offset into `logs/{task_id}/trace.jsonl`, so
    `detach` → kill the TUI → `neo watch` reconstructs the run's live
    state from the journal with no gap.

    `--json` prints exactly one document on stdout and nothing else, so a
    machine consumer never has to strip a progress line. The progress
    lines themselves go to stderr.
    """
    from rich.markup import escape

    from cli import background as _bg

    con = ui.console()
    err = ui.err_console()
    as_json = bool(getattr(args, "json", False))
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    task_id = str(getattr(args, "task_id", "") or "")

    def render(frame: Dict[str, Any]) -> None:
        if as_json:
            return
        phase = str(frame.get("phase_label") or frame.get("phase") or "")
        live = str(frame.get("live_text") or "").strip()
        tail = live.splitlines()[-1] if live else ""
        if len(tail) > 72:
            tail = tail[:72] + "..."
        err.print(
            f"[neo.muted]{frame.get('events', 0)} events[/] "
            f"[neo.running]{escape(phase)}[/]"
            + (f" [neo.muted]{escape(tail)}[/]" if tail else "")
        )

    receipt = _bg.watch(
        log_root,
        task_id,
        interval_s=float(getattr(args, "interval_s", 0.25) or 0.25),
        timeout_s=float(getattr(args, "timeout_s", 0.0) or 0.0),
        on_frame=render,
    )
    if as_json:
        con.print_json(data=_jsonable(receipt))
        # Exit code carries the RUN's outcome, not the follower's: a
        # machine consumer must be able to tell a failed run from a
        # succeeded one without parsing the document. "Still running" is
        # not a failure of the watch, so it stays 0.
        if not receipt.get("watched"):
            return 2
        if receipt.get("terminal_seen") and str(receipt.get("phase")) == "failed":
            return 1
        return 0
    if not receipt.get("watched"):
        err.print(
            f"[neo.error]cannot watch {escape(task_id)}: "
            f"{escape(str(receipt.get('reason') or 'unknown reason'))}[/]"
        )
        return 2
    status = str(receipt.get("status") or "")
    if receipt.get("terminal_seen"):
        if str(receipt.get("phase")) == "failed":
            err.print(
                f"[neo.error]{escape(task_id)} FAILED[/] "
                f"[neo.muted]status {escape(status or 'unknown')} · "
                f"{receipt.get('events', 0)} events[/]"
            )
            return 1
        err.print(
            f"[neo.ok]{escape(task_id)} finished[/] "
            f"[neo.muted]status {escape(status or 'unknown')} · "
            f"{receipt.get('events', 0)} events · "
            f"{receipt.get('frames', 0)} frames[/]"
        )
        return 0
    err.print(
        f"[neo.warn]{escape(task_id)} still running[/] "
        f"[neo.muted]({escape(str(receipt.get('reason') or 'no terminal event yet'))} · "
        f"phase {escape(str(receipt.get('phase') or 'unknown'))})[/]"
    )
    return 0


def _jsonable(payload: Any) -> Any:
    """Coerce a receipt to JSON-safe values without raising."""
    if isinstance(payload, dict):
        return {str(k): _jsonable(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_jsonable(item) for item in payload]
    if isinstance(payload, (str, int, float, bool)) or payload is None:
        return payload
    return str(payload)


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
                f"[neo.error]error: invalid task id: {task_id!r} "
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
                        f"\n[neo.muted]available task dirs under {log_root}: {shown}[/]"
                    )
            err.print(
                f"[neo.error]no state file at {state_file}[/][neo.muted]{hint}[/]"
            )
            return 2
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            from shared.security import redact_secrets

            state = redact_secrets(state)
        except (OSError, ValueError) as exc:
            err.print(f"[neo.error]error: cannot read {state_file}: {exc}[/]")
            return 2
    else:
        state, task_dir, code = _load_status_state(task_id, log_root)
        if code != 0 or state is None:
            err.print(
                f"[neo.error]error: no readable state for task {task_id!r} "
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
        evidence = data.get("verification_evidence") or data.get("verification")
        if isinstance(evidence, dict):
            evidence = [evidence]
        display_status = _rv.effective_terminal_status(data.get("status"), evidence)
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
                "status": display_status,
                "cost_usd": data.get("cost_usd"),
                "note": ui.strip_ansi(data.get("note") or ""),
            }
            if result_event
            else None,
        }
        print(json.dumps(payload, indent=2))
        return 0

    ui.rule(f"[neo.accent]task {state.get('task_id', task_id)}[/]")
    con.print(
        f"[neo.muted]state file: {(task_dir or log_root / task_id) / 'state.json'}[/]"
    )
    done = "all" if plan and not remaining else f"{len(completed)}/{len(plan)}"
    con.print(f"[neo.muted]progress:   {done} step(s) complete[/]")
    if plan:
        con.print(f"[neo.muted]plan ({len(plan)}):[/]")
        for step in plan:
            mark = "[neo.ok]x[/]" if step in completed else " "
            con.print(f"  [{mark}] {step}")
    if files_touched:
        con.print(f"[neo.muted]files touched ({len(files_touched)}):[/]")
        for f in files_touched:
            con.print(f"  [neo.muted]- {f}[/]")
    if decisions:
        con.print(f"[neo.muted]decisions ({len(decisions)}):[/]")
        for d in decisions:
            con.print(f"  [neo.muted]- {d}[/]")
    if remaining:
        con.print(f"[neo.muted]remaining ({len(remaining)}):[/]")
        for r in remaining:
            con.print(f"  [neo.muted]- {r}[/]")

    if result_event:
        status = data.get("status", "?")
        evidence = data.get("verification_evidence") or data.get("verification")
        if isinstance(evidence, dict):
            evidence = [evidence]
        display_status = _rv.effective_terminal_status(status, evidence)
        style = (
            "neo.ok"
            if _rv.status_is_verified(display_status)
            else "neo.warn"
            if _rv.status_is_completed(display_status)
            else "neo.error"
        )
        con.print(f"[neo.muted]result:     [{style}]{display_status}[/]")
        if "cost_usd" in data:
            con.print(
                f"[neo.muted]cost:       {ui.fmt_cost(data.get('cost_usd', 0))}[/]"
            )
        if data.get("note"):
            con.print(f"[neo.muted]note:       {ui.strip_ansi(data['note'])}[/]")
    return 0


def _tail_result_from_trace(trace_file: Path) -> Optional[Dict[str, Any]]:
    """Last legacy or strict terminal event from a trace.jsonl (small files:
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
        kind, payload, _timestamp, _identity = _rv.event_parts(obj)
        if kind in ("result", "task_end", "run_finished"):
            data = dict(payload)
            nested = data.get("result")
            if isinstance(nested, dict):
                merged = dict(nested)
                merged.update(
                    {key: value for key, value in data.items() if key != "result"}
                )
                data = merged
            return {"kind": kind, "data": data}
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
        ui.err_console().print("[neo.error]error: empty decision text[/]")
        return 2
    con.print(f"[neo.ok]recorded decision #{rid}[/]: {args.text}")
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
            "[neo.error]error: --repo is required for query-structure[/]"
        )
        return 2
    from memory.code_graph import CodeGraph

    try:
        graph = CodeGraph(args.repo)
    except NotADirectoryError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2
    except (OSError, ValueError) as exc:
        # null-byte paths / permission errors surface as clean usage errors
        ui.err_console().print(
            f"[neo.error]error: cannot index repo {args.repo!r}: {exc}[/]"
        )
        return 2
    ui.console().print(graph.query(args.query or "help"))
    return 0


def cmd_memory_ingest(args: argparse.Namespace) -> int:
    """One-shot ingestion of all state files under a logs dir."""
    store = _store()
    new = store.poll(args.logs_dir or str(default_logs_dir()))
    ui.console().print(
        f"[neo.ok]ingested {new} new decision(s)[/] "
        f"[neo.muted]from {args.logs_dir or default_logs_dir()}[/]"
    )
    return 0


# ---------------------------------------------------------------------------
# config (two-tier settings: global + project — neo config ...)
# ---------------------------------------------------------------------------


def _config_source_of(key: str, effective: Dict[str, Any]) -> str:
    """Which tier last set an effective key (for config output)."""
    from cli import neoconfig

    label = neoconfig.value_source(key, effective=effective)
    if label == "legacy":
        return "legacy (~/.neo/config.toml)"
    return label


_SECRET_KEYS = {"api_key", "api_base"}
_REDACTED = {"api_key"}


def _display_value(key: str, value: Any) -> str:
    """Render a config value for display without endpoint credentials."""
    from cli import neoconfig

    if key in _REDACTED or key in (
        "api_base",
        "base_url",
        "provider_profile",
        "provider_profiles",
    ):
        public = neoconfig.public_value(key, value)
        return (
            json.dumps(public, sort_keys=True)
            if isinstance(public, dict)
            else str(public)
        )
    return repr(value)


def _json_config_value(key: str, value: Any) -> Any:
    """Return a JSON-safe, non-secret config value."""
    from cli import neoconfig

    if key in _REDACTED or key in (
        "api_base",
        "base_url",
        "provider_profile",
        "provider_profiles",
    ):
        return neoconfig.public_value(key, value)
    if isinstance(value, (dict, list)):
        return "<structured>"
    return value


def cmd_config(args: argparse.Namespace) -> int:
    """`neo config <subcommand>` — view and edit the two-tier settings."""
    from cli import neoconfig

    con = ui.console()
    sub = args.config_command
    as_json = bool(getattr(args, "json", False))

    if sub in ("list", "get") and as_json:
        resolved = neoconfig.effective_settings_with_sources()
        if sub == "get":
            key = args.key
            if key not in resolved["values"]:
                print(json.dumps({"key": key, "set": False, "source": "default"}))
                return 1
            print(
                json.dumps(
                    {
                        "key": key,
                        "set": True,
                        "value": _json_config_value(key, resolved["values"][key]),
                        "source": resolved["sources"].get(key, "default"),
                        "source_tier": resolved["source_tiers"].get(key, "default"),
                    },
                    indent=2,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "values": {
                        key: _json_config_value(key, value)
                        for key, value in sorted(resolved["values"].items())
                    },
                    "sources": resolved["sources"],
                    "source_tiers": resolved["source_tiers"],
                },
                indent=2,
            )
        )
        return 0

    if sub in ("status", "provider"):
        resolved = neoconfig.public_provider_config()
        if as_json:
            print(json.dumps(resolved, indent=2))
            return 0
        model = resolved.get("model") or "(none — run `neo login`)"
        provider = resolved.get("provider") or "(router default)"
        source = resolved.get("source_tier") or "default"
        label = resolved.get("display_label") or ""
        profile = resolved.get("profile") or ""
        suffix = f" · profile: {profile}" if profile else ""
        suffix += f" · {label}" if label else ""
        con.print(
            f"[neo.accent]{model}[/] [neo.muted]· {provider} · source: {source}{suffix}[/]"
        )
        if resolved.get("api_base"):
            con.print(f"[neo.muted]base_url: {resolved['api_base']}[/]")
        return 0

    if sub == "path":
        gp = neoconfig.global_settings_path()
        con.print(f"[neo.accent]global[/][neo.muted]  {gp}[/]")
        pd_ = neoconfig.project_settings_dir()
        if pd_ is not None:
            con.print(f"[neo.accent]project[/][neo.muted] {pd_ / 'settings.toml'}[/]")
            con.print(
                f"[neo.accent]local[/][neo.muted]    {pd_ / 'settings.local.toml'}[/]"
            )
        else:
            con.print(
                "[neo.muted]project (none found — `neo config "
                "init-project` creates .neo/settings.toml)[/]"
            )
        if neoconfig.legacy_settings_path().is_file() and not gp.is_file():
            con.print(
                f"[neo.muted]legacy[/]    {neoconfig.legacy_settings_path()} "
                "(read as global fallback until the new file exists)[/]"
            )
        return 0

    if sub == "list":
        eff = neoconfig.effective_settings()
        if not eff:
            con.print("[neo.muted]no settings set (all defaults in effect)[/]")
        else:
            ui.rule("[neo.accent]effective settings[/]")
            for key in sorted(eff):
                origin = _config_source_of(key, eff)
                con.print(
                    f"  [neo.accent]{key}[/] = {_display_value(key, eff[key])} "
                    f"[neo.muted]({origin})[/]"
                )
        con.print(
            "\n[neo.muted]tier files that exist on this chain (low -> high "
            "merge order):[/]"
        )
        chain = neoconfig.settings_chain()
        if chain:
            for label, p, _data in chain:
                con.print(f"  [neo.muted]{label:13} {p}[/]")
        else:
            con.print("  [neo.muted](none)[/]")
        pd_ = neoconfig.project_settings_dir()
        if pd_ is not None:
            gitignore = pd_.parent / ".gitignore"
            ignore_text = (
                gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
            )
            for relative, entry in (
                ("settings.local.toml", ".neo/settings.local.toml"),
                ("connectors.local.toml", ".neo/connectors.local.toml"),
            ):
                path = pd_ / relative
                if path.is_file() and not neoconfig._gitignore_covers(
                    ignore_text, entry
                ):
                    con.print(
                        f"[neo.warn]warning: {path} is NOT in .gitignore — "
                        "run `neo config init-project` to fix[/]"
                    )
        return 0

    if sub == "get":
        eff = neoconfig.effective_settings()
        if args.key not in eff:
            con.print(f"[neo.muted]{args.key}: not set (default in effect)[/]")
            return 1
        con.print(
            f"{args.key} = {_display_value(args.key, eff[args.key])} "
            f"[neo.muted]({_config_source_of(args.key, eff)})[/]"
        )
        return 0

    if sub == "set":
        try:
            value = neoconfig.coerce_value(args.key, args.value)
        except ValueError as exc:
            ui.err_console().print(f"[neo.error]error: {exc}[/]")
            return 2
        try:
            path, created = neoconfig.set_tier_key(args.tier, args.key, value)
        except (ValueError, OSError) as exc:
            ui.err_console().print(f"[neo.error]error: {exc}[/]")
            return 2
        rel = f" ({args.tier} tier)" if args.tier != "global" else ""
        con.print(
            f"[neo.ok]set {args.key} = {_display_value(args.key, value)}[/] "
            f"[neo.muted]{rel} -> {path}[/]"
        )
        if args.tier in ("local", "project"):
            ensure = neoconfig.ensure_gitignore(tier_project_root(args.tier))
            if ensure in ("appended", "created"):
                con.print(
                    "[neo.ok]gitignore covers .neo/settings.local.toml[/] "
                    "[neo.muted](kept out of commits automatically)[/]"
                )
        return 0

    if sub == "unset":
        try:
            outcome, path = neoconfig.unset_tier_key(args.tier, args.key)
        except (ValueError, OSError) as exc:
            ui.err_console().print(f"[neo.error]error: {exc}[/]")
            return 2
        verb = "unset" if outcome == "removed" else "was not set"
        con.print(f"[neo.ok]{args.key} {verb}[/] [neo.muted]({path})[/]")
        return 0 if outcome == "removed" else 1

    if sub == "init-project":
        root = Path(args.project_root or Path.cwd()).resolve()
        if not root.is_dir():
            ui.err_console().print(f"[neo.error]error: not a directory: {root}[/]")
            return 2
        try:
            info = neoconfig.ensure_project_layout(root)
        except OSError as exc:
            ui.err_console().print(f"[neo.error]error: {exc}[/]")
            return 2
        created, path = info["settings_created"], info["path"]
        if created:
            con.print(f"[neo.ok]created[/] [neo.muted]{path}[/]")
        else:
            con.print(f"[neo.muted]already exists: {path}[/]")
        extras = [c for c in info["created"] if c != "settings.toml"]
        if extras:
            con.print(
                "[neo.muted]scaffolded "
                + ", ".join(f".neo/{c}" for c in extras)
                + "[/]"
            )
        ensure = neoconfig.ensure_gitignore(root)
        if ensure == "created":
            con.print(
                "[neo.ok]created .gitignore[/] [neo.muted](ignores "
                "settings.local.toml)[/]"
            )
        elif ensure == "appended":
            con.print("[neo.ok]added .neo/settings.local.toml to .gitignore[/]")
        elif ensure == "present":
            con.print("[neo.muted].gitignore already covers it[/]")
        elif ensure is None and (root / ".git").exists():
            con.print(
                "[neo.warn]could not update .gitignore — add "
                ".neo/settings.local.toml by hand[/]"
            )
        return 0

    ui.err_console().print(f"[neo.error]error: unknown config command {sub!r}[/]")
    return 2


def tier_project_root(tier: str) -> Path:
    """The repo root a project/local-tier write targets: the .neo dir's
    parent when found, else the CWD (the set's write view)."""
    from cli import neoconfig

    pd_ = neoconfig.project_settings_dir()
    return pd_.parent if pd_ is not None else Path.cwd()


def cmd_profile(args: argparse.Namespace) -> int:
    """List, show, select, or remove named provider profiles."""
    from cli import neoconfig

    try:
        if args.profile_command == "list":
            profiles = neoconfig.provider_profiles()
            _profiles, sources = neoconfig.provider_profiles_with_sources()
            active = neoconfig.active_provider_profile()
            if getattr(args, "json", False):
                print(
                    json.dumps(
                        {
                            "active": active.get("name", ""),
                            "active_source": active.get("source", ""),
                            "profiles": {
                                name: {
                                    **neoconfig.public_provider_profiles().get(
                                        name, {}
                                    ),
                                    "source": sources.get(name, "settings"),
                                }
                                for name in profiles
                            },
                        },
                        indent=2,
                    )
                )
                return 0
            if not profiles:
                ui.console().print(
                    "[neo.muted]no provider profiles configured; create one with "
                    "`neo login --profile <name> ...`[/]"
                )
                return 0
            for name in sorted(profiles):
                marker = " (active)" if name == active.get("name") else ""
                ui.console().print(
                    f"  [neo.accent]{name}[/]{marker} "
                    f"[neo.muted]({sources.get(name, 'settings')})[/]"
                )
                shown = neoconfig.public_provider_profiles().get(name, {})
                summary = ", ".join(
                    f"{key}={value}"
                    for key, value in shown.items()
                    if key
                    in ("provider", "model", "base_url", "api_key", "display_label")
                )
                if summary:
                    ui.console().print(f"    [neo.muted]{summary}[/]")
            return 0
        if args.profile_command == "show":
            shown = neoconfig.public_provider_profiles().get(args.name)
            if shown is None:
                raise ValueError(f"no provider profile named {args.name!r}")
            _profiles, sources = neoconfig.provider_profiles_with_sources()
            if getattr(args, "json", False):
                print(
                    json.dumps(
                        {
                            "name": args.name,
                            "source": sources.get(args.name, "settings"),
                            "values": shown,
                        },
                        indent=2,
                    )
                )
                return 0
            ui.console().print(
                f"[neo.accent]{args.name}[/] [neo.muted]({sources.get(args.name, 'settings')})[/]"
            )
            for key, value in shown.items():
                ui.console().print(f"  [neo.muted]{key}:[/] {value}")
            return 0
        if args.profile_command == "use":
            name, path = neoconfig.select_provider_profile(args.name, tier=args.tier)
            if args.tier == "local":
                neoconfig.ensure_gitignore(path.parent.parent)
            ui.console().print(
                f"[neo.ok]selected provider profile {name}[/] "
                f"[neo.muted]({args.tier} -> {path})[/]"
            )
            return 0
        if args.profile_command == "remove":
            name, path = neoconfig.remove_provider_profile(args.name, tier=args.tier)
            ui.console().print(
                f"[neo.ok]removed provider profile {name}[/] [neo.muted]({path})[/]"
            )
            return 0
        ui.err_console().print("[neo.error]unknown profile command[/]")
        return 2
    except (OSError, ValueError) as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2


def _cmd_login(args: argparse.Namespace) -> int:
    """`neo login` — the onboarding wizard on demand."""
    from cli.onboard import cmd_login

    return cmd_login(args)


def _cmd_logout(args: argparse.Namespace) -> int:
    """`neo logout` — strip the stored api_key."""
    from cli.onboard import cmd_logout

    return cmd_logout(args)


# ---------------------------------------------------------------------------
# mcp client (consume EXTERNAL MCP servers — spec item 30)
# ---------------------------------------------------------------------------


def _mcp_repo_path(args: argparse.Namespace) -> Optional[str]:
    """Choose the repository context for connector label resolution."""
    repo = getattr(args, "repo", None)
    if repo:
        return str(Path(repo).expanduser())
    cwd = getattr(args, "cwd", None)
    if cwd:
        return str(Path(cwd).expanduser())
    return None


def cmd_mcp_list_tools(args: argparse.Namespace) -> int:
    """List tools from a configured connector or raw launch command."""
    from cli import connectors as connectors_mod

    out = connectors_mod.list_tools(
        args.server,
        repo_path=_mcp_repo_path(args),
        cwd=args.cwd,
        timeout_s=float(getattr(args, "timeout", 60.0)),
    )
    if not out.get("ok"):
        ui.err_console().print(
            f"[neo.error]error: {ui.sanitize_text(out.get('error'))}[/]"
        )
        return 1
    tools = out.get("tools", [])
    con = ui.console()
    con.print(f"[neo.accent]{len(tools)} tool(s)[/] [neo.muted]on {args.server!r}:[/]")
    for tool in tools:
        desc = (
            f" — {ui.sanitize_text(tool['description'])}"
            if isinstance(tool, dict) and tool.get("description")
            else ""
        )
        name = tool.get("name", "?") if isinstance(tool, dict) else "?"
        con.print(f"  [neo.accent]{ui.sanitize_text(name)}[/][neo.muted]{desc}[/]")
    return 0


def cmd_mcp_call(args: argparse.Namespace) -> int:
    """Call one tool on a configured connector or raw MCP command."""
    from cli import connectors as connectors_mod

    tool_args: Dict[str, Any] = {}
    if args.args_json:
        try:
            tool_args = json.loads(args.args_json)
        except ValueError as exc:
            ui.err_console().print(
                f"[neo.error]error: --args is not valid JSON: {exc}[/]"
            )
            return 2
        if not isinstance(tool_args, dict):
            ui.err_console().print("[neo.error]error: --args must be a JSON object[/]")
            return 2
    out = connectors_mod.call_tool(
        args.server,
        args.tool,
        tool_args,
        repo_path=_mcp_repo_path(args),
        cwd=args.cwd,
        timeout_s=float(getattr(args, "timeout", 60.0)),
    )
    if not out.get("ok"):
        ui.err_console().print(
            f"[neo.error]error: {ui.sanitize_text(out.get('error'))}[/]"
        )
        return 1
    ui.console().print(ui.sanitize_text(out.get("text", "")))
    return 0


# ---------------------------------------------------------------------------
# mcp registry (neo mcp add/remove/list/health — the unified connectors)
# ---------------------------------------------------------------------------


def cmd_mcp(args: argparse.Namespace) -> int:
    """Dispatch neo mcp add/remove/list/health (the connectors registry).

    Exposed as one callable for NIGHT-B's /mcp surface (which consumes
    these helpers without touching the slash table): add/remove/list
    go through cli.connectors; health spawns each server. All
    ConnectorErrors render cleanly + exit 2; health never tracebacks
    (per-server ok/fail lines + exit 1 when any fail).
    """
    from cli import connectors as connectors_mod

    try:
        if args.mcp_command == "add":
            tier = getattr(args, "tier", "global")
            repo_path = _mcp_repo_path(args)
            raw_parts = list(getattr(args, "command", []) or [])
            cmd_parts: List[str] = []
            index = 0
            while index < len(raw_parts):
                part = raw_parts[index]
                if part == "--":
                    cmd_parts.extend(raw_parts[index + 1 :])
                    break
                if part in ("--tier", "--repo") and index + 1 < len(raw_parts):
                    value = raw_parts[index + 1]
                    if part == "--tier":
                        if value not in ("global", "project", "local"):
                            raise connectors_mod.ConnectorError(
                                f"unknown connector tier: {value!r}"
                            )
                        tier = value
                    else:
                        repo_path = value
                    index += 2
                    continue
                cmd_parts.append(part)
                index += 1
            if not cmd_parts:
                ui.err_console().print(
                    "[neo.error]error: `neo mcp add <label> -- <cmd...>` "
                    "needs a command after `--`[/]"
                )
                return 2
            import shlex

            command = " ".join(shlex.quote(part) for part in cmd_parts)
            label = connectors_mod.add_server(
                args.label,
                command,
                tier=tier,
                repo_path=repo_path,
            )
            ui.console().print(
                f"[neo.ok]added MCP server {label}[/] "
                f"[neo.muted]({tier} settings; "
                f"{connectors_mod.mask_command(command)})[/]"
            )
            return 0
        if args.mcp_command == "remove":
            label = connectors_mod.remove_server(
                args.label,
                tier=getattr(args, "tier", "global"),
                repo_path=_mcp_repo_path(args),
            )
            ui.console().print(
                f"[neo.ok]removed MCP server {label}[/] "
                f"[neo.muted]({getattr(args, 'tier', 'global')} tier)[/]"
            )
            return 0
        if args.mcp_command == "list":
            servers = connectors_mod.list_servers(_mcp_repo_path(args))
            if not servers:
                ui.console().print(
                    "[neo.muted]no MCP servers configured "
                    "(neo mcp add <label> -- <cmd...>)[/]"
                )
                return 0
            ui.console().print(
                f"[neo.accent]{len(servers)} MCP server(s)[/] "
                "[neo.muted](plugin < global < project < local; labels resolve in "
                "list-tools/call)[/]"
            )
            for s in servers:
                ui.console().print(
                    f"  [neo.accent]{s['label']}[/] "
                    f"[neo.muted]({s['source']}) {s['command']}[/]"
                )
            return 0
        if args.mcp_command == "health":
            results = connectors_mod.check_health(
                repo_path=_mcp_repo_path(args),
                timeout_s=float(getattr(args, "timeout", 60.0)),
            )
            if not results:
                ui.console().print(
                    "[neo.muted]no MCP servers configured "
                    "(neo mcp add <label> -- <cmd...>; plugins may also "
                    "provide servers)[/]"
                )
                return 0
            failed = 0
            for r in results:
                if r.get("ok"):
                    n = len(r.get("tools") or [])
                    ui.console().print(
                        f"  [neo.ok]ok[/] [neo.accent]{r['label']}[/] "
                        f"[neo.muted]({r['source']}) {n} tool(s)[/]"
                    )
                else:
                    failed += 1

                    ui.console().print(
                        f"  [neo.error]fail[/] [neo.accent]{r['label']}[/] "
                        f"[neo.muted]({r['source']}) "
                        f"{ui.sanitize_text(r.get('error'))}[/]"
                    )
            return 1 if failed else 0
        ui.err_console().print("[neo.error]unknown mcp command[/]")
        return 2
    except connectors_mod.ConnectorError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2


# ---------------------------------------------------------------------------
# Extension and operations surfaces (R2-16)
# ---------------------------------------------------------------------------


def cmd_mcp_permissions(args: argparse.Namespace) -> int:
    """Declare, narrow, show, or clear a connector's permission set.

    A connector's blast radius used to be undeclared: the only statement about
    it was a launch command. This is the surface that makes it a statement, and
    the enforcement that reads it lives in ``cli.connectors.call_tool``.
    """
    from cli import connectors as connectors_mod

    tier = getattr(args, "tier", "project")
    repo_path = _mcp_repo_path(args)
    label = getattr(args, "label", None)
    as_json = bool(getattr(args, "json", False))
    try:
        if not label:
            declared = connectors_mod.read_permissions(repo_path)
            if as_json:
                print(
                    json.dumps(
                        {
                            label_name: permission.to_dict()
                            for label_name, permission in sorted(declared.items())
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            con = ui.console()
            if not declared:
                con.print(
                    "[neo.muted]no connector declares permissions yet "
                    "(every connector can call every tool its server offers)[/]"
                )
                return 0
            con.print(f"[neo.accent]{len(declared)} declared connector(s)[/]")
            for name, permission in sorted(declared.items()):
                tools = (
                    "any tool the server offers"
                    if permission.allows_any_tool
                    else (", ".join(permission.tools) or "no tools (all refused)")
                )
                con.print(
                    f"  [neo.accent]{name}[/] [neo.muted]({permission.tier})[/] "
                    f"tools: {tools}; side effect <= {permission.side_effect}; "
                    f"network: {', '.join(permission.network) or 'none declared'}; "
                    f"write: "
                    + (
                        "declared yes"
                        if permission.write
                        else (
                            "declared no" if permission.write is False else "UNDECLARED"
                        )
                    )
                )
            return 0
        if getattr(args, "clear", False):
            removed = connectors_mod.clear_permissions(
                label, tier=tier, repo_path=repo_path
            )
            ui.console().print(
                f"[neo.ok]cleared the declared permissions for {removed}[/]"
            )
            return 0
        wrote = any(
            getattr(args, key, None) is not None
            for key in ("tools", "side_effect", "network", "write")
        )
        if not wrote:
            permission = connectors_mod.connector_permissions(
                label, repo_path=repo_path
            )
            if permission is None:
                ui.err_console().print(
                    f"[neo.error]error: connector {label!r} has no declared permissions "
                    "(its tool list is unrestricted)[/]"
                )
                return 2
            if as_json:
                print(json.dumps(permission.to_dict(), indent=2, sort_keys=True))
                return 0
            ui.console().print(
                f"[neo.accent]{label}[/] [neo.muted]({permission.tier})[/] "
                f"tools: "
                + (
                    "any tool the server offers"
                    if permission.allows_any_tool
                    else (", ".join(permission.tools) or "none (all refused)")
                )
                + f"; side effect <= {permission.side_effect}; "
                f"network: {', '.join(permission.network) or 'none declared'}; "
                "write: "
                + (
                    "declared yes"
                    if permission.write
                    else ("declared no" if permission.write is False else "UNDECLARED")
                )
            )
            if permission.write is None:
                ui.console().print(
                    "[neo.muted]  write capability is UNDECLARED; the gate is off. "
                    "Pass --no-write or --write to state it.[/]"
                )
            return 0
        permission = connectors_mod.set_permissions(
            label,
            tier=tier,
            repo_path=repo_path,
            tools=tuple(getattr(args, "tools", None) or ()) or None,
            side_effect=getattr(args, "side_effect", None),
            network=tuple(getattr(args, "network", None) or ()) or None,
            write=getattr(args, "write", None),
        )
        if as_json:
            print(json.dumps(permission.to_dict(), indent=2, sort_keys=True))
            return 0
        ui.console().print(
            f"[neo.ok]declared permissions for {permission.label}[/] "
            f"[neo.muted]({permission.tier})[/]"
        )
        ui.console().print(
            "[neo.muted]  tools: "
            + (
                "any tool the server offers"
                if permission.allows_any_tool
                else (", ".join(permission.tools) or "none (all refused)")
            )
            + f"; side effect <= {permission.side_effect}; "
            f"network: {', '.join(permission.network) or 'none declared'}; "
            "write: "
            + (
                "declared yes"
                if permission.write
                else ("declared no" if permission.write is False else "UNDECLARED")
            )
        )
        return 0
    except connectors_mod.ConnectorError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2


def cmd_doctor(args: argparse.Namespace) -> int:
    """`neo doctor` — the first-class operator surface over the same checks.

    Exit code carries the verdict: 0 when nothing is actionable, 1 when at
    least one check is failed/errored/skipped, 2 for a usage error. A doctor
    that always exits 0 cannot be used as a CI gate, and one that exits 1 for
    an unconfigured provider is a doctor nobody runs.
    """
    from cli import doctor as doctor_mod

    record = doctor_mod.doctor_record(
        repo_path=getattr(args, "repo", None),
        log_root=getattr(args, "log_root", None),
    )
    if getattr(args, "json", False):
        print(doctor_mod.render_doctor_json(record))
    else:
        ui.console().print(doctor_mod.render_doctor_human(record))
    summary = record.get("summary") or {}
    return 1 if int(summary.get("actionable", 0) or 0) else 0


def cmd_support_bundle(args: argparse.Namespace) -> int:
    """`neo support-bundle` — one archive a user can attach to an issue.

    Every value in the bundle has already been through the shared redactor, and
    the manifest records that fact, so the archive is safe to hand to a public
    issue tracker. Nothing here reads a credential: environment VALUES are
    deliberately not recorded and a settings value is either masked through the
    existing public-value authority or reported as ``withheld``.
    """
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone

    from cli import doctor as doctor_mod

    out = getattr(args, "out", None)
    if not out:
        stamp = _datetime.now(_timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = f"neo-support-{stamp}.zip"
    bundle = doctor_mod.build_support_bundle(
        repo_path=getattr(args, "repo", None),
        log_root=getattr(args, "log_root", None),
        recent_runs=max(1, int(getattr(args, "recent_runs", 5) or 5)),
    )
    written = doctor_mod.write_support_bundle(
        bundle, str(out), archive=not bool(getattr(args, "no_archive", False))
    )
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    **bundle.to_dict(),
                    "archive": written,
                    "files_written": sorted(bundle.files),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    con = ui.console()
    con.print(f"[neo.ok]support bundle written[/] [neo.muted]to {written}[/]")
    con.print(
        f"[neo.muted]  {len(bundle.files)} section(s); every value passed through "
        f"{bundle.redaction}[/]"
    )
    for name in sorted(bundle.files):
        con.print(f"[neo.muted]    {name}[/]")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """`neo migrate` — detect, plan, and (on request) apply atomically.

    Without ``--apply`` this writes NOTHING and prints the exact plan, which is
    the default because a tool that migrates a machine on the strength of a
    typed word is a tool nobody trusts with a production checkout. With
    ``--apply`` the whole plan runs as one transaction: any failure restores
    everything this run changed and exits 2.
    """
    from cli import migration as migration_mod

    apply_now = bool(getattr(args, "apply", False))
    only = tuple(getattr(args, "only", None) or ())
    known = set(migration_mod.MIGRATION_IDS)
    unknown = [name for name in only if name not in known]
    if unknown:
        ui.err_console().print(
            f"[neo.error]error: unknown migration id(s) {unknown}; "
            f"known ids are {sorted(known)}[/]"
        )
        return 2
    result = migration_mod.migrate(
        repo_path=getattr(args, "repo", None),
        log_root=getattr(args, "log_root", None),
        apply=apply_now,
        allow_destructive=bool(getattr(args, "allow_destructive", False)),
        only=only,
    )
    as_json = bool(getattr(args, "json", False))
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 2 if (result.error or result.rolled_back) else 0
    con = ui.console()
    _print_migration_plan(con, result.plan)
    if not apply_now:
        if result.plan.pending():
            con.print(
                "[neo.muted]nothing was changed. Re-run with --apply to take this "
                "plan.[/]"
            )
        return 0
    if result.rolled_back:
        ui.err_console().print(
            f"[neo.error]migration failed and was ROLLED BACK: {result.error}[/]"
        )
        for message in result.rollback_errors:
            ui.err_console().print(f"[neo.error]  {message}[/]")
        return 2
    if result.error:
        ui.err_console().print(f"[neo.error]migration error: {result.error}[/]")
        return 2
    for outcome in result.outcomes:
        if outcome.applied:
            con.print(
                f"[neo.ok]applied[/] [neo.accent]{outcome.id}[/] "
                f"[neo.muted]({outcome.actions_applied} action(s))[/]"
            )
        else:
            con.print(
                f"[neo.muted]skipped[/] [neo.accent]{outcome.id}[/] "
                f"[neo.muted]{outcome.detail}[/]"
            )
    if result.receipt_path:
        con.print(f"[neo.muted]receipt: {result.receipt_path}[/]")
    con.print("[neo.muted]re-run `neo migrate` to confirm the install is current.[/]")
    return 0


def _print_migration_plan(con: Any, plan: Any) -> int:
    """Print the plan and return the pending-step count."""
    if plan.already_current:
        con.print(
            "[neo.ok]already current[/] [neo.muted]— nothing to migrate; this install "
            "matches the layout this version expects[/]"
        )
    else:
        con.print(
            f"[neo.accent]{len(plan.pending())} migration step(s) apply[/] "
            f"[neo.muted]({plan.action_count} action(s))[/]"
        )
    for step in plan.steps:
        if step.applies:
            marker = "destructive" if step.destructive else "reversible"
            con.print(f"  [neo.accent]• {step.id}[/] [neo.muted]({marker})[/]")
            con.print(f"    [neo.muted]{step.reason}[/]")
            for action in step.actions:
                flag = " [neo.error](destructive)[/]" if action.destructive else ""
                con.print(f"      [neo.muted]{action.kind}[/] {action.path}{flag}")
        else:
            con.print(f"  [neo.muted]· {step.id}: {step.reason}[/]")
    if plan.over_budget:
        ui.err_console().print(
            f"[neo.error]error: the plan has {plan.action_count} actions, above the "
            f"{migration_budget()} per-run budget; nothing was applied[/]"
        )
    return len(plan.pending())


def migration_budget() -> int:
    """Return the per-run action budget (indirection keeps the printer testable)."""
    from cli import migration as migration_mod

    return int(migration_mod.MAX_ACTIONS_PER_RUN)


def cmd_hooks(args: argparse.Namespace) -> int:
    """`neo hooks` — the declarative hook layer's verbs.

    ``list`` prints the merged config, the whole event vocabulary with each
    event's declared class and fail policy, and whether each hook is trusted
    against the code on disk. ``run <Event>`` fires one event through the same
    gate the connector surface uses. ``test --id <hook>`` runs ONE hook against
    a synthetic event and shows its real output. ``trust`` records the user's
    decision about a hook. ``reload`` re-registers without a restart.

    Every verb is delegated to
    :func:`extensions.user_hooks.hooks_command` — the SAME function the ``/hooks``
    slash command calls — so there is one implementation of each behaviour rather
    than one per surface. This function only maps argparse attributes onto the
    verb's own flags and prints.
    """
    from extensions import user_hooks as hooks_mod

    repo_path = getattr(args, "repo", None)
    as_json = bool(getattr(args, "json", False))
    command = getattr(args, "hooks_command", "list")

    # The verb's own flags, mapped to the vocabulary the shared implementation
    # reads. argparse already rejected an unknown option, so anything missing
    # here is an option this subcommand simply does not have.
    argv: List[str] = [command]
    flag_map = (
        (getattr(args, "event", None), "--event"),
        (getattr(args, "id", None), "--id"),
        (getattr(args, "hook_id", None), "--id"),
        (getattr(args, "tool", None), "--tool"),
        (getattr(args, "path", None), "--path"),
        (getattr(args, "command", None), "--command"),
        (getattr(args, "mcp_server", None), "--mcp-server"),
        (getattr(args, "side_effect", None), "--side-effect"),
        (getattr(args, "decision", None), "--decision"),
        (getattr(args, "note", None), "--note"),
    )
    for value, flag in flag_map:
        if value is None:
            continue
        text = str(value)
        if not text:
            continue
        # `hooks run` takes its event positionally; every other verb takes it as
        # `--event`, so the positional form is only added for `run`.
        if flag == "--event" and command != "run":
            argv.extend([flag, text])
        elif flag != "--event":
            argv.extend([flag, text])
        else:
            argv.append(text)
    if repo_path and command != "reload":
        argv.extend(["--repo", str(repo_path)])

    if as_json:
        # JSON mode prints ONE document and nothing else, so a script never has
        # to pick prose out of a stream.
        def _quiet(_line: str) -> None:
            return None

        result = hooks_mod.hooks_command(argv, repo_path=repo_path, write=_quiet)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
        return result.exit_code

    con = ui.console()

    def _write(line: str) -> None:
        # Every line is already escaped by `hooks_command`, so a hook id, a
        # matcher or a hook's own output cannot be interpreted as markup. A
        # render failure must never delete a message.
        con.print(line)

    try:
        result = hooks_mod.hooks_command(argv, repo_path=repo_path, write=_write)
    except hooks_mod.HookConfigError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2
    return result.exit_code


# ---------------------------------------------------------------------------
# skills (neo skills list/show — read-only view over the skill scan)
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
        from cli import plugins as plugins_mod

        return [
            {
                "name": str(item.get("name") or ""),
                "origin": str(item.get("origin") or "unknown"),
                "description": str(item.get("description") or ""),
                "source": str(item.get("source") or ""),
                "enabled": bool(item.get("enabled", False)),
                "active": bool(item.get("active", False)),
            }
            for item in plugins_mod.list_skill_installs(repo_path)
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
    """Install, list, show, remove, enable, or disable standalone skills."""
    from cli import plugins as plugins_mod

    repo = getattr(args, "repo", None)
    tier = getattr(args, "tier", "global") or "global"
    try:
        if args.skills_command == "install":
            name = plugins_mod.install_skill(
                args.source,
                tier=tier,
                repo_path=repo,
                name_override=getattr(args, "name", None),
            )
            ui.console().print(
                f"[neo.ok]installed skill {name}[/] [neo.muted]({tier} tier)[/]"
            )
            return 0
        if args.skills_command == "remove":
            name = plugins_mod.remove_skill(args.name, tier=tier, repo_path=repo)
            ui.console().print(
                f"[neo.ok]removed skill {name}[/] [neo.muted]({tier} tier)[/]"
            )
            return 0
        if args.skills_command == "enable":
            name = plugins_mod.enable_skill(args.name, tier=tier, repo_path=repo)
            ui.console().print(
                f"[neo.ok]enabled skill {name}[/] [neo.muted]({tier} tier)[/]"
            )
            return 0
        if args.skills_command == "disable":
            name = plugins_mod.disable_skill(args.name, tier=tier, repo_path=repo)
            ui.console().print(
                f"[neo.ok]disabled skill {name}[/] [neo.muted](body stays installed)[/]"
            )
            return 0
        if args.skills_command == "list":
            skills = list_skills_for_cli(repo)
            if not skills:
                ui.console().print(
                    "[neo.muted]no skills installed "
                    "(.neo/skills/, global skills, or a plugin)[/]"
                )
                return 0
            for skill in skills:
                head = (skill["description"] or "").strip().splitlines()
                desc = head[0][:100] if head else "(no description)"
                state = "" if skill.get("enabled", True) else " (disabled)"
                if skill.get("enabled", True) and not skill.get("active", True):
                    state = " (shadowed)"
                source = skill.get("source") or "unknown"
                ui.console().print(
                    f"  [neo.accent]{skill['name']}[/] "
                    f"[neo.muted]({skill['origin']}; {source}){state} {desc}[/]"
                )
            return 0
        if args.skills_command == "show":
            found = show_skill_for_cli(args.name, repo)
            if found is None:
                ui.err_console().print(
                    f"[neo.error]error: no enabled skill named {args.name!r}[/]"
                )
                return 2
            ui.console().print(
                f"[neo.accent]{found['name']}[/] [neo.muted]({found['origin']}; {found['source']})[/]"
            )
            if found.get("description"):
                ui.console().print(f"[neo.muted]{found['description']}[/]")
            ui.console().print(found["body"])
            return 0
        ui.err_console().print("[neo.error]unknown skills command[/]")
        return 2
    except plugins_mod.SkillError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2


# ---------------------------------------------------------------------------
# plugin subcommands (Plugins round, Task C)
# ---------------------------------------------------------------------------


def cmd_plugin(args: argparse.Namespace) -> int:
    """Dispatch neo plugin install/list/remove/enable/disable."""
    from cli import plugins as plugins_mod

    try:
        if args.plugin_command == "install":
            name = plugins_mod.install(args.source, name_override=args.name)
            con = ui.console()
            con.print(f"[neo.ok]installed plugin {name}[/]")
            entry = next(
                (e for e in plugins_mod.list_plugins() if e.get("name") == name),
                {},
            )
            con.print(f"[neo.muted]dir:      {entry.get('dir', '?')}[/]")
            skills = entry.get("skills_on_disk") or []
            commands = entry.get("commands_on_disk") or []
            tools = (entry.get("tools") or {}).get("verbs") or []
            mcp = entry.get("mcp_servers") or {}
            con.print(
                f"[neo.muted]skills:   {len(skills)}"
                f"{' — ' + ', '.join(skills) if skills else ''}[/]"
            )
            con.print(
                f"[neo.muted]commands: {len(commands)}"
                f"{' — ' + ', '.join('/' + c for c in commands) if commands else ''}[/]"
            )
            if tools:
                con.print(f"[neo.muted]tools:    BATCH verbs: {', '.join(tools)}[/]")
            if mcp:
                from cli.connectors import mask_command

                con.print(
                    "[neo.muted]mcp:      "
                    + ", ".join(f"{k} ({mask_command(str(v))})" for k, v in mcp.items())
                    + "[/]"
                )
            return 0
        if args.plugin_command == "list":
            con = ui.console()
            plugins = plugins_mod.list_plugins()
            if not plugins:
                con.print(
                    f"[neo.muted]no plugins installed under "
                    f"{plugins_mod.plugins_root().resolve()}[/]"
                )
                return 0
            con.print(
                f"[neo.accent]{len(plugins)} plugin(s) installed[/] "
                f"[neo.muted](source: install from a local path or git URL)[/]"
            )
            for p in plugins:
                if p.get("error"):
                    tag = " (disabled)" if p.get("enabled") is False else ""
                    con.print(
                        f"  [neo.error]{p['name']}[/] [neo.muted]— broken: "
                        f"{p['error']}{tag}[/]"
                    )
                    continue
                desc = f" — {p['description']}" if p.get("description") else ""
                # "(disabled)" in parens: a bracketed [disabled] would parse
                # as a rich style tag and vanish from the rendered line.
                state = "" if p.get("enabled", True) else " (disabled)"
                con.print(
                    f"  [neo.accent]{p['name']}[/][neo.muted]{desc}{state} "
                    f"(origin: {p.get('origin', 'plugin')})[/]"
                )
                con.print(
                    f"[neo.muted]    {len(p.get('skills_on_disk') or [])} "
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
        if args.plugin_command in ("show", "inspect"):
            entry = plugins_mod.inspect_plugin(args.name)
            if entry is None:
                raise plugins_mod.PluginError(
                    f"no installed plugin named {args.name!r}"
                )
            con = ui.console()
            con.print(
                f"[neo.accent]{entry.get('name', args.name)}[/] "
                f"[neo.muted](origin: {entry.get('origin', 'plugin')}; "
                f"{'enabled' if entry.get('enabled', True) else 'disabled'})[/]"
            )
            con.print(f"[neo.muted]path: {entry.get('dir', '?')}[/]")
            if entry.get("description"):
                con.print(f"[neo.muted]{entry['description']}[/]")
            con.print(
                "[neo.muted]skills: "
                + ", ".join(entry.get("skills_on_disk") or entry.get("skills") or [])
                + "[/]"
            )
            con.print(
                "[neo.muted]commands: "
                + ", ".join(
                    "/" + str(name)
                    for name in (
                        entry.get("commands_on_disk") or entry.get("commands") or []
                    )
                )
                + "[/]"
            )
            if entry.get("error"):
                con.print(f"[neo.error]{entry['error']}[/]")
                return 2
            return 0
        if args.plugin_command == "remove":
            name = plugins_mod.remove(args.name)
            ui.console().print(f"[neo.ok]removed plugin {name}[/]")
            return 0
        if args.plugin_command == "enable":
            name = plugins_mod.enable(args.name)
            ui.console().print(f"[neo.ok]enabled plugin {name}[/]")
            return 0
        if args.plugin_command == "disable":
            name = plugins_mod.disable(args.name)
            ui.console().print(
                f"[neo.ok]disabled plugin {name}[/] "
                "[neo.muted](stays installed; skills/commands/tools/mcp "
                "skipped until re-enabled)[/]"
            )
            return 0
        ui.err_console().print("[neo.error]unknown plugin command[/]")
        return 2
    except plugins_mod.PluginError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2


# ---------------------------------------------------------------------------
# run (headless surface for the shared slash-command contract)
# ---------------------------------------------------------------------------


def cmd_run_command(args: argparse.Namespace) -> int:
    """Run one slash command headlessly with a stable exit code.

    The same registry the TUI palette and the REPL dispatch against decides
    whether the command is available here, what arguments it takes, and
    which recovery actions apply. Commands needing a live run or an
    interactive prompt refuse with exit 2 instead of pretending; commands a
    CLI flag already owns point at that flag. Exit codes come from
    cli.exit_codes (0 ok, 2 usage/refusal), never a raw traceback.
    """
    from cli.command_exec import run_command_line
    from cli.exit_codes import EXIT_CODES

    repo = Path(args.repo) if args.repo else Path.cwd()
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    # REMAINDER arrives as a list: quoted "/diff undo" is one element,
    # unquoted /steer focus the tests is several. Joining restores the one
    # command line the shared registry parses.
    line = (
        " ".join(args.command).strip()
        if isinstance(args.command, list)
        else str(args.command)
    )
    if getattr(args, "at", None):
        return _cmd_schedule_run(
            line,
            at=args.at,
            repo=repo,
            log_root=log_root,
            schedule_id=getattr(args, "schedule_id", None),
            interval_s=getattr(args, "schedule_every", 0.0),
            max_runs=getattr(args, "schedule_max_runs", 32),
            as_json=bool(args.json),
        )
    if not line:
        ui.err_console().print(
            '[neo.error]error: a command line is required, e.g. neo run "/help"[/]'
        )
        return EXIT_CODES["usage_error"]
    result = run_command_line(
        line, log_root=log_root, repo=repo if repo.is_dir() else None
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    elif result.text:
        print(result.text)
    return result.exit_code if result.exit_code else EXIT_CODES["success"]


def _cmd_schedule_run(
    line: str,
    *,
    at: str,
    repo: Path,
    log_root: Path,
    schedule_id: Optional[str],
    interval_s: float,
    max_runs: int,
    as_json: bool,
) -> int:
    """Register ``neo run --at``: a bounded, logged, revertible schedule.

    This registers a REQUEST and returns; it does not run the command and it
    does not start a worker. ``runtime.schedules`` owns validation, the
    approval/budget clamps, the journal, and the revert path — this function
    is only the CLI surface. A schedule cannot weaken an inherited approval
    policy, can only tighten a budget bound, and never carries credentials.
    """
    from cli.exit_codes import EXIT_CODES
    from runtime.schedules import ScheduleError, ScheduleRegistry

    if not line.strip():
        ui.err_console().print(
            "[neo.error]error: --at needs a command line to schedule, "
            'e.g. neo run --at +30m "/status"[/]'
        )
        return EXIT_CODES["usage_error"]
    if not repo.is_dir():
        ui.err_console().print(f"[neo.error]error: not a directory: {repo}[/]")
        return EXIT_CODES["usage_error"]
    name = (schedule_id or "").strip() or "run-" + hashlib.sha256(
        line.encode("utf-8", "replace")
    ).hexdigest()[:12]
    try:
        registry = ScheduleRegistry(log_root)
        receipt = registry.register(
            name,
            repo=str(repo.resolve()),
            issue=line,
            at=at,
            config={"trigger": "cli"},
            interval_s=interval_s,
            source="neo run --at",
            trigger="cli",
            max_runs=max_runs,
        )
    except ScheduleError as exc:
        if as_json:
            print(
                json.dumps(
                    {"error": str(exc), "exit_code": EXIT_CODES["usage_error"]},
                    indent=2,
                )
            )
        else:
            ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return EXIT_CODES["usage_error"]
    payload = receipt.as_dict()
    payload["command_line"] = line
    payload["log_root"] = str(log_root)
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(receipt.at))
        print(f"scheduled {name} at {when} — list: python -m runtime.schedules list")
        print("revert:   python -m runtime.schedules revert " + name)
    return EXIT_CODES["success"]


# ---------------------------------------------------------------------------
# dashboard (read-only web view of existing logs)
# ---------------------------------------------------------------------------


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the read-only dashboard over existing logs (blocks)."""
    try:
        from dashboard.server import serve
    except ImportError as exc:
        ui.err_console().print(
            f"[neo.error]error: dashboard module unavailable: {exc}[/]"
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
    (`neo fix --finding <scan_id>#<n>`) are the deliverables.
    """
    from harness.scan_mode import run_scan

    con = ui.console()
    as_json = bool(getattr(args, "json", False))
    if getattr(args, "fix", None) is not None and as_json:
        ui.err_console().print(
            "[neo.error]error: --fix and --json are mutually exclusive[/]"
        )
        return 2
    repo = Path(args.repo)
    if not repo.is_dir():
        ui.err_console().print(
            f"[neo.error]error: --repo is not a directory: {args.repo}[/]"
        )
        return 2
    remote = bool(getattr(args, "remote", False))
    focus = getattr(args, "focus", None) or None
    if focus not in (None, "coverage", "smells", "dependencies"):
        ui.err_console().print(
            "[neo.error]error: --focus must be coverage|smells|dependencies[/]"
        )
        return 2
    log_root = Path(args.log_root) if args.log_root else default_logs_dir()
    os.environ.setdefault("NEO_TRACE_DIR", str(log_root))
    as_json = bool(getattr(args, "json", False))
    if not as_json:
        con.print(
            f"[neo.accent]neo scan[/] [neo.muted]"
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
            f"[neo.error]error: scan failed: {scan['notes'][0] if scan['notes'] else 'unknown'}[/]"
        )
        return 1
    elapsed = time.time() - started
    if as_json:
        # --json: one JSON document on stdout, nothing else (the same
        # contract as `neo fix --json`).
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

    sev_style = {"high": "neo.error", "medium": "neo.warn", "low": "neo.muted"}
    for f in shown:
        loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
        con.print()
        con.print(
            f"[neo.accent2]{f['index']}.[/] "
            f"[{sev_style[f['severity']]}]{f['severity']}[/] "
            f"[neo.muted]{f['kind']}[/] {f['title']}"
        )
        con.print(f"[neo.muted]  location: {loc}[/]")
        if f.get("evidence"):
            con.print(f"[neo.muted]  evidence: {f['evidence']}[/]")
        for line in f["rationale"].split("\n"):
            if line.strip():
                con.print(f"  {line}")
        con.print(
            f"[neo.muted]  fix as a task:[/] "
            f"neo fix --finding {scan['scan_id']}#{f['index']}"
        )
    if not shown:
        con.print("[neo.ok]no findings worth attention[/]")
    if scan["suppressed"]:
        con.print(
            f"[neo.muted]({scan['suppressed']} lower-value finding(s) suppressed — "
            f"--max-findings to resurface; full list in {scan['scan_path']})[/]"
        )
    for n in scan["notes"]:
        con.print(f"[neo.muted]note: {n}[/]")
    con.print()
    counts = scan["counts"].get("by_kind") or {}
    con.print(
        f"[neo.muted]scan {scan['scan_id']} · {len(scan['findings'])} finding(s)"
        f" · {scan['shown']} shown · {scan['suppressed']} suppressed"
        f"{' · ' + ' / '.join(f'{k} {v}' for k, v in sorted(counts.items())) if counts else ''}"
        f" · {elapsed:.1f}s[/]"
    )
    if scan["report_path"]:
        con.print(f"[neo.muted]report: {scan['report_path']}[/]")
    return 0


def _run_finding(args: argparse.Namespace, con) -> int:
    """Shared driver for `neo fix --finding <id>#<n>` (Task C).

    Resolves the finding from its scan.json, then dispatches through
    the existing verifier-gated entries: fix_kind "fix" -> the plain
    fix loop (cmd_fix with the finding's issue/target); "build" ->
    build mode (a version-floor acceptance test genuinely fails on the
    old state). Returns a process exit code.
    """
    from harness.scan_mode import finding_task_params, resolve_finding

    log_root = _artifact_log_root(
        getattr(args, "log_root", None), getattr(args, "repo", None)
    )
    scan, finding, err = resolve_finding(args.finding, log_root)
    if finding is None:
        ui.err_console().print(f"[neo.error]error: {err}[/]")
        return 2
    params = finding_task_params(finding)
    con.print()
    con.print(
        f"[neo.accent]neo fix --finding[/] [neo.muted]"
        f"{args.finding} {ui.GLYPHS['arrow']} {finding['title']}[/]"
    )
    if params["note"]:
        con.print(f"[neo.muted]{params['note']}[/]")
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
    from cli.neoconfig import apply_config_defaults, normalize_runtime_keys

    cfg = normalize_runtime_keys(
        apply_config_defaults(cfg, start=Path(getattr(args, "repo", ".")))
    )
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
            "[neo.warn]already passing — the acceptance test passed on "
            "the current repo; nothing to build[/]"
        )
        return 0
    result = out.get("result")
    if result is not None:
        _print_result(result, elapsed)
    rc = 0 if status == "success" else 1
    if status == "error":
        ui.err_console().print(
            f"[neo.error]build failed: {out.get('note', 'internal error')}[/]"
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
            f"[neo.error]error: analyze-history module unavailable: {exc}[/]"
        )
        return 2
    logs_root = Path(args.log_root or "logs")
    if not logs_root.is_dir():
        ui.err_console().print(f"[neo.error]error: logs root not found: {logs_root}[/]")
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
        f"analyzed [neo.accent2]{rep['n_real_tasks']}[/] real tasks "
        f"([neo.accent2]{rep['n_routed_tasks']}[/] adaptively routed)"
    )
    agg = rep["aggregate"]
    div = agg["predictor_divergence"]
    con.print()
    con.print("[neo.running]predictor divergence (routed tasks)[/]")
    con.print(
        f"  aligned: {div['aligned']['n']}   "
        f"false escalations: {div['false_escalation']['n']}   "
        f"missed escalations: {div['missed_escalation']['n']}"
    )
    strat = agg["retrieval_vs_repairs"]
    con.print()
    con.print("[neo.running]retrieval strategy vs repair attempts[/]")
    for k, v in strat.items():
        con.print(
            f"  {k}: n={v['n']} mean_attempts={v['mean_attempts']} "
            f"mean_repairs={v['mean_repairs']}"
        )
    fails = agg["failure_patterns"]
    con.print()
    con.print("[neo.running]failure patterns[/]")
    for k, v in fails.items():
        con.print(f"  {k}: {v['n']}")
    cal = rep["calibration"]
    con.print()
    if cal.get("status") == "ok":
        con.print("[neo.running]difficulty predictor recalibration[/]")
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
        con.print(f"  recommendation: [neo.warn]{rep['recommendation']}[/]")
    else:
        con.print(
            "[neo.warn]calibration: insufficient routed data "
            f"({cal.get('n', 0)} usable rows)[/]"
        )
    if args.apply:
        written = apply_recommendation(rep)
        if written:
            con.print(f"calibration applied: [neo.ok]{written}[/]")
        else:
            con.print(
                "[neo.warn]--apply: recommendation is not 'apply' - nothing written[/]"
            )
    con.print()
    con.print(f"[neo.muted]report: {rep.get('_report_path')}[/]")
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
    parser.add_argument(
        "--profile",
        help="named provider profile (overrides settings and provider env defaults)",
    )
    parser.add_argument("--api-key", help="API key (or env; prefer env)")
    parser.add_argument(
        "--api-base",
        "--base-url",
        dest="api_base",
        help="custom endpoint base URL (BYO router/gateway; also "
        "`neo config set base_url ...` or NEO_BASE_URL)",
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


#: Distribution names this project CONSIDERED for PyPI and may still be
#: installed under on a developer's machine (see the PyPI packaging round in
#: ``cli/AGENTS.md``). Distinct from ``shared.brand.LEGACY_DISTRIBUTIONS``,
#: which is the set this project was actually PUBLISHED under: these are
#: best-effort probes appended after the real ones.
_CONSIDERED_DIST_NAMES: Tuple[str, ...] = ("neo-agent-cli",)

#: The version reported for a source tree that was never pip-installed. The
#: ``+source`` local marker is load-bearing: ``cli/selfupdate`` treats it as
#: "not an installed release" and refuses to compare it against a public one.
SOURCE_VERSION = "0.2.1+source"


def _get_version() -> str:
    """The installed distribution version (with a static fallback).

    Assumes the package is installed (normal case: importlib.metadata
    answers); if the metadata lookup fails (e.g. running from a source
    tree that was never pip-installed), return the version this tree
    was released as instead of crashing.

    The candidate list comes from :mod:`shared.brand` — the CURRENT
    distribution name first, then every name this project shipped under
    before — rather than being written out here. A hardcoded list is how a
    rename silently reports ``(unknown)``: the names move, the literal does
    not, and every version probe, self-update check and release-staleness
    notice reads as "this build has no version".
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        from shared.brand import DISTRIBUTION, LEGACY_DISTRIBUTIONS

        for dist in (DISTRIBUTION, *LEGACY_DISTRIBUTIONS, _CONSIDERED_DIST_NAMES):
            try:
                return version(dist)
            except PackageNotFoundError:
                continue
        return SOURCE_VERSION
    except Exception:
        return SOURCE_VERSION


def _help_metavar() -> str:
    """The ``{...}`` usage command list, derived from the command registry.

    Ceiling-16: the metavar used to be a hand-written string, which is how a
    help line ends up advertising a command that was renamed and omitting one
    that was added. ``cli.capability`` holds the registry the parser fills in
    at the end of this function; this reads it, and falls back to the
    previous literal only if the registry is somehow empty.
    """
    try:
        from cli.capability import help_metavar

        return help_metavar()
    except Exception:
        return "{fix,run,scan,config,status,serve,acp,capabilities}"


# ---------------------------------------------------------------------------
# The AUTOMATION surface, and the SCRIPT FORMS it sits on top of
# ---------------------------------------------------------------------------
#
# The measured problem this round exists to close: `cli/main.py` declared 28
# visible top-level argparse commands sitting beside 56 slash commands as
# apparent peers, so a reader could not tell which vocabulary was the
# product. Two vocabularies for one product is a documentation defect, and
# the honest fix is to say which one is which rather than to delete working
# code.
#
# Everything below is DATA, and the help text is DERIVED from it, because a
# hand-written sentence beside a hand-written table is how the two drift.

#: The one line `neo --help` uses to say where the product surface actually
#: is. Kept to a single line deliberately: a second paragraph of framing in a
#: usage screen is a wall nobody reads.
PRODUCT_SURFACE_NOTE = (
    "the interactive product surface is the slash commands inside a session: "
    "run `neo` with no arguments and type `/help`. Any of them runs "
    'non-interactively as `neo run "/<command> [args]"`.'
)

#: Top-level argparse commands that stay DISPATCHABLE but are not advertised
#: as top-level commands, because a session command already hands the user
#: that exact line.
#:
#: ``value`` is the slash command that is the door. Nothing here is removed:
#: `cli.commands.HEADLESS_FLAG_EQUIVALENTS` rows name these very commands
#: (``/plugins`` -> ``neo plugin list``), so deleting one would break a row
#: whose whole job is to tell a session user what to type instead. What
#: changed is the VOCABULARY — they are script forms, not peers of ``/help``.
#:
#: Two commands that fit this description are deliberately NOT here:
#: ``doctor`` and ``support-bundle``. Both are named by the automation
#: contract as things an operator types from memory against a machine they
#: cannot open a session on, and hiding them from the one screen such an
#: operator can read would trade a duplication complaint for a
#: discoverability complaint. That is a judgement call and it is recorded
#: rather than smuggled in: see ``ONE_IMPLEMENTATION`` below, which is the
#: table that declares the relationship for BOTH the hidden and the
#: advertised duplicates.
SCRIPT_FORM_COMMANDS: Dict[str, str] = {
    "status": "/status (also /cost)",
    "config": "/settings, /init, /theme",
    "login": "/login (also /model)",
    "logout": "/logout",
    "connect": "/connect",
    "mcp": "/mcp",
    "skills": "/skills",
    "plugin": "/plugins",
    "watch": "/watch",
    "hooks": "/hooks",
    "migrate": "/migrate",
    "worktree": "/worktree",
    "auth": "/login, /logout",
}

#: The declared one-implementation relationship for every top-level command
#: that a slash command also reaches: ``{command: (slash commands, how)}``.
#:
#: ``how`` is the honest answer and there are exactly two kinds:
#:
#: ``"shared-handler"``
#:     both doors reach one function, so their receipts cannot disagree.
#: ``"script-form"``
#:     the session command refuses and names this script line, so there is
#:     one implementation and the session is not a second one.
#: ``"two-renderers"``
#:     BOTH read the same backend module but render their own text, so the
#:     FACTS agree and the BYTES do not. Declared as its own kind because
#:     calling it ``shared-handler`` would be a claim this round did not
#:     earn; the delegation that would close it is filed as a handoff.
#: ``"same-capability"``
#:     another script name for a capability whose door is a session command,
#:     and which no ``HEADLESS_FLAG_EQUIVALENTS`` row points at. Declared
#:     rather than left implicit precisely because nothing else in the tree
#:     records that this name exists.
ONE_IMPLEMENTATION: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "status": (("/status", "/cost"), "two-renderers"),
    "config": (("/settings", "/init", "/theme"), "two-renderers"),
    "login": (("/login", "/model"), "shared-handler"),
    "logout": (("/logout",), "shared-handler"),
    "mcp": (("/mcp",), "two-renderers"),
    "skills": (("/skills",), "two-renderers"),
    "plugin": (("/plugins",), "two-renderers"),
    "watch": (("/watch",), "script-form"),
    "hooks": (("/hooks",), "script-form"),
    "migrate": (("/migrate",), "script-form"),
    "worktree": (("/worktree",), "script-form"),
    "connect": (("/connect",), "script-form"),
    "doctor": (("/doctor",), "shared-handler"),
    "support-bundle": (("/support-bundle",), "script-form"),
    "auth": (("/login", "/logout"), "same-capability"),
}

#: The kinds ``ONE_IMPLEMENTATION`` may declare. A new word here is an edit
#: somebody makes on purpose rather than a typo that silently reads as a
#: stronger claim than the code earns.
ONE_IMPLEMENTATION_KINDS = frozenset(
    {"shared-handler", "script-form", "two-renderers", "same-capability"}
)


def script_form_note() -> str:
    """One ``neo --help`` line naming every script form, derived from data.

    The names come from :data:`SCRIPT_FORM_COMMANDS` rather than from a
    sentence somebody wrote, because a help line that lists a command the
    parser does not have is exactly the drift
    ``cli.capability.help_metavar`` was written to remove.
    """
    names = ", ".join(sorted(SCRIPT_FORM_COMMANDS))
    return (
        "script forms — a session command is the door, and these stay "
        f"callable for CI and for `neo --help` on the command itself: {names}"
    )


def _apply_automation_surface(sub: Any) -> None:
    """Drop the script forms from the top-level help LISTING.

    Dispatch is untouched: argparse resolves a name through
    ``_SubParsersAction._name_parser_map`` (the very object exposed as
    ``sub.choices``), while the help listing is rendered from the separate
    ``_choices_actions`` help-only registry. Filtering that registry is the
    same technique ``cli/completion.py`` uses to hide ``__completions``, and
    on 3.10/3.11 it is the ONLY one that works: a subparser registered with
    ``help=argparse.SUPPRESS`` is still rendered by those versions.

    The registry handed to ``cli.capability.register_commands`` stays the
    COMPLETE parser set, so the registry/parser cross-check cannot start
    reporting drift over a presentational choice.
    """
    choices = getattr(sub, "choices", {}) or {}
    missing = sorted(set(SCRIPT_FORM_COMMANDS) - set(choices))
    if missing:
        # Fail loudly at build time rather than advertising a door that does
        # not exist: a registry row naming a removed command is worse than a
        # crash, because it is a lie a user acts on.
        raise RuntimeError(
            "cli.main.SCRIPT_FORM_COMMANDS names commands this parser does "
            f"not create: {', '.join(missing)}"
        )
    hidden = set(SCRIPT_FORM_COMMANDS)
    sub._choices_actions = [
        action for action in sub._choices_actions if action.dest not in hidden
    ]


def advertised_commands(sub: Any) -> List[str]:
    """The top-level commands ``neo --help`` lists, in listing order.

    Read from the parser's own help-only registry, so it cannot name a
    command the parser does not create and cannot miss one it does.
    """
    return [
        str(action.dest)
        for action in (getattr(sub, "_choices_actions", []) or [])
        if not str(action.dest).startswith("_")
    ]


def automation_surface(sub: Any = None) -> Dict[str, Any]:
    """A machine-readable receipt for the surface this module declares.

    ``tests/test_automation_surface.py`` asserts against this rather than
    re-deriving the tables, and so can any surface that wants to render the
    split itself. Total: with no ``sub`` it builds the parser once, because
    the advertised list is only known once every subcommand exists.
    """
    if sub is None:
        sub = _subparsers_of(build_parser())
    return {
        "schema": "neo.automation_surface/1",
        "script_forms": dict(sorted(SCRIPT_FORM_COMMANDS.items())),
        "one_implementation": {
            key: {"slash": list(slash), "how": how}
            for key, (slash, how) in sorted(ONE_IMPLEMENTATION.items())
        },
        "advertised": advertised_commands(sub),
        "note": (
            "script forms stay dispatchable and stay documented per command "
            "by `neo <name> --help` and docs/commands.md"
        ),
    }


def _subparsers_of(parser: Any) -> Any:
    """The one ``_SubParsersAction`` a parser built by this module has.

    ``parser._subparsers`` is the argparse *group* the action lives in, not
    the action itself, so the action is the group action whose ``choices``
    is a name->parser map.
    """
    group = getattr(parser, "_subparsers", None)
    actions = [
        action
        for action in (getattr(group, "_group_actions", []) or [])
        if isinstance(getattr(action, "choices", None), dict)
    ]
    if len(actions) != 1:
        raise ValueError(
            f"expected exactly one subparsers action, found {len(actions)}"
        )
    return actions[0]


def build_parser() -> argparse.ArgumentParser:
    """Assemble the full CLI parser (exported for tests)."""
    from cli import capability as _capability

    # build_parser asks cli.capability for its metavar, and that module's
    # cross-check would otherwise re-enter this function.
    _capability._PARSER_BUSY = True
    try:
        return _build_parser_inner()
    finally:
        _capability._PARSER_BUSY = False


def _build_parser_inner() -> argparse.ArgumentParser:
    """The real parser body (see :func:`build_parser` for the re-entry guard)."""
    from cli.completion import add_completion_parser
    from cli.selfupdate import add_update_parser
    from cli.uninstall import add_uninstall_parser

    epilog = (
        PRODUCT_SURFACE_NOTE
        + "\n"
        + script_form_note()
        + "\nexit codes: 0 success | 1 task failed | 2 usage/config error | "
        "3 environment error (Docker, deps) | 4 model/network error | "
        "130 interrupted\n"
        "docs: https://github.com/Pavanteja2007/coding-harness#readme"
    )
    parser = argparse.ArgumentParser(
        prog="neo",
        description="Neo — the AI coding agent for your terminal. This is the "
        "AUTOMATION surface: one-shot agent work plus the commands a "
        "script, a CI job, or an editor needs.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="neo " + _get_version())
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
        help="list recent sessions across every repository "
        "(resumable marked; add --repo-filter <name> to narrow)",
    )
    parser.add_argument(
        "--repo-filter",
        dest="repo_filter",
        default=None,
        metavar="REPO",
        help="restrict --list-sessions/--continue to one repository "
        "(path, repo key, or directory name)",
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        # Usage/help list the PUBLIC commands only (hidden machinery
        # like `__completions` still dispatches via sub.choices). The
        # metavar is set at the END of this function from the registry
        # cli.capability holds, because the list is only known once every
        # subcommand has been added; a hand-written string is how a help
        # line ends up advertising a command that was renamed.
        metavar="{command}",
    )

    # serve (ceiling-16: the local agent server)
    p_serve = sub.add_parser(
        "serve",
        help="serve the local agent server (loopback HTTP/SSE/WebSocket)",
    )
    p_serve.add_argument(
        "--repo", default=None, help="repository root (default: current directory)"
    )
    p_serve.add_argument(
        "--host", default="127.0.0.1", help="bind host (default: 127.0.0.1, loopback)"
    )
    p_serve.add_argument(
        "--port", type=int, default=8765, help="bind port (default: 8765; 0 = pick one)"
    )
    p_serve.add_argument(
        "--auth-token",
        default="",
        help="require this bearer token (or set NEO_SERVER_TOKEN); required "
        "for any non-loopback bind",
    )
    p_serve.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="permit a non-loopback bind (still requires a token) and say so",
    )
    p_serve.add_argument("--log-root", default=None, help="task log root")
    p_serve.add_argument(
        "--json", action="store_true", help="print the bound endpoints as JSON"
    )
    p_serve.set_defaults(func=cmd_serve)

    # acp (ceiling-16: editor integration over ACP v1 on stdio)
    p_acp = sub.add_parser(
        "acp",
        help="serve ACP v1 on stdio for an editor (Zed, and any ACP client)",
    )
    p_acp.add_argument(
        "--repo", default=None, help="repository root (default: current directory)"
    )
    p_acp.add_argument(
        "--editor",
        default="zed",
        help="editor whose configuration the first-run notice prints (default: zed)",
    )
    p_acp.add_argument(
        "--print-config",
        action="store_true",
        help="print the exact editor configuration and exit without serving",
    )
    p_acp.add_argument(
        "--no-first-run-notice",
        action="store_true",
        help="suppress the once-per-install editor configuration notice",
    )
    p_acp.add_argument("--log-root", default=None, help="task log root")
    p_acp.set_defaults(func=cmd_acp)

    # capabilities (ceiling-16: install truth)
    p_caps = sub.add_parser(
        "capabilities",
        help="report what this installation can actually do",
    )
    p_caps.add_argument(
        "--json", action="store_true", help="machine-readable capability report"
    )
    p_caps.set_defaults(func=cmd_capabilities)

    # worktree (ceiling-06: Git worktree isolation for agent runs)
    p_worktree = sub.add_parser(
        "worktree",
        help="manage isolated Git worktrees (new/list/go/rm)",
    )
    _worktree_sub = p_worktree.add_subparsers(dest="worktree_action", required=True)
    _wt_new = _worktree_sub.add_parser("new", help="create a detached worktree")
    _wt_new.add_argument("--repo", default=".", help="source repository (default: cwd)")
    _wt_new.add_argument("name", help="worktree name")
    _wt_new.add_argument("--base", default="", help="base commit (default: repo HEAD)")
    _wt_new.add_argument(
        "--log-root", default=None, help="logs root holding worktrees/"
    )
    _wt_new.add_argument("--json", action="store_true", help="machine-readable output")
    _wt_new.set_defaults(func=cmd_worktree)
    _wt_list = _worktree_sub.add_parser("list", help="list managed worktrees")
    _wt_list.add_argument(
        "--repo", default=".", help="source repository (default: cwd)"
    )
    _wt_list.add_argument(
        "--log-root", default=None, help="logs root holding worktrees/"
    )
    _wt_list.add_argument("--json", action="store_true", help="machine-readable output")
    _wt_list.set_defaults(func=cmd_worktree)
    _wt_go = _worktree_sub.add_parser("go", help="print a worktree's absolute path")
    _wt_go.add_argument("--repo", default=".", help="source repository (default: cwd)")
    _wt_go.add_argument("name", help="worktree name")
    _wt_go.add_argument("--log-root", default=None, help="logs root holding worktrees/")
    _wt_go.add_argument("--json", action="store_true", help="machine-readable output")
    _wt_go.set_defaults(func=cmd_worktree)
    _wt_rm = _worktree_sub.add_parser("rm", help="remove a worktree")
    _wt_rm.add_argument("--repo", default=".", help="source repository (default: cwd)")
    _wt_rm.add_argument("name", help="worktree name")
    _wt_rm.add_argument("--force", action="store_true", help="remove even when dirty")
    _wt_rm.add_argument("--log-root", default=None, help="logs root holding worktrees/")
    _wt_rm.add_argument("--json", action="store_true", help="machine-readable output")
    _wt_rm.set_defaults(func=cmd_worktree)

    # fix
    p_fix = sub.add_parser("fix", help="fix one bug in one repo (single-task mode)")
    p_fix.add_argument("--repo", required=True, help="path to the repo to fix")
    p_fix.add_argument(
        "--worktree",
        default="",
        metavar="NAME",
        help="run inside an isolated Git worktree (the original checkout is "
        "never mutated by the run)",
    )
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
        help="turn a neo scan finding into this task "
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

    p_cfg_status = cfg_sub.add_parser(
        "status", help="show the active provider/model and source tier"
    )
    p_cfg_status.add_argument("--json", action="store_true", help="print masked JSON")
    p_cfg_status.set_defaults(func=cmd_config)

    p_cfg_provider = cfg_sub.add_parser("provider", help="alias for config status")
    p_cfg_provider.add_argument("--json", action="store_true", help="print masked JSON")
    p_cfg_provider.set_defaults(func=cmd_config)

    p_cfg_path = cfg_sub.add_parser(
        "path", help="show the settings file locations for this tier setup"
    )
    p_cfg_path.set_defaults(func=cmd_config)

    p_cfg_list = cfg_sub.add_parser(
        "list", help="effective settings + which tier each came from"
    )
    p_cfg_list.add_argument("--json", action="store_true", help="print masked JSON")
    p_cfg_list.set_defaults(func=cmd_config)

    p_cfg_get = cfg_sub.add_parser("get", help="print one effective value")
    p_cfg_get.add_argument("key")
    p_cfg_get.add_argument("--json", action="store_true", help="print masked JSON")
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
        help="create <repo>/.neo/settings.toml (committable) and "
        "git-ignore settings.local.toml",
    )
    p_cfg_init.add_argument(
        "--project-root",
        default=None,
        help="repo root (default: current directory)",
    )
    p_cfg_init.set_defaults(func=cmd_config)

    p_profile = sub.add_parser("profile", help="manage named provider profiles")
    profile_sub = p_profile.add_subparsers(dest="profile_command", required=True)
    p_profile_list = profile_sub.add_parser("list", help="list provider profiles")
    p_profile_list.add_argument("--json", action="store_true", help="print masked JSON")
    p_profile_list.set_defaults(func=cmd_profile)
    p_profile_show = profile_sub.add_parser("show", help="show one masked profile")
    p_profile_show.add_argument("name")
    p_profile_show.add_argument("--json", action="store_true", help="print masked JSON")
    p_profile_show.set_defaults(func=cmd_profile)
    p_profile_use = profile_sub.add_parser("use", help="select a profile at one tier")
    p_profile_use.add_argument("name")
    p_profile_use.add_argument(
        "--tier", choices=("global", "project", "local"), default="global"
    )
    p_profile_use.set_defaults(func=cmd_profile)
    p_profile_remove = profile_sub.add_parser("remove", help="remove a stored profile")
    p_profile_remove.add_argument("name")
    p_profile_remove.add_argument(
        "--tier", choices=("global", "project", "local"), default="global"
    )
    p_profile_remove.set_defaults(func=cmd_profile)

    # login / logout (first-run onboarding, cli/onboard.py)
    p_login = sub.add_parser(
        "login",
        help="configure a model endpoint (interactive wizard, saved globally)",
    )
    p_login.add_argument(
        "--tier",
        choices=("global", "project", "local"),
        default="global",
        help="where to save the profile (project keys remain global; local is ignored)",
    )
    p_login.add_argument("--provider", help="provider for non-interactive login")
    p_login.add_argument(
        "--profile",
        help="save and select this named provider profile after a successful health check",
    )
    p_login.add_argument("--model", help="model for non-interactive login")
    p_login.add_argument(
        "--base-url", dest="base_url", help="OpenAI-compatible endpoint for login"
    )
    p_login.add_argument(
        "--api-key",
        dest="api_key",
        help="API key for non-interactive login (prefer NEO_API_KEY)",
    )
    p_login.add_argument("--label", help="optional display label")
    p_login.add_argument(
        "--no-health-check",
        action="store_true",
        help="skip contact only for a local endpoint (remote credentials must pass health check)",
    )
    p_login.set_defaults(func=_cmd_login)

    p_logout = sub.add_parser(
        "logout", help="remove the stored api_key (keeps model/base_url)"
    )
    p_logout.set_defaults(func=_cmd_logout)

    # connect / auth (the opencode-style credential flow, cli/auth.py).
    # ONE additive line: the subparser and the handlers it dispatches to are
    # declared together in cli/auth.py, so the command surface cannot drift
    # away from the backend it names.
    from cli import auth as _auth

    _auth.register_connect_parser(sub)

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

    # watch — follow a detached run's event journal (VEX-CEILING-10)
    p_watch = sub.add_parser(
        "watch",
        help="follow a run (including a detached one) from its event journal",
    )
    p_watch.add_argument("task_id", help="the run's task id")
    p_watch.add_argument(
        "--log-root", default=None, help="task log root (default: ./logs)"
    )
    p_watch.add_argument(
        "--interval-s",
        type=float,
        default=0.25,
        help="journal poll interval (default: 0.25)",
    )
    p_watch.add_argument(
        "--timeout-s",
        type=float,
        default=0.0,
        help="give up after N seconds without a terminal event (0 = no limit)",
    )
    p_watch.add_argument(
        "--json",
        action="store_true",
        help="print the final watch receipt as JSON on stdout (machine-readable)",
    )
    p_watch.set_defaults(func=cmd_watch)

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

    p_show = plug_sub.add_parser(
        "show", help="inspect one plugin, its origin, skills, and commands"
    )
    p_show.add_argument("name", help="installed plugin name")
    p_show.set_defaults(func=cmd_plugin)

    p_inspect = plug_sub.add_parser("inspect", help="alias for plugin show")
    p_inspect.add_argument("name", help="installed plugin name")
    p_inspect.set_defaults(func=cmd_plugin)

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

    # run (headless surface for the shared slash-command contract)
    p_run = sub.add_parser(
        "run",
        help="run one slash command headlessly (same contract as the TUI/REPL)",
    )
    p_run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help=(
            'one command line, e.g. "/diff" or "/trace 12"; quoting the '
            "whole line is optional — neo run /steer focus the tests also works"
        ),
    )
    p_run.add_argument(
        "--repo", default=None, help="repository root (default: current directory)"
    )
    p_run.add_argument(
        "--log-root", default=None, help="task log root (default: ./logs)"
    )
    p_run.add_argument(
        "--json", action="store_true", help="machine-readable result document"
    )
    # Ceiling-12 automation: register this command line to run at a future
    # time. The schedule is a bounded, logged, revertible REQUEST; it is
    # resolved through runtime.schedules, which clamps approval and budget
    # policy and never executes work itself.
    p_run.add_argument(
        "--at",
        default=None,
        help=(
            "schedule this command instead of running it now: ISO-8601 "
            "timestamp, epoch seconds, or an offset like +15m / +2h / +1d"
        ),
    )
    p_run.add_argument(
        "--schedule-id",
        default=None,
        help="schedule name (default: derived from the command line)",
    )
    p_run.add_argument(
        "--schedule-every",
        type=float,
        default=0.0,
        help="repeat interval in seconds (min 60); omit for a one-shot schedule",
    )
    p_run.add_argument(
        "--schedule-max-runs",
        type=int,
        default=32,
        help="maximum times a periodic schedule may fire (default: 32)",
    )
    p_run.set_defaults(func=cmd_run_command)

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
    p_mcp_list.add_argument(
        "--repo", default=None, help="repository for connector labels"
    )
    p_mcp_list.add_argument("--timeout", type=float, default=60.0)
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
    p_mcp_call.add_argument(
        "--repo", default=None, help="repository for connector labels"
    )
    p_mcp_call.add_argument("--timeout", type=float, default=60.0)
    p_mcp_call.set_defaults(func=cmd_mcp_call)

    p_mcp_add = mcp_sub.add_parser(
        "add", help="register an MCP server label (stored in global settings)"
    )
    p_mcp_add.add_argument("label", help="server label (letters/digits/dots/dashes)")
    p_mcp_add.add_argument(
        "--tier",
        choices=("global", "project", "local"),
        default="global",
        help="where to persist the connector",
    )
    p_mcp_add.add_argument(
        "--repo", default=None, help="repository for project/local tiers"
    )
    p_mcp_add.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="launch command after `--`, e.g. neo mcp add linter -- python -m mcp_server",
    )
    p_mcp_add.set_defaults(func=cmd_mcp)

    p_mcp_rm = mcp_sub.add_parser(
        "remove", help="remove an MCP server label from global settings"
    )
    p_mcp_rm.add_argument("label", help="server label")
    p_mcp_rm.add_argument(
        "--tier",
        choices=("global", "project", "local"),
        default="global",
        help="tier to remove from",
    )
    p_mcp_rm.add_argument(
        "--repo", default=None, help="repository for project/local tiers"
    )
    p_mcp_rm.set_defaults(func=cmd_mcp)

    p_mcp_ls = mcp_sub.add_parser(
        "list", help="list configured MCP servers (global < project < local)"
    )
    p_mcp_ls.add_argument(
        "--repo", default=None, help="repository for project/local labels"
    )
    p_mcp_ls.set_defaults(func=cmd_mcp)

    p_mcp_health = mcp_sub.add_parser(
        "health", help="spawn each MCP server and report ok/fail"
    )
    p_mcp_health.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="per-server health timeout in seconds",
    )
    p_mcp_health.add_argument(
        "--repo", default=None, help="repository for project/local labels"
    )
    p_mcp_health.set_defaults(func=cmd_mcp)

    p_mcp_perms = mcp_sub.add_parser(
        "permissions",
        help="declare or show what a connector is allowed to do",
    )
    p_mcp_perms.add_argument(
        "label", nargs="?", default=None, help="connector label (omit to list all)"
    )
    p_mcp_perms.add_argument(
        "--tier", choices=("global", "project", "local"), default="project"
    )
    p_mcp_perms.add_argument(
        "--repo", default=None, help="repository for project/local tiers"
    )
    p_mcp_perms.add_argument(
        "--tool",
        dest="tools",
        action="append",
        default=None,
        help="declare one callable tool name (repeatable; '*' = all the server offers)",
    )
    p_mcp_perms.add_argument(
        "--side-effect",
        dest="side_effect",
        default=None,
        choices=("read", "search", "network", "mutation", "destructive"),
        help="session ceiling applied to each tool's declared side-effect class",
    )
    p_mcp_perms.add_argument(
        "--network",
        dest="network",
        action="append",
        default=None,
        help="declare one host a network-capable tool may reach (repeatable)",
    )
    p_mcp_perms.add_argument(
        "--write",
        dest="write",
        action="store_true",
        default=None,
        help="declare that this connector MAY write files",
    )
    p_mcp_perms.add_argument(
        "--no-write",
        dest="write",
        action="store_false",
        help="declare that this connector may NOT write files (mutating tools refused)",
    )
    p_mcp_perms.add_argument(
        "--clear", action="store_true", help="remove the declaration for this label"
    )
    p_mcp_perms.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    p_mcp_perms.set_defaults(func=cmd_mcp_permissions)

    p_doctor = sub.add_parser(
        "doctor", help="read-only health check with actionable remediation"
    )
    p_doctor.add_argument("--repo", default=None, help="repository to report about")
    p_doctor.add_argument(
        "--log-root", default=None, help="run-artifact root to report about"
    )
    p_doctor.add_argument("--json", action="store_true", help="machine-readable output")
    p_doctor.set_defaults(func=cmd_doctor)

    p_support = sub.add_parser(
        "support-bundle",
        help="gather version, environment, config shape, and recent errors into one archive",
    )
    p_support.add_argument("--repo", default=None, help="repository to report about")
    p_support.add_argument("--log-root", default=None, help="run-artifact root to scan")
    p_support.add_argument(
        "--out",
        default=None,
        help="output path (default: ./neo-support-<timestamp>.zip)",
    )
    p_support.add_argument(
        "--recent-runs",
        type=int,
        default=5,
        help="how many recent run directories to read journals from (default: 5)",
    )
    p_support.add_argument(
        "--no-archive",
        action="store_true",
        help="write a plain directory of files instead of one .zip",
    )
    p_support.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    p_support.set_defaults(func=cmd_support_bundle)

    p_migrate = sub.add_parser(
        "migrate",
        help="detect an older config/state layout, show the plan, apply it atomically",
    )
    p_migrate.add_argument("--repo", default=None, help="repository to migrate")
    p_migrate.add_argument("--log-root", default=None, help="run-artifact root in use")
    p_migrate.add_argument(
        "--apply", action="store_true", help="apply the plan (default: show it only)"
    )
    p_migrate.add_argument(
        "--yes", action="store_true", help="apply without the interactive confirmation"
    )
    p_migrate.add_argument(
        "--allow-destructive",
        action="store_true",
        help="allow steps that remove data (still fully reversible while uncommitted)",
    )
    p_migrate.add_argument(
        "--only",
        action="append",
        default=None,
        help="restrict to one migration id (repeatable)",
    )
    p_migrate.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    p_migrate.set_defaults(func=cmd_migrate)

    # `neo hooks` verbs. Each verb's flags are declared here and dispatched to
    # the shared implementation in extensions.user_hooks.hooks_command, which is
    # the same function the `/hooks` slash command calls.
    p_hooks = sub.add_parser(
        "hooks",
        help="inspect and try the declarative hook layer: list, run, test, trust, reload",
    )
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command", required=True)

    def _add_hooks_common(parser, *, subject: bool = True) -> None:
        """Add the flags every `neo hooks` verb shares."""
        parser.add_argument("--repo", default=None, help="repository for project hooks")
        parser.add_argument(
            "--json", action="store_true", help="machine-readable output (one document)"
        )
        if not subject:
            return
        parser.add_argument("--tool", default="", help="subject tool name")
        parser.add_argument("--path", default="", help="subject path")
        parser.add_argument("--command", default="", help="subject command")
        parser.add_argument("--mcp-server", default="", help="subject MCP server label")
        parser.add_argument(
            "--side-effect",
            dest="side_effect",
            default="",
            help="subject declared side-effect class (read|search|network|mutation|destructive)",
        )

    p_hooks_list = hooks_sub.add_parser(
        "list",
        help="list every configured hook, the event vocabulary, and the fail policy in force",
    )
    _add_hooks_common(p_hooks_list, subject=False)
    p_hooks_list.set_defaults(func=cmd_hooks)

    p_hooks_run = hooks_sub.add_parser(
        "run", help="fire one hook event through the real gate and show what it decided"
    )
    p_hooks_run.add_argument(
        "event",
        help=(
            "SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, "
            "PostToolUseFailure, Notification, Stop, SubagentStart, "
            "SubagentStop, PreCompact, SessionEnd"
        ),
    )
    _add_hooks_common(p_hooks_run)
    p_hooks_run.set_defaults(func=cmd_hooks)

    p_hooks_test = hooks_sub.add_parser(
        "test",
        help="run ONE hook against a synthetic event and show its real output",
    )
    p_hooks_test.add_argument(
        "--id", dest="hook_id", default="", help="the hook id to run (the only one)"
    )
    p_hooks_test.add_argument(
        "--event", default="PreToolUse", help="event whose synthetic subject to use"
    )
    _add_hooks_common(p_hooks_test)
    p_hooks_test.set_defaults(func=cmd_hooks)

    p_hooks_trust = hooks_sub.add_parser(
        "trust",
        help="show, or record, your decision about whether to trust a hook",
    )
    p_hooks_trust.add_argument("--id", dest="hook_id", default="", help="the hook id")
    p_hooks_trust.add_argument(
        "--decision",
        default="",
        choices=("", "trusted", "untrusted"),
        help="record this decision (omit to only report the current state)",
    )
    p_hooks_trust.add_argument(
        "--note", default="", help="a note stored with the decision"
    )
    _add_hooks_common(p_hooks_trust, subject=False)
    p_hooks_trust.set_defaults(func=cmd_hooks)

    p_hooks_reload = hooks_sub.add_parser(
        "reload",
        help="re-register every hook without restarting, and report what changed",
    )
    _add_hooks_common(p_hooks_reload, subject=False)
    p_hooks_reload.set_defaults(func=cmd_hooks)

    p_skills = sub.add_parser(
        "skills", help="manage standalone skills and inspect plugin skills"
    )
    skills_sub = p_skills.add_subparsers(dest="skills_command", required=True)

    p_skills_install = skills_sub.add_parser(
        "install", help="install or replace a standalone skill from a local path"
    )
    p_skills_install.add_argument("source")
    p_skills_install.add_argument(
        "--tier", choices=("global", "project"), default="global"
    )
    p_skills_install.add_argument("--repo", default=None)
    p_skills_install.add_argument(
        "--name", default=None, help="override the skill name"
    )
    p_skills_install.set_defaults(func=cmd_skills)

    p_skills_list = skills_sub.add_parser(
        "list", help="list skills with origin, source, and enabled state"
    )
    p_skills_list.add_argument(
        "--repo", default=None, help="repo for project skills (default: CWD)"
    )
    p_skills_list.set_defaults(func=cmd_skills)

    p_skills_show = skills_sub.add_parser("show", help="print one skill's full body")
    p_skills_show.add_argument("name")
    p_skills_show.add_argument(
        "--repo", default=None, help="repo for project skills (default: CWD)"
    )
    p_skills_show.set_defaults(func=cmd_skills)

    p_skills_remove = skills_sub.add_parser(
        "remove", help="remove one standalone skill"
    )
    p_skills_remove.add_argument("name")
    p_skills_remove.add_argument(
        "--tier", choices=("global", "project"), default="global"
    )
    p_skills_remove.add_argument("--repo", default=None)
    p_skills_remove.set_defaults(func=cmd_skills)

    p_skills_enable = skills_sub.add_parser(
        "enable", help="enable one standalone skill"
    )
    p_skills_enable.add_argument("name")
    p_skills_enable.add_argument(
        "--tier", choices=("global", "project"), default="global"
    )
    p_skills_enable.add_argument("--repo", default=None)
    p_skills_enable.set_defaults(func=cmd_skills)

    p_skills_disable = skills_sub.add_parser(
        "disable", help="disable one standalone skill without deleting it"
    )
    p_skills_disable.add_argument("name")
    p_skills_disable.add_argument(
        "--tier", choices=("global", "project"), default="global"
    )
    p_skills_disable.add_argument("--repo", default=None)
    p_skills_disable.set_defaults(func=cmd_skills)

    # self-update (Task E — CLI citizenship)
    add_update_parser(sub)

    # shell completions (Task D) + the hidden __completions backend
    add_completion_parser(sub)

    # clean uninstall (Task F)
    add_uninstall_parser(sub)

    # Ceiling-16: publish the public command list to cli.capability's
    # registry, so --help's metavar, the completion inventory, and the
    # capability report all read the names this parser actually created.
    from cli import capability as _capability

    # VEX-CS-10: the script forms are dropped from the top-level help
    # LISTING before the registry is written, and they stay out of the
    # registry too — `register_commands` receives every name the parser
    # actually created, so the registry/parser cross-check stays exact.
    _apply_automation_surface(sub)
    _capability.register_commands(
        list(getattr(sub, "choices", {}) or {}), source="cli.main.build_parser"
    )
    sub.metavar = _help_metavar()
    return parser


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the local agent server (``agent_sdk.server.AgentServer``).

    Loopback by default. A non-loopback bind is REFUSED unless
    ``--allow-non-loopback`` is passed, and a non-loopback bind with no
    token is refused even then: an unauthenticated agent that can read a
    repository and spend a provider budget is never a default. The port
    actually bound is printed to stdout in ``--json`` mode, so
    ``--port 0`` is usable from a script.

    Exit codes follow cli.exit_codes: 2 for a refused bind, 4 when no model
    is configured, 0 only when the server stopped cleanly.
    """
    from cli.exit_codes import EXIT_CODES
    from cli.serve import build_serve_plan, serve_once

    repo = Path(args.repo) if getattr(args, "repo", None) else None
    log_root = _artifact_log_root(getattr(args, "log_root", None), repo)
    try:
        plan = build_serve_plan(
            repo=repo,
            host=str(args.host),
            port=int(args.port),
            token=str(getattr(args, "auth_token", "") or ""),
            allow_non_loopback=bool(getattr(args, "allow_non_loopback", False)),
            log_root=log_root,
        )
    except PermissionError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return EXIT_CODES["usage_error"]
    if not plan.model:
        message = (
            "no model configured — run `neo login` before serving, or the "
            "server will answer every request with an auth error"
        )
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "status": "refused",
                        "error": message,
                        "model": "",
                        "exit_code": EXIT_CODES["model_error"],
                    },
                    indent=2,
                )
            )
        else:
            ui.err_console().print(f"[neo.error]error: {message}[/]")
        return EXIT_CODES["model_error"]
    if plan.warning:
        print(plan.warning, file=sys.stderr)

    def _ready(endpoints: Dict[str, Any]) -> None:
        if getattr(args, "json", False):
            # ONE line, deliberately: this receipt is a handshake a script
            # reads to learn the bound port, so it must be parseable from a
            # single read() rather than a pretty-printed block.
            print(json.dumps({**endpoints, "plan": plan.to_dict()}), flush=True)
        else:
            print(f"neo serve listening on {endpoints['url']}")
            print(f"  model   {plan.model} (from {plan.model_source})")
            print(
                "  auth    "
                + (
                    "bearer token required"
                    if plan.auth_required
                    else "none (loopback only)"
                )
            )
            sys.stdout.flush()

    serve_once(plan, on_ready=_ready)
    return EXIT_CODES["success"]


def cmd_acp(args: argparse.Namespace) -> int:
    """Serve ACP v1 on stdio for an editor (``acp.server.ACPServer``).

    Protocol frames go to stdout and nothing else does. The first-run
    editor configuration and every warning go to stderr, because one stray
    byte on stdout desynchronizes the JSON-RPC stream and the editor then
    reports a protocol error with no visible cause.

    ``--print-config`` prints the exact editor configuration and exits
    without serving, so a user can wire their editor without starting a
    server.
    """
    from cli.exit_codes import EXIT_CODES
    from cli.serve import acp_stdio_serve, editor_configuration, first_run_notice

    repo = Path(args.repo) if getattr(args, "repo", None) else None
    editor = str(getattr(args, "editor", "") or "zed")
    if getattr(args, "print_config", False):
        print(editor_configuration(repo=repo, editor=editor))
        return EXIT_CODES["success"]
    if not getattr(args, "no_first_run_notice", False):
        notice = first_run_notice(repo=repo, editor=editor)
        if notice:
            print(notice, file=sys.stderr)
            sys.stderr.flush()
    log_root = _artifact_log_root(getattr(args, "log_root", None), repo)
    return acp_stdio_serve(repo=repo, log_root=log_root)


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Report what THIS installation can actually do.

    Compares the live runtime registry (the argparse command tree, the
    slash-command registry, the optional integration modules) against the
    installed distribution, and the installed version against the version
    the local docs describe. Exit 0 when every advertised capability is
    present; 1 when one is missing, so a packaging regression fails a CI
    check instead of quietly shipping a thinner wheel.
    """
    from cli.capability import capability_report_dict
    from cli.exit_codes import EXIT_CODES

    payload = capability_report_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        console = ui.console()
        console.print(
            f"[neo.accent]neo[/] {payload['version']}"
            + (
                f" [neo.muted](docs describe {payload['docs_version']})[/]"
                if payload["version_drift"]
                else ""
            )
        )
        for item in payload["capabilities"]:
            mark = ui.GLYPHS["ok"] if item["available"] else ui.GLYPHS["fail"]
            style = "neo.ok" if item["available"] else "neo.error"
            console.print(
                f"  [{style}]{mark}[/] {item['name']}"
                + (f" [neo.muted]— {item['detail']}[/]" if item["detail"] else "")
            )
        if payload["missing"]:
            console.print(
                f"[neo.error]{len(payload['missing'])} advertised "
                f"capabilit{'y' if len(payload['missing']) == 1 else 'ies'} "
                f"missing from this install: {', '.join(payload['missing'])}[/]"
            )
        else:
            console.print(
                f"[neo.ok]{ui.GLYPHS['ok']} every advertised capability is present[/]"
            )
    return EXIT_CODES["success"] if payload["ok"] else EXIT_CODES["task_failure"]


# Ceiling-16: the headless agent surface. ``-p``/``-`` are dispatched
# PRE-parse, next to --continue/--resume, because a bare ``-`` is not a
# legal top-level argparse token and because a piped run must never be
# mistaken for an interactive session.
_HEADLESS_VALUE_FLAGS = frozenset(
    {"--repo", "--log-root", "--session-id", "--model", "--provider"}
)
_HEADLESS_BOOL_FLAGS = frozenset({"--json"})


def _headless_prompt_args(
    raw: List[str],
) -> Optional[Tuple[str, bool, List[str]]]:
    """Detect ``-p SENTENCE`` / ``-`` in raw argv.

    Returns ``(prompt, read_stdin, rest)`` or None when this is not a
    headless invocation. ``rest`` is the remaining argv, from which
    :func:`_split_headless_options` takes the options.
    """
    argv = list(raw or [])
    if not argv:
        return None
    head = argv[0]
    if head == "-":
        return "", True, argv[1:]
    if head in ("-p", "--prompt"):
        if len(argv) < 2 or not str(argv[1]).strip():
            raise ValueError(
                f'{head} needs a sentence, e.g. neo {head} "explain cli/main.py"'
            )
        return str(argv[1]), False, argv[2:]
    return None


def _split_headless_options(
    rest: List[str],
) -> Tuple[Dict[str, Any], str]:
    """Split the headless tail into options and the remaining prompt text.

    Options may appear before or after the sentence, because a user
    composing a shell command will put them wherever reads well. An
    unrecognized ``--flag`` is treated as PROMPT TEXT rather than an error:
    a sentence may legitimately contain a double dash, and silently eating
    it would change what the agent was asked.
    """
    options: Dict[str, Any] = {
        "json": False,
        "repo": None,
        "log_root": None,
        "session_id": "",
        "model": None,
        "provider": None,
    }
    words: List[str] = []
    argv = list(rest or [])
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _HEADLESS_BOOL_FLAGS:
            options["json"] = True
        elif token in _HEADLESS_VALUE_FLAGS and index + 1 < len(argv):
            key = token[2:].replace("-", "_")
            options[key] = argv[index + 1]
            index += 1
        else:
            words.append(token)
        index += 1
    return options, " ".join(words).strip()


def _cmd_headless_prompt(prompt: str, read_stdin: bool, rest: List[str]) -> int:
    """Run one headless agent turn (``neo -p`` / ``neo -``).

    Returns the envelope's exit code. The piped form is refused on an
    interactive terminal rather than blocking: a user who typed ``neo -``
    at a prompt gets a usage line, not a hang.
    """
    from cli.exit_codes import EXIT_CODES

    options, tail = _split_headless_options(rest)
    if tail:
        prompt = f"{prompt} {tail}".strip() if prompt else tail
    context = ""
    truncated = False
    chars = 0
    if read_stdin:
        is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
        if is_tty and not prompt:
            ui.err_console().print(
                "[neo.error]error: neo - reads piped context; type a "
                'sentence with -p, e.g. neo -p "explain cli/main.py"[/]'
            )
            return EXIT_CODES["usage_error"]
        from cli.headless import read_piped_context

        context, truncated = read_piped_context()
        chars = len(context)
    if not prompt and not context:
        ui.err_console().print(
            "[neo.error]error: nothing to do — pass a sentence with -p or "
            "pipe context with neo -[/]"
        )
        return EXIT_CODES["usage_error"]

    from cli.headless import run_headless

    repo = Path(options["repo"]) if options.get("repo") else None
    log_root = _artifact_log_root(options.get("log_root"), repo)
    outcome = run_headless(
        prompt,
        repo=repo,
        log_root=log_root,
        session_id=str(options.get("session_id") or ""),
        context=context,
        stdin_truncated=truncated,
        stdin_chars=chars,
        as_json=bool(options.get("json")),
    )
    if outcome.text:
        print(outcome.text)
    return int(outcome.exit_code)


def _emit_release_staleness_notice(raw: List[str]) -> None:
    """Warn once when the public release is older than the local docs.

    Three deliberate properties:

    - **Never on the version/update commands.** ``neo --version`` and
      ``neo update --check`` are the commands a user runs BECAUSE of a
      version discrepancy; printing a stale-release warning into their
      output is noise that makes the check harder to read.
    - **Never on ``--version``/``--help``**, which must stay byte-clean.
    - **Opt out entirely** with ``NEO_NO_RELEASE_NOTICE=1`` (CI, scripted
      callers) and stay silent offline: an unreachable index means "unknown",
      never "out of date".
    """
    if os.environ.get("NEO_NO_RELEASE_NOTICE") in ("1", "true", "yes"):
        return
    argv = list(raw or [])
    if not argv or argv[0] in ("--version", "--help", "-h", "update", "uninstall"):
        return
    try:
        from cli.capability import (
            local_docs_version,
            public_release_version,
            stale_release_notice,
        )

        public = public_release_version()
        if not public:
            return
        notice = stale_release_notice(
            public_version=public, docs_version=local_docs_version()
        )
    except Exception:
        return
    if notice:
        print(notice, file=sys.stderr)
        sys.stderr.flush()


def _normalize_run_argv(argv: List[str]) -> List[str]:
    """Move recognized ``neo run`` options ahead of REMAINDER command text."""
    run_index = 0
    while run_index < len(argv) and argv[run_index] == "--no-color":
        run_index += 1
    if run_index >= len(argv) or argv[run_index] != "run":
        return list(argv)
    prefix = argv[: run_index + 1]
    tail = argv[run_index + 1 :]
    options: List[str] = []
    command: List[str] = []
    index = 0
    while index < len(tail):
        token = tail[index]
        if token == "--json":
            options.append(token)
        elif token in {"--repo", "--log-root"} and index + 1 < len(tail):
            options.extend((token, tail[index + 1]))
            index += 1
        else:
            command.append(token)
        index += 1
    return [*prefix, *options, *command]


def _repo_filter_from_argv(raw: List[str]) -> Optional[str]:
    """Read the optional ``--repo-filter <REPO>`` from the raw argv.

    ``--continue`` / ``--resume`` / ``--list-sessions`` are dispatched
    PRE-parse (they work with no subcommand), so the cross-repo filter is
    read straight from argv instead of argparse's namespace. Returns None
    when the flag is absent or has no value.
    """
    argv = list(raw or [])
    if "--repo-filter" in argv:
        i = argv.index("--repo-filter")
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            return argv[i + 1]
    return None


def _artifact_log_root(explicit: Optional[str], repo: Optional[str] = None) -> Path:
    """The one artifact-root authority for a run-producing command.

    Routes through ``cli.session.resolve_artifact_root`` so a FORCED
    in-repo ``--log-root`` is gitignored and reported (a run directory
    holds a full ``pristine``/``work`` copy of the target source tree)
    while the default stays OUTSIDE the user's repository. The warning is
    printed once here so no caller can forget it: a run silently writing
    into a repository is exactly the failure this prevents.
    """
    try:
        from cli.session import resolve_artifact_root

        resolved = resolve_artifact_root(explicit, repo)
    except Exception:
        return Path(explicit) if explicit else default_logs_dir(repo)
    for warning in resolved.get("warnings") or ():
        try:
            ui.err_console().print(f"[neo.warn]{warning}[/]")
        except Exception:
            pass
    return Path(resolved["log_root"])


def _owns_interactive_terminal() -> bool:
    """Whether this process owns a real terminal on BOTH ends.

    ``sys.stdin.isatty()`` alone is not enough. On Windows a stdin bound
    to the NUL device — how CI jobs, Task Scheduler entries, services and
    ``cmd ... < NUL`` are wired — reports ``isatty() == True``, and the
    no-argument dispatch then dropped a strictly non-interactive
    invocation into the REPL/TUI instead of printing usage and exiting 2
    (found while verifying the installed artifact). Requiring stdout to
    be a TTY too keeps the session for a human at a terminal and makes
    every piped, redirected, or scheduler-driven shape honest."""
    try:
        return bool(
            sys.stdin is not None
            and sys.stdin.isatty()
            and sys.stdout is not None
            and sys.stdout.isatty()
        )
    except Exception:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (console script ``neo`` / ``python -m cli``).

    Assumes argv is None (real invocation: sys.argv) or a list for tests.
    No arguments + a terminal on both stdin and stdout -> the
    natural-language session (the primary UX); otherwise subcommand
    dispatch.

    The first statement honours the previous ``VEX_*`` environment names
    (see :mod:`shared.brand`). It runs here, before any parser or config
    read, so a rename cannot reach the user as a silently ignored setting.

    Returns a process exit code: 0 success, 1 task failure, 2 usage error.
    """
    from shared.brand import apply_legacy_env

    apply_legacy_env()
    raw = sys.argv[1:] if argv is None else list(argv)
    raw = _normalize_run_argv(raw)
    if "--no-color" in raw:
        ui.set_no_color(True)
    if os.environ.get("NO_COLOR") or os.environ.get("NEO_NO_COLOR") == "1":
        ui.set_no_color(True)

    # Ceiling-16: the headless AGENT surface (`neo -p "sentence"`, `neo -`).
    # Dispatched pre-parse, beside the session flags, for two reasons: a bare
    # `-` is not a legal top-level argparse token, and a piped run must never
    # be mistaken for an interactive session further down.
    try:
        headless = _headless_prompt_args(raw)
    except ValueError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2
    if headless is not None:
        return _cmd_headless_prompt(headless[0], headless[1], headless[2])

    # Ceiling-16: warn ONCE when the public release is older than the docs the
    # user is reading. Emitted to stderr so a `--json` document on stdout is
    # still the only thing on stdout.
    _emit_release_staleness_notice(raw)

    # Session persistence (Task A): --continue / --resume / --list-sessions
    # work with or without a subcommand and with piped stdin (they run a
    # resumable task and print the summary; no interactive loop needed).
    session_flags = [
        a for a in raw if a in ("--continue", "--resume", "--list-sessions")
    ]

    # Interactive natural-language mode: no subcommand, stdin is a TTY.
    # Tests pass argv explicitly, so they never land here by accident.
    # Full-screen TUI (textual) when the terminal supports it; the rich
    # REPL stays as the fallback (no TTY / NEO_TUI=0 / textual missing).
    if argv is None and not raw and _owns_interactive_terminal():
        try:
            try:
                from cli.tui import can_run_tui
            except ImportError:

                def can_run_tui() -> bool:
                    return False

            if can_run_tui():
                from cli.tui import run_tui

                return run_tui(version=_get_version())
            from cli.interactive import run_interactive

            return run_interactive()
        except KeyboardInterrupt:
            ui.console().print("\n[neo.muted]interrupted[/]")
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
            ui.err_console().print("[neo.error]interactive module unavailable[/]")
            return 2
        repo_filter = _repo_filter_from_argv(raw)
        if "--list-sessions" in raw:
            return cmd_list_sessions(repo=repo_filter)
        if "--resume" in raw:
            i = raw.index("--resume")
            if i + 1 >= len(raw):
                ui.err_console().print("[neo.error]error: --resume needs a task id[/]")
                return 2
            return cmd_resume(raw[i + 1])
        return cmd_continue(repo=repo_filter)

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
            "\n[neo.warn]interrupted — no orphaned "
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
