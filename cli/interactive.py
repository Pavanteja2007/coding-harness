"""Vex interactive natural-language mode — the PRIMARY user experience.

`vex` with no arguments drops into a session where the user types plain
language ("fix the login bug where the password is empty") and the harness
loop starts from that sentence — no flags required (Claude Code / Codex
style). The repo is inferred from the current working directory; the
session can switch repos, run multiple fixes, and shows live progress.

Flag-based commands (vex fix / run-benchmark / ...) remain the scriptable
automation path — Task D of the Vex CLI pass.

Live monitoring design (Tasks C+E): run_task is a blocking call whose
internals live in another module's objects — we do NOT reach into them.
The harness's own trace.jsonl (logs/{task_id}/) is the public observability
surface (documented in harness/AGENTS.md + INTERFACES.md): a monitor thread
tails it, derives phase/cost/steps from the event kinds, and renders a
rich Status spinner + running cost + last event, updated live.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli import ui

# Trace-event -> short status label (the only contract with harness.core:
# event KINDS are the public schema; fields used are documented ones).
_EVENT_LABELS = {
    "task_start": "starting",
    "baseline_verify": "verifying baseline on the pristine copy",
    "retrieval": "retrieving code context",
    "plan": "planning sub-steps",
    "plan_parse_error": "re-planning (unparseable plan)",
    "attempt_start": "attempt {attempt}",
    "attempt_resume": "resuming interrupted attempt",
    "step_start": "step: starting",
    "step_end": "step done",
    "step_skipped_resume": "skipping completed step (resume)",
    "model_request": "model: thinking ({step})",
    "model_response": "model: replied ({step})",
    "tool_call": "sandbox: running command",
    "tool_result": "sandbox: command finished",
    "verify": "verifier: running tests",
    "final_verify": "final verification",
    "recall": "recalling compacted context",
    "git_output": "producing git branch/commit",
    "rationale": "writing rationale",
    "task_end": "finishing",
}

# Events whose data carries step/attempt context for the label.
_LABEL_FIELDS: Dict[str, List[str]] = {
    "model_request": ["step"],
    "model_response": ["step"],
    "attempt_start": ["attempt"],
    "verify": ["phase"],
}


class LiveMonitor:
    """Tails logs/{task_id}/trace.jsonl and renders live status.

    Usage:
        mon = LiveMonitor(task_id, log_root)
        mon.start()            # spinner + running cost appear
        ... blocking run_task ...
        mon.stop()             # final line rendered, spinner cleared

    The monitor never raises into the run: any file/read error just leaves
    the last rendered status in place.
    """

    def __init__(
        self, task_id: str, log_root: Path, status_interval_s: float = 0.25
    ) -> None:
        self.task_id = task_id
        self.trace_path = Path(log_root) / task_id / "trace.jsonl"
        self._interval = status_interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status: Optional[ui.Status] = None
        self._events_seen = 0
        self._cost_usd = 0.0
        self._tokens = 0
        self._calls = 0
        self._last_label = "starting"
        self._last_event_at = time.monotonic()
        self._console = ui.console()
        self._quiet = False

    # -- lifecycle --------------------------------------------------------

    def start(self, quiet: bool = False) -> "LiveMonitor":
        """Begin rendering (quiet=True: track but don't render — for tests)."""
        self._quiet = quiet
        if not quiet:
            self._status = self._console.status(
                f"[vex.running]{self._last_label}[/]", spinner="dots"
            )
            self._status.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop rendering; print the one-line summary of what happened."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._status is not None:
            try:
                self._status.stop()
            except Exception:
                pass
            self._status = None
        if not self._quiet:
            calls = (
                f"[vex.muted]{self._calls} model call(s), {self._tokens:,} tokens[/]"
            )
            self._console.print(
                f"[vex.muted]run events: {self._events_seen} | "
                f"model: {calls} | cost: "
                f"[vex.accent]{ui.fmt_cost(self._cost_usd)}[/][/]"
            )

    # -- internals --------------------------------------------------------

    def _loop(self) -> None:
        """Tail the trace file, updating status/cost from each event."""
        # events may start flowing any moment; poll for the file itself.
        while not self._stop.is_set():
            if self.trace_path.exists():
                break
            time.sleep(0.1)
        pos = 0
        while not self._stop.is_set():
            try:
                with self.trace_path.open("rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    if chunk:
                        pos += len(chunk)
                        for line in chunk.decode(
                            "utf-8", errors="replace"
                        ).splitlines():
                            self._consume(line)
                        if (
                            self._last_event_at < time.monotonic() - 10.0
                            and not self._quiet
                        ):
                            # long silence (e.g. one long model call):
                            # show elapsed so the line keeps moving.
                            pass
            except OSError:
                pass  # file being rotated/archived mid-read
            self._stop.wait(self._interval)

    def _consume(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            return
        kind = obj.get("kind")
        data = obj.get("data") or {}
        if not kind:
            return
        self._events_seen += 1
        self._last_event_at = time.monotonic()

        if kind == "model_response":
            usage = data.get("usage") or {}
            self._calls += 1
            self._tokens += int(usage.get("tokens") or 0)
            self._cost_usd += float(usage.get("cost") or 0.0)

        label = _EVENT_LABELS.get(kind)
        if label:
            for field in _LABEL_FIELDS.get(kind, []):
                if data.get(field) is not None:
                    label = label.format(**{field: data[field]})
            self._last_label = label
            self._render()

    def _render(self) -> None:
        if self._quiet or self._status is None:
            return
        try:
            self._status.update(
                f"[vex.running]{self._last_label}[/] "
                f"[vex.muted]({self._events_seen} events, "
                f"{ui.fmt_cost(self._cost_usd)})[/]"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Approval watcher (Task E: interactive prompts with rendered diff)
# ---------------------------------------------------------------------------


def watch_for_approvals(
    log_root: Path, task_ids: List[str], stop: threading.Event, poll_s: float = 0.5
) -> None:
    """While runs are live, surface pending approval requests inline.

    The worker's gate is file-based (runtime/approval.py): the request
    appears at logs/{task_id}.runtime/approval/request.json and the worker
    blocks. This watcher renders the request's diff with the Vex theme and
    prompts the human; the typed decision goes to decision.json via the
    module's `decide()` helper (same protocol the tests use). Runs in the
    interactive session's monitor thread; a no-op when no task parks.
    """
    from runtime import approval as approval_mod

    handled: Dict[str, bool] = {tid: False for tid in task_ids}
    while not stop.is_set():
        for tid, done in handled.items():
            if done:
                continue
            gate = Path(log_root) / f"{tid}.runtime" / "approval"
            req = approval_mod.pending_request(str(gate))
            if req is None:
                continue
            handled[tid] = True  # one prompt per task per watcher
            con = ui.console()
            con.print()
            con.rule(f"[vex.accent]approval needed — {tid}[/]")
            if req.get("summary"):
                con.print(f"[vex.muted]{req['summary']}[/]")
            con.print("[vex.muted]proposed diff:[/]")
            ui.print_diff(req.get("diff") or "(no diff in request)")
            con.print("[vex.muted]issue:[/] " + str(req.get("issue_text", ""))[:300])
            while True:
                answer = input("approve this fix? [y/N] ").strip().lower()
                if answer in ("y", "yes", "n", "no", ""):
                    break
                print("please answer y or n")
            approve = answer in ("y", "yes")
            approval_mod.decide(str(gate), approve=approve)
            con.print(
                f"[vex.{'ok' if approve else 'error'}]"
                f"{'approved' if approve else 'rejected'}[/] — "
                f"the run continues {'with' if approve else 'without'} "
                f"the fix"
            )
        stop.wait(poll_s)


# ---------------------------------------------------------------------------
# Session persistence (Task A) — the CLI's memory of past runs
# ---------------------------------------------------------------------------

_SESSION_FILE = "sessions.jsonl"


def session_store_path(log_root: Path) -> Path:
    """logs/.vex-sessions.jsonl — the interactive session index."""
    return Path(log_root) / ".vex-sessions.jsonl"


def record_session(
    log_root: Path, task_id: str, issue: str, repo: str, status: str
) -> None:
    """Append one completed/attempted interactive run to the session index.

    Never raises: the index is an enhancement — a failed append must not
    take down a verified fix's reporting.
    """
    try:
        p = session_store_path(log_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "task_id": task_id,
                        "issue": issue[:200],
                        "repo": repo,
                        "status": status,
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def list_sessions(log_root: Path, limit: int = 15) -> List[Dict[str, Any]]:
    """Most-recent-first session entries (index + directory fallback).

    The index (logs/.vex-sessions.jsonl) carries metadata but only exists
    for runs THIS CLI version made AND recorded — including interrupted
    ones. As a fallback for anything the index misses (pre-ST2 runs,
    externally-killed workers, index loss), the log dirs themselves are
    scanned for resumable tasks and merged (deduped by task_id; newest
    by dir mtime). Resumable = state.json has completed AND remaining
    steps and no final result event.
    """
    entries: Dict[str, Dict[str, Any]] = {}

    p = session_store_path(log_root)
    if p.is_file():
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            obj["resumable"] = _is_resumable(log_root, obj.get("task_id", ""))
            entries[obj.get("task_id", "")] = obj

    # Directory fallback: resumable task dirs not covered by the index.
    root = Path(log_root)
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            if d.name in entries:
                continue
            if not _is_resumable(root, d.name):
                continue
            start = _first_event(d / "trace.jsonl", "task_start")
            if start is None:
                continue  # not a harness run dir
            data = start.get("data") or {}
            entries[d.name] = {
                "ts": d.stat().st_mtime,
                "task_id": d.name,
                "issue": (data.get("issue_text") or "")[:200],
                "repo": data.get("repo_path") or "",
                "status": "interrupted?",
                "resumable": True,
                "source": "dir-scan",
            }

    out = sorted(entries.values(), key=lambda e: e.get("ts", 0), reverse=True)
    return out[:limit]


def _is_resumable(log_root: Path, task_id: str) -> bool:
    """True when a run for task_id exists and has unfinished work.

    Uses ONLY public on-disk state (state.json + plan.json + trace.jsonl):
    completed steps present AND remaining steps present AND no final
    result event — i.e. exactly the state the harness's resume contract
    (config["resume"]=True + same task_id) can continue from.
    """
    if not task_id:
        return False
    d = Path(log_root) / task_id
    state_file = d / "state.json"
    plan_file = d / "plan.json"
    if not state_file.is_file() or not plan_file.is_file():
        return False
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    completed = state.get("completed_steps") or []
    remaining = state.get("remaining_plan") or []
    if not completed or not remaining:
        return False
    # a final result event means the run already finished
    trace = d / "trace.jsonl"
    if trace.is_file():
        try:
            for line in reversed(trace.read_text(encoding="utf-8").splitlines()):
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("kind") == "result":
                    return False
        except OSError:
            pass
    return True


def _resume_task(task_id: str, log_root: Path, state: Dict[str, Any]) -> None:
    """Continue a previous interactive run by task id (Task A).

    Rebuilds the Task from the run's own trace (task_start carries
    repo_path/issue_text/config verbatim — the harness's public
    observability surface) and re-enters _run_one_fix with
    config["resume"]=True, which makes harness.core.run_task continue
    from state.json + plan.json (skip completed steps, keep work/).
    """
    from shared.types import Task

    con = ui.console()
    d = Path(log_root) / task_id
    start = _first_event(d / "trace.jsonl", "task_start")
    if start is None:
        con.print(
            f"[vex.error]no run found for {task_id!r} under "
            f"{Path(log_root).resolve()}[/]"
        )
        return
    data = start.get("data") or {}
    repo = data.get("repo_path") or ""
    issue = data.get("issue_text") or ""
    if not Path(repo).is_dir():
        con.print(f"[vex.error]repo from the original run no longer exists: {repo}[/]")
        return
    prior_cfg = data.get("config") or {}
    cfg = {k: v for k, v in prior_cfg.items() if k != "api_key"}
    cfg["resume"] = True
    # session-state pins (model) override the prior run's config
    for key in ("model", "provider"):
        if state.get(key):
            cfg[key] = state[key]
    task = Task(task_id=task_id, repo_path=repo, issue_text=issue, config=cfg)

    # Environment snapshot restore (Round 8, Task C): if the original
    # run snapshotted its dep image (logs/{task_id}.runtime/env_snapshot
    # .json) and the image cache has since been pruned, restore it with
    # an O(1) retag so the resumed run doesn't rebuild the environment.
    # Best-effort: no snapshot / stale fingerprint / docker down just
    # means a normal lazy rebuild.
    try:
        from execution import env_snapshot as envs

        runtime_dir = Path(log_root) / f"{task_id}.runtime"
        restored = envs.restore_environment(task_id, repo, runtime_dir)
        if restored:
            ui.console().print(
                f"[vex.ok]environment snapshot restored "
                f"(image {restored.split(':')[-1]})[/]"
            )
    except Exception:
        pass

    _execute_task(task, log_root, preview=False)  # plan already approved once


def _first_event(trace_file: Path, kind: str) -> Optional[Dict[str, Any]]:
    """First event of a kind from a trace.jsonl (None when absent)."""
    if not trace_file.is_file():
        return None
    try:
        for line in trace_file.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("kind") == kind:
                return o
    except OSError:
        pass
    return None


def most_recent_resumable(log_root: Path) -> Optional[Dict[str, Any]]:
    """The newest session-index entry whose task is still resumable."""
    for s in list_sessions(log_root, limit=100):
        if s.get("resumable"):
            return s
    return None


def cmd_continue(log_root: Optional[Path] = None) -> int:
    """`vex --continue`: resume the most recent resumable session."""
    from cli.vexconfig import sessions_log_root

    con = ui.console()
    root = log_root or sessions_log_root()
    s = most_recent_resumable(root)
    if s is None:
        con.print(
            "[vex.muted]no resumable sessions under "
            f"{root.resolve()} — nothing to continue[/]"
        )
        con.print(
            "[vex.muted]start one with plain `vex`, or list with "
            "`vex --list-sessions`[/]"
        )
        return 1
    con.print(
        f"[vex.accent]resuming[/] [vex.muted]{s['task_id']} — "
        f"{s.get('issue', '')[:80]}[/]"
    )
    _resume_task(s["task_id"], root, {})
    return 0


def cmd_list_sessions(log_root: Optional[Path] = None) -> int:
    """`vex --list-sessions`: recent interactive runs, resumable marked."""
    from cli.vexconfig import sessions_log_root

    con = ui.console()
    root = log_root or sessions_log_root()
    sessions = list_sessions(root)
    if not sessions:
        con.print(f"[vex.muted]no recorded sessions under {root.resolve()}[/]")
        return 0
    con.print(
        f"[vex.accent]recent sessions[/] [vex.muted](newest first; "
        f"[vex.running]R[/][vex.muted] = resumable — "
        f"`vex --resume <task_id>`)[/]"
    )
    for s in sessions:
        mark = "[vex.running]R[/]" if s.get("resumable") else " "
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("ts", 0)))
        con.print(
            f"  {mark} [vex.accent]{s.get('task_id', '?')}[/] "
            f"[vex.muted]{t}  {s.get('status', '?'):8} "
            f"{(s.get('issue') or '')[:60]}[/]"
        )
    return 0


def cmd_resume(task_id: str, log_root: Optional[Path] = None) -> int:
    """`vex --resume <task_id>`: resume one session by id."""
    from cli.vexconfig import sessions_log_root

    root = log_root or sessions_log_root()
    _resume_task(task_id, root, {})
    return 0


# ---------------------------------------------------------------------------
# The interactive session
# ---------------------------------------------------------------------------

_BANNER = r"""
[vex.accent]    __      __       _    _       [/]
[vex.accent]    \ \    / /__ _ _| |__| |___ _ _ [/]
[vex.accent]     \ \/\/ / - _) '_| / _` / -_) '_|[/]
[vex.accent]      \___/\___\_|_|_\__,_\___|_|  [/]
[vex.muted]  the AI harness that fixes bugs — type what's wrong[/]
"""

_HELP = """[vex.accent]what you can say[/]
  [vex.muted]<anything>[/]          fix that bug — the sentence becomes the issue text
  [vex.accent]/status[/]            current/last task's structured state
  [vex.accent]/diff[/]              re-render the last fix's diff
  [vex.accent]/sessions[/]          list previous sessions (resumable marked)
  [vex.accent]/resume <task_id>[/]  continue an interrupted task
  [vex.accent]/approve[/]           approve a pending approval request
  [vex.accent]/reject[/]            reject a pending approval request
  [vex.accent]/cancel[/]            stop the current run cleanly (resumable)
  [vex.accent]/quiet[/]             toggle spinner verbosity
  [vex.accent]repo <path>[/]        switch the target repo (default: current dir)
  [vex.accent]model <name>[/]        pin a model for subsequent fixes
  [vex.accent]help[/] / [vex.accent]/help[/]  this text
  [vex.accent]exit[/] / Ctrl+D      quit
"""


def _detect_repo() -> Path:
    """Repo for the session default: CWD if it looks like a repo, else CWD."""
    cwd = Path.cwd().resolve()
    if (
        (cwd / ".git").exists()
        or list(cwd.glob("pyproject.toml"))
        or list(cwd.glob("setup.py"))
        or list(cwd.glob("requirements.txt"))
    ):
        return cwd
    return cwd


def _looks_like_path(text: str) -> bool:
    t = text.strip()
    return (
        t.startswith(("./", "../", "/"))
        or (len(t) > 1 and t[1] == ":" and t[2] in "\\/")
        or (Path(t).exists() and " " not in t)
    )


def run_interactive(argv_quote: str = "", log_root: Optional[Path] = None) -> int:
    """The `vex` no-args session. Returns a process exit code (0/130).

    Assumes stdin is an interactive terminal (the CLI dispatches here only
    when sys.stdin.isatty() and no subcommand was given; scripted callers
    use the flag commands).
    """
    con = ui.console()
    con.print(_BANNER)
    repo = _detect_repo()
    log_root = log_root or Path("logs")
    con.print(f"[vex.muted]repo:  {repo}[/]")
    con.print(f"[vex.muted]logs:  {Path(log_root).resolve()}[/]")
    con.print(
        f"[vex.muted]type [vex.accent]help[/][vex.muted] for commands — "
        "plain language for everything else[/]"
    )
    con.print()

    state: Dict[str, Any] = {
        "model": None,
        "provider": None,
        "plan_preview": None,
        "quiet": False,
    }
    last: Dict[str, Any] = {}
    from cli.vexconfig import load_vex_config

    file_config = load_vex_config()
    if file_config.get("log_verbosity") == "quiet":
        state["quiet"] = True
    if file_config.get("plan_preview") is not None:
        state["plan_preview"] = bool(file_config["plan_preview"])

    while True:
        try:
            con.print(f"[vex.accent]vex {ui.GLYPHS['prompt']}[/] ", end="")
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            con.print("\n[vex.muted]bye[/]")
            return 0
        if not line:
            continue
        low = line.lower()

        # -- slash commands (Task B) -----------------------------------
        if low.startswith("/"):
            handled = _slash_command(line, low, last, log_root, state)
            if handled == "continue":
                continue
            if handled is None:
                continue
            # unknown slash command: hint (the message itself is printed
            # by _slash_command; this branch just stops processing)
            continue

        # -- session commands (bare words kept from the first pass) ----
        if low in ("exit", "quit", "q"):
            con.print("[vex.muted]bye[/]")
            return 0
        if low in ("help", "?"):
            con.print(_HELP)
            continue
        if (
            low.startswith("repo ")
            or _looks_like_path(low)
            and not low.startswith(("fix", "in"))
        ):
            path = line[5:].strip() if low.startswith("repo ") else line
            cand = Path(path).expanduser().resolve()
            if cand.is_dir():
                repo = cand
                con.print(f"[vex.ok]repo {ui.GLYPHS['arrow']} {repo}[/]")
            else:
                con.print(f"[vex.error]not a directory: {cand}[/]")
            continue
        if low.startswith("model "):
            state["model"] = line[6:].strip()
            con.print(f"[vex.ok]model pinned {ui.GLYPHS['arrow']} {state['model']}[/]")
            continue

        # -- a fix request: the sentence IS the issue text ----------------
        try:
            result_info = _run_one_fix(line, repo, state, log_root, file_config)
        except KeyboardInterrupt:
            # Task A: interrupted runs are RESUMABLE — record the partial
            # task in the session index so --continue //sessions find it.
            con.print(
                "\n[vex.warn]interrupted — containers cleaned, "
                "checkpoints kept ([vex.accent]vex --continue[/]"
                "[vex.warn] resumes)[/]"
            )
            continue
        except Exception as exc:  # never dump a traceback on the user
            # Task D: plain-language explanation (what broke + what to
            # check) instead of a bare exception line — the interactive
            # session then continues.
            from cli.errors import explain_exception

            explain_exception(exc, save_traceback=True)
            continue
        if result_info is None:
            continue
        last = result_info
    return 0


def _decide_pending(log_root: Path, task_id: str, approve: bool) -> Optional[bool]:
    """Decide a pending approval request for task_id via the existing
    file protocol (runtime.approval.decide writes decision.json).

    Interactive `vex fix --approval` and benchmark subsets park the
    worker in the gate; /approve //reject answer from the session.
    Returns True/False (decided) or None (no pending request). The gate
    dir layout is logs/{task_id}.runtime/approval (runtime's sibling
    layout — worker-owned, outside logs/{task_id}/ which the harness
    archives on relaunch).
    """
    con = ui.console()
    try:
        from runtime import approval as approval_mod
    except ImportError:
        con.print("[vex.error]runtime.approval unavailable[/]")
        return None
    gate = Path(log_root) / f"{task_id}.runtime" / "approval"
    req = approval_mod.pending_request(str(gate))
    if req is None:
        return None
    if not req.get("diff") and req.get("summary"):
        pass  # some requests carry only a summary
    approval_mod.decide(str(gate), approve=approve)
    con.print(
        f"[vex.{'ok' if approve else 'error'}]"
        f"{'approved' if approve else 'rejected'}[/] — the worker "
        f"continues {'with' if approve else 'without'} the fix"
    )
    return approve


def _slash_command(
    line: str, low: str, last: Dict[str, Any], log_root: Path, state: Dict[str, Any]
):
    """Dispatch one /command (Task B). Returns "continue" (handled),
    None (handled, no further processing), or "unknown".
    Kept small and genuinely useful per the brief — anything else goes
    to cli/AGENTS.md's future-work list instead of here."""
    con = ui.console()
    cmd = low.split()[0]

    if cmd in ("/help",):
        con.print(_HELP)
        return None

    if cmd in ("/status",):
        target = last.get("task_id")
        if not target:
            con.print("[vex.muted]no run in this session yet[/]")
            return None
        from cli.main import cmd_status  # late import: avoid a cycle
        import argparse as _ap

        ns = _ap.Namespace(
            task_id=target, log_root=str(last.get("log_root") or log_root)
        )
        cmd_status(ns)
        return None

    if cmd in ("/diff",):
        if last.get("diff"):
            ui.print_diff(last["diff"])
        else:
            con.print("[vex.muted]no diff from the last run[/]")
        return None

    if cmd in ("/sessions",):
        cmd_list_sessions(log_root)
        return None

    if cmd in ("/resume",):
        parts = line.split()
        if len(parts) < 2:
            con.print("[vex.muted]usage: /resume <task_id> (see /sessions)[/]")
            return None
        try:
            _resume_task(parts[1], log_root, state)
        except KeyboardInterrupt:
            con.print(
                "\n[vex.warn]interrupted — resumable via "
                "[vex.accent]vex --continue[/][/]"
            )
        except Exception as exc:
            # Task D: plain-language explanation (bad task id, unreadable
            # state, sandbox down...) — plus the id that was asked for.
            from cli.errors import explain_exception

            con.print(f"[vex.error]resume of {parts[1]!r} failed:[/]")
            explain_exception(exc)
        return None

    if cmd in ("/approve", "/reject"):
        parts = line.split()
        tid = parts[1] if len(parts) > 1 else last.get("task_id")
        if not tid:
            con.print(
                "[vex.muted]no task to decide on — run with "
                "--approval or approve from a benchmark[/]"
            )
            return None
        decided = _decide_pending(log_root, tid, approve=(cmd == "/approve"))
        if decided is None:
            con.print(f"[vex.muted]no pending approval request for {tid}[/]")
        return None

    if cmd in ("/cancel",):
        con.print(
            "[vex.warn]cancel requested — sending Ctrl+C semantics "
            "to the running task (checkpoints kept)[/]"
        )
        _interrupt_main()
        return None

    if cmd in ("/quiet",):
        state["quiet"] = not state.get("quiet", False)
        con.print(f"[vex.ok]verbosity: {'quiet' if state['quiet'] else 'normal'}[/]")
        return None

    con.print(
        f"[vex.error]unknown command: {line.split()[0]}[/] [vex.muted]— try /help[/]"
    )
    return "unknown"


def _run_one_fix(
    issue: str,
    repo: Path,
    state: Dict[str, Any],
    log_root: Path,
    file_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Run one NEW fix from the interactive session; live-monitor the trace.

    Returns {"task_id", "log_root", "diff", "status"} for the session's
    `status`/`diff` commands, or None when the run couldn't start.
    Config file defaults (Task C) fill keys the session didn't set;
    plan preview (Task D) is honored from file config when the session
    state didn't decide it.
    """
    import uuid

    from shared.types import Task

    from cli.vexconfig import apply_config_defaults

    con = ui.console()
    task_id = f"fix-{uuid.uuid4().hex[:8]}"
    # Session-explicit values win (None = unset, falls to file config);
    # apply_config_defaults fills gaps from ~/.vex/config.toml (Task C).
    config = apply_config_defaults(
        {
            k: v
            for k, v in {
                "model": state.get("model"),
                "provider": state.get("provider"),
                "plan_preview": state.get("plan_preview"),
            }.items()
            if v is not None
        },
        file_config,
    )
    task = Task(task_id=task_id, repo_path=str(repo), issue_text=issue, config=config)
    try:
        result_info = _execute_task(
            task,
            log_root,
            preview=bool(config.get("plan_preview")),
            issue_for_index=issue,
        )
    except KeyboardInterrupt:
        # Task A: an interrupted run IS a session (state.json + plan.json
        # survive; --continue finishes it). Record, then re-raise so the
        # caller prints its interrupt message.
        record_session(log_root, task_id, issue, str(repo), "interrupted")
        raise
    if result_info is not None:
        record_session(log_root, task_id, issue, str(repo), result_info["status"])
    return result_info


def _execute_task(
    task: "Task",
    log_root: Path,
    preview: bool = False,
    issue_for_index: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Shared run-and-report core for new fixes AND resumed runs.

    Live-monitors the trace, renders the result summary (status/cost/
    verification chips/diff/rationale), and applies the plan preview
    (Task D) when `preview` is set: a watcher thread renders the plan
    event (the harness's public decomposition) and prompts BEFORE any
    edit can happen — approval continues the run, rejection cancels it
    gracefully (KeyboardInterrupt into run_task, which keeps checkpoints).
    The preview reuses the approval-gate CONFIRMATION semantics but is
    CLI-local (the harness's own approval protocol gates the final diff
    at the worker level and stays unchanged).

    Returns the session-state dict or None when the run couldn't start.
    """
    from cli import deps
    from cli.main import _clear_router_context, _set_router_context

    con = ui.console()
    task_id = task.task_id
    repo_name = Path(task.repo_path).name

    con.print(
        f"[vex.muted]{ui.GLYPHS['arrow']} fixing in "
        f"[vex.accent]{repo_name}[/] [vex.muted](task {task_id})[/]"
    )
    _set_router_context(task)
    preview_cancel = threading.Event()
    preview_thread: Optional[threading.Thread] = None
    if preview:
        preview_thread = threading.Thread(
            target=_plan_preview_watch,
            args=(log_root, task_id, preview_cancel),
            daemon=True,
        )
        preview_thread.start()
    mon = LiveMonitor(task_id, log_root).start()
    run_task = deps.get_run_task()
    t0 = time.time()
    try:
        result = run_task(task, log_root=Path(log_root))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        mon.stop()
        # Task D: plain-language explanation instead of a bare line.
        from cli.errors import explain_exception

        explain_exception(exc)
        return None
    finally:
        _clear_router_context()
        mon.stop()
        if preview_thread is not None:
            preview_cancel.set()
            preview_thread.join(timeout=2.0)
    elapsed = time.time() - t0

    style = "vex.ok" if result.status == "success" else "vex.error"
    con.print(
        f"[{style}]{ui.GLYPHS['bullet']} {result.status.upper()}[/] "
        f"[vex.muted]{result.attempts} attempt(s), "
        f"{len(result.model_calls)} model call(s), "
        f"{ui.fmt_cost(result.cost_usd)}, {elapsed:.0f}s[/]"
    )
    if result.verification is not None:
        v = result.verification
        con.print(
            f"[vex.muted]target test: "
            f"[{'vex.ok' if v.target_test_passed else 'vex.error'}]"
            f"{'PASS' if v.target_test_passed else 'FAIL'}[/]  "
            f"regression: "
            f"[{'vex.ok' if v.regression_passed else 'vex.error'}]"
            f"{'PASS' if v.regression_passed else 'FAIL'}[/]  "
            f"flaky: {v.flaky}[/]"
        )
    if result.diff:
        con.print("[vex.muted]diff:[/]")
        ui.print_diff(result.diff)
    # rationale.md (Task E): render the grounded paragraph if written
    rat = Path(log_root) / task_id / "rationale.md"
    if rat.is_file():
        from rich.markdown import Markdown

        con.print()
        con.print(Markdown(rat.read_text(encoding="utf-8")))
    con.print(
        f"[vex.muted]trace: {Path(log_root).resolve() / task_id / 'trace.jsonl'}[/]"
    )
    con.print(
        "[vex.muted]type [vex.accent]status[/][vex.muted] for the plan "
        "checklist, [vex.accent]diff[/] to re-render[/]"
    )
    return {
        "task_id": task_id,
        "log_root": log_root,
        "diff": result.diff,
        "status": result.status,
    }


def _plan_preview_watch(
    log_root: Path, task_id: str, cancel: threading.Event, poll_s: float = 0.25
) -> None:
    """Task D: render the plan and ask before edits start.

    Watches the task's trace for the FIRST `plan` event (the harness's
    public decomposition: [{id, description, checkpoint}, ...]), renders
    it as a numbered list, and prompts. approve -> return (run proceeds
    into the edit phase); reject/timeout -> raise KeyboardInterrupt into
    the blocking run via the main thread's interrupt — simplest reliable
    cross-thread cancellation for a blocking call that already handles
    Ctrl+C by keeping checkpoints (the resume backend Task A exposes).

    The prompt window closes automatically if the run finishes before
    an answer (cancel set by the caller).
    """
    con = ui.console()
    trace = Path(log_root) / task_id / "trace.jsonl"
    # wait for the plan event (or cancellation)
    deadline = time.time() + 600  # generous: planning includes a model call
    plan_event = None
    while not cancel.is_set() and time.time() < deadline:
        plan_event = _first_kind_after(trace, "plan")
        if plan_event is not None:
            break
        time.sleep(poll_s)
    if cancel.is_set() or plan_event is None:
        return
    plan = (plan_event.get("data") or {}).get("plan") or []
    con.print()
    con.rule(f"[vex.accent]plan preview — {task_id}[/]")
    for step in plan:
        sid = step.get("id", "?")
        desc = step.get("description", "")
        checkpoint = step.get("checkpoint", "")
        line = f"  [vex.accent]{sid}.[/] {desc}"
        if checkpoint:
            line += f" [vex.muted](done when: {checkpoint})[/]"
        con.print(line)
    try:
        answer = input("run this plan? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("", "y", "yes"):
        con.print("[vex.ok]approved — starting edits[/]")
        return
    con.print(
        "[vex.warn]rejected — cancelling (checkpoints kept; "
        "`vex --continue` resumes this task)[/]"
    )
    # cancel the blocking run_task: interrupt the MAIN thread the same
    # way Ctrl+C does — run_task's KeyboardInterrupt path keeps state.
    _interrupt_main()


def _first_kind_after(trace_file: Path, kind: str) -> Optional[Dict[str, Any]]:
    """First event of a kind from a trace (scan from start; plan events
    precede edits)."""
    if not trace_file.is_file():
        return None
    try:
        for line in trace_file.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("kind") == kind:
                return o
    except OSError:
        pass
    return None


def _interrupt_main() -> None:
    """Deliver Ctrl+C semantics to the main thread (plan rejection).

    signal.raise_signal(SIGINT) reaches the MAIN thread's interrupt
    handling (Python only runs signal handlers on the main thread) —
    exactly what a human pressing Ctrl+C does. run_task's
    KeyboardInterrupt path then fires: containers stop, checkpoints
    stay on disk, the task is resumable via `vex --continue`.
    On any failure to signal, we degrade gracefully: the run is left
    to finish normally (the user was warned by the reject message).
    """
    import signal

    try:
        signal.raise_signal(signal.SIGINT)
    except (ValueError, OSError, AttributeError):
        pass
