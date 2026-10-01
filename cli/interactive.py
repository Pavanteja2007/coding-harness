"""Vex interactive natural-language mode â€” the PRIMARY user experience.

`vex` with no arguments drops into a session where the user types plain
language ("fix the login bug where the password is empty") and the harness
loop starts from that sentence â€” no flags required (Claude Code / Codex
style). The repo is inferred from the current working directory; the
session can switch repos, run multiple fixes, and shows live progress.

Flag-based commands (vex fix / run-benchmark / ...) remain the scriptable
automation path â€” Task D of the Vex CLI pass.

Live monitoring design (Tasks C+E): run_task is a blocking call whose
internals live in another module's objects â€” we do NOT reach into them.
The harness's own trace.jsonl (logs/{task_id}/) is the public observability
surface (documented in harness/AGENTS.md + INTERFACES.md): a monitor thread
tails it, derives phase/cost/steps from the event kinds, and renders a
rich Status spinner + running cost + last event, updated live.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from rich.markup import escape

if TYPE_CHECKING:
    from shared.types import Task

from cli import commands as _commands
from cli import ui
from cli.exit_codes import EXIT_CODES

#: The one numeric meaning of "the REPL refused this command". Declared
#: once here because the file uses it in every refusal branch, and it is
#: the same value the shared registry and the headless adapter use.
EXIT_CODES_USAGE = EXIT_CODES["usage_error"]

#: What the dispatcher returns when a command means "end the session".
#: A named constant because `None` already means "handled, nothing more to
#: do" and `"continue"` means "restart the loop", so an exit cannot be
#: spelled by reusing either.
_COMMAND_EXIT = "vex:exit"

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
    "steering": "steered: applying your instruction",
    "steering_abort": "steered: aborting at the next checkpoint",
    "steering_replan": "steered: re-planning around your instruction",
    "steering_step_yield": "steered: yielding step to the task level",
    "recall": "recalling compacted context",
    "git_output": "producing git branch/commit",
    "rationale": "writing rationale",
    "task_end": "finishing",
    "run_started": "starting",
    "run_finished": "finishing",
    "strategy_selected": "selecting strategy",
    "context_built": "assembling context",
    "context": "assembling context",
    "session_context": "loading session context",
    "checkpoint_saved": "saving checkpoint",
    "permission_decision": "checking permissions",
    "approval_denied": "approval denied",
    "input_requested": "waiting for input",
    "verification": "verifier: running tests",
    "completion_decision": "deciding completion",
    "cancellation_requested": "cancelling",
    "diagnostics": "checking diagnostics",
    "turn_started": "turn: starting",
    "model_recovery": "model: retrying",
    "tool_recovery": "recovering tool call",
    "checkpoint_warning": "checkpoint warning",
    "context_warning": "context warning",
    "approval_error": "approval error",
    "run_error": "run error",
}

# Events whose data carries step/attempt context for the label.
_LABEL_FIELDS: Dict[str, List[str]] = {
    "model_request": ["step"],
    "model_response": ["step"],
    "attempt_start": ["attempt"],
    "verify": ["phase"],
    "turn_started": ["turn"],
}

# Embedded-UI hooks (full-screen TUI round, 2026-09-13). cli.tui sets
# these at app mount; the rich REPL leaves them None. They let the TUI
# (a) attach its live run-line the moment a task id exists (before any
# trace event can land) and (b) redirect /cancel's interrupt at the
# TUI's worker thread instead of SIGINT-at-main (in the TUI, the main
# thread is textual's event loop â€” SIGINT there would kill the app).
# Both are optional callables; firing them is wrapped in try/except so
# a broken UI hook can never take a run down.
_ON_TASK_START: Optional[Any] = None  # callable(task_id: str) -> None
_CANCEL_RUN: Optional[Any] = None  # callable() -> None (no args)
# Fired right before a blocking prompt (plan preview, approval gate):
# callable(prompt: str, body_lines: List[str]) -> None. The embedded UI
# (cli.tui) uses it to render the prompt's CONTEXT (the plan steps / the
# diff) in its modal body â€” the plain REPL leaves it unset, and the
# console prints below remain the source of truth for the transcript.
_PROMPT_BODY: Optional[Any] = None

# ---------------------------------------------------------------------------
# Mid-task steering (steering round) â€” live-run registration + the REPL's
# stdin-owning reader thread
# ---------------------------------------------------------------------------

# The CURRENTLY-RUNNING harness task (fix/build/resume â€” the long,
# loop-driving modes; question/research are short read-only calls and
# deliberately not registered). The REPL's reader thread consults this
# to know where to inject steering; None when no run is live.
_LIVE_RUN_LOCK = threading.Lock()
_LIVE_RUN: Dict[str, Any] = {"task_id": None, "log_root": None}


def _set_live_run(task_id: str, log_root: Path) -> None:
    """Mark a harness task as live (called by _execute_task/_run_one_build
    around the blocking run). Assumes task_id/log_root belong to the run
    being started; a second concurrent registration overwrites (the
    session runs one task at a time by design)."""
    with _LIVE_RUN_LOCK:
        _LIVE_RUN["task_id"] = task_id
        _LIVE_RUN["log_root"] = Path(log_root)


def _clear_live_run() -> None:
    """Clear the live-run registration (run finished/interrupted)."""
    with _LIVE_RUN_LOCK:
        _LIVE_RUN["task_id"] = None
        _LIVE_RUN["log_root"] = None


def _live_run() -> Optional[Dict[str, Any]]:
    """A COPY of the live-run registration, or None when idle."""
    with _LIVE_RUN_LOCK:
        if _LIVE_RUN["task_id"] is None:
            return None
        return {
            "task_id": _LIVE_RUN["task_id"],
            "log_root": _LIVE_RUN["log_root"],
        }


def steer_live_run(
    line: str,
    task_id: str,
    log_root: Path,
    source: str = "repl",
    say: Optional[Any] = None,
    queue_only: bool = False,
) -> Optional[str]:
    """Inject one mid-run user line as steering into the LIVE task.

    Returns an ack string for the user, or None when refused:
    - conversational input (cli.intent: greetings/chit-chat) is NEVER
      injected â€” steering must not pollute a task with small talk;
      the ack says so (cost asymmetry: a wrong injection burns a
      verifier cycle, a skipped one costs one line).
    - steering disabled in the merged config -> honest refusal.
    - the buffer's pending cap -> honest refusal (inject() contract).

    ``queue_only`` (VEX-CEILING-10) is the "queue without interrupting"
    half of the steering contract: the instruction is journaled the same
    way, but an ``abort`` intent is downgraded to a plain guide so queuing
    can never stop the run. The loop delivers queued steering at its next
    safe boundary in journal order, after any instruction already queued,
    so a burst of corrections arrives in the order the user typed them.

    `say` (optional) renders the ack lines â€” the REPL passes nothing
    (console print, its idiom); the TUI passes a transcript renderer
    (its console output is captured away from the user, so the ack
    must land in the transcript instead). Assumes task_id/log_root
    identify the run currently inside run_task (the harness's
    SteeringBuffer for that task lives at log_root/task_id/
    steering.jsonl â€” same journal the loop polls).
    """
    from cli.intent import classify

    def _say(markup: str) -> None:
        if say is not None:
            say(markup)
        else:
            ui.console().print(markup)

    if classify(line).kind == "convo":
        return None  # caller answers inline; never steer small talk

    from harness import steering as steering_mod

    # The loop must actually be live: run_task's _fresh_paths archives
    # a pre-existing log dir on a FRESH start, so a journal written
    # BEFORE the loop starts (e.g. during a build's stage-1 acceptance-
    # test authoring) would be archived with the dir and the
    # instruction silently lost â€” refuse honestly instead. A RESUMED
    # run keeps its prior trace (the dir is never archived on resume),
    # so steering right after a resume correctly passes this gate and
    # the loop's freshly constructed buffer replays it.
    if not (Path(log_root) / task_id / "trace.jsonl").is_file():
        _say(
            "[vex.muted]the run is still starting (before the fix loop) "
            "â€” steering applies once the loop is live; send it again in "
            "a moment[/]"
        )
        return "starting"

    buf = steering_mod.SteeringBuffer(Path(log_root) / task_id, task_id)
    intent, text = steering_mod.parse_steering_line(line)
    if queue_only and intent == "abort":
        # Queuing must never be able to stop the run. A user who wants to
        # stop the run uses /cancel; a queued "abort" is delivered as an
        # ordinary instruction and says so.
        intent = "guide"
        _say(
            "[vex.warn]queued, not aborted[/] [vex.muted](queuing cannot stop "
            "a run â€” use [vex.accent]/cancel[/] to stop it)[/]"
        )
    ev = buf.inject(text, intent=intent, source=source)
    if ev is None:
        _say(
            "[vex.warn]steering refused[/] [vex.muted]â€” the task's steering "
            "queue is full or steering is disabled for this run[/]"
        )
        return "refused"
    if ev.intent == "abort":
        _say(
            f"[vex.warn]abort requested[/] [vex.muted]({ev.seq}) â€” the task "
            "stops cleanly at its next checkpoint (resumable via "
            "[vex.accent]vex --resume[/][vex.muted])[/]"
        )
    elif ev.intent == "replan":
        _say(
            f"[vex.ok]steered (re-plan)[/] [vex.muted]({ev.seq}) â€” the task "
            "re-plans at the next step boundary, keeping work done so far[/]"
        )
    else:
        _say(
            f"[vex.ok]steered[/] [vex.muted]({ev.seq}) â€” applies at the next "
            "checkpoint (between commands); work so far is kept[/]"
        )
    return ev.intent


def inject_async_interrupt(thread_ident: int) -> bool:
    """Raise KeyboardInterrupt in a SPECIFIC thread (the TUI's proven
    ctypes pattern, reused by the REPL's reader for mid-run /cancel).

    The REPL's run_task blocks the MAIN thread; a reader thread cannot
    signal.raise_signal(SIGINT) at it (Python runs signal handlers on
    the main thread only). PyThreadState_SetAsyncExc delivers the KI
    at the target's next bytecode boundary. IDENT-SAFETY: the caller
    must pass a LIVE thread's ident (idents get recycled â€” check
    is_alive() immediately before the call). Returns True on delivery.
    """
    import ctypes

    try:
        target = None
        for th in threading.enumerate():
            if th.ident == thread_ident and th.is_alive():
                target = th
                break
        if target is None:
            return False
        ret = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(thread_ident),
            ctypes.py_object(KeyboardInterrupt),
        )
        return ret == 1
    except Exception:
        return False


_EOF = object()  # sentinel: the reader saw EOF/leave


class _ReplReader:
    """The REPL's stdin owner (steering round, Task A).

    Before steering, the REPL's main thread called input() directly â€”
    so while a run was live NOTHING could be typed (mid-task steering
    impossible from the REPL without this restructure). One daemon
    thread owns stdin for the whole session and hands each line to
    the main loop via a queue; while a harness run is live, lines are
    consumed HERE instead (steering injected into the live task,
    slash commands dispatched, conversational input answered inline).

    Ctrl+C semantics are preserved: SIGINT is delivered to the MAIN
    thread (inside the blocking run_task â€” its KeyboardInterrupt path
    keeps checkpoints); the reader's own blocked input() aborts on
    Windows too, which the reader swallows when a run is live (the
    interrupt was for the run; the session continues) and treats as
    leave when idle (the previous at-prompt behavior).
    """

    def __init__(
        self, main_ident: int, state: Optional[Dict[str, Any]] = None
    ) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._main_ident = main_ident
        self._state = state
        self._dead = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def getline(self) -> str:
        """Main-thread API: next session line; EOFError when the reader
        died (EOF / interrupt-at-idle)."""
        item = self._queue.get()
        if item is _EOF:
            raise EOFError from None
        return str(item)

    def _loop(self) -> None:
        while not self._dead:
            try:
                line = input()
            except EOFError:
                self._queue.put(_EOF)
                return
            except KeyboardInterrupt:
                # A console Ctrl+C hits every blocked reader on Windows.
                # If a run is live, the interrupt was FOR the run (the
                # main thread's run_task handles it: checkpoints kept);
                # keep the session alive and keep reading. Idle = the
                # old at-prompt Ctrl+C = leave.
                if _live_run() is not None:
                    continue
                self._queue.put(_EOF)
                return
            if not line.strip():
                continue
            try:
                if self._handle_live(line):
                    continue
            except Exception:
                pass  # reader-side handling must never kill the session
            self._queue.put(line)

    def _handle_live(self, line: str) -> bool:
        """Consume a line while a run is live. Returns True when fully
        handled here (steering/ slash/ conversational); False when the
        main loop should process it normally (run just ended, or a
        line typed during a short non-steerable call)."""
        live = _live_run()
        if live is None:
            return False
        low = line.strip().lower()

        if low.startswith("/"):
            task_dir = Path(live["log_root"]) / live["task_id"]
            snapshot: Dict[str, Any] = {}
            try:
                from cli.runview import read_live_projection

                snapshot = read_live_projection(task_dir)
            except Exception:
                snapshot = {}
            pending = _pending_approval_request(Path(live["log_root"]), live["task_id"])
            context = _commands.surface_command_context(
                "repl",
                in_flight=True,
                snapshot=snapshot,
                task_id=str(live["task_id"]),
                pending_approval=pending is not None,
            )
            # THE ALIAS SEAM, MOUNTED — the READER THREAD's own call.
            # It has its own resolution call (it answers `/status` and
            # `/sessions` mid-run), and a seam mounted in only one of two
            # resolution paths is a seam that answers differently depending
            # on which thread the user happened to type into.
            from cli import command_aliases as _aliases

            resolution = _aliases.resolve_line(line, context)
            if resolution.spec is not None and resolution.status != "ok":
                con = ui.console()
                con.print(
                    f"[vex.warn]{escape(resolution.spec.name)} unavailable: "
                    f"{escape(resolution.message)}[/]"
                )
                con.print(
                    f"[vex.muted]{escape(_commands.command_usage(resolution.spec))}[/]"
                )
                con.print(
                    f"[vex.muted]{escape(_commands.command_recovery_hint(resolution.spec))}[/]"
                )
                return True
            if resolution.spec is None:
                ui.console().print(
                    "[vex.warn]a custom command cannot start while a run is "
                    "active â€” /cancel first, or retype when it finishes[/]"
                )
                return True
            spec = resolution.spec
            cmd = spec.name
            canonical = f"{cmd} {resolution.args}".rstrip()
            if cmd == "/cancel":
                con = ui.console()
                con.print(
                    "[vex.warn]cancel requested â€” sending Ctrl+C semantics "
                    "to the running task (checkpoints kept)[/]"
                )
                if not inject_async_interrupt(self._main_ident):
                    con.print(
                        "[vex.warn](main thread unreachable â€” press Ctrl+C again)[/]"
                    )
                return True
            if cmd in ("/approve", "/reject"):
                values = resolution.args.split()
                scope_words = {"once", "session", "path", "command", "y", "s", "p", "c"}
                scope = values[0] if values and values[0].lower() in scope_words else (
                    values[1] if len(values) > 1 else "once"
                )
                state = self._state if isinstance(self._state, dict) else {}
                policy = state.get("approval_policy")
                if not isinstance(policy, _commands.ApprovalPolicy):
                    policy = _commands.session_approval_policy()
                    state["approval_policy"] = policy
                decided = _decide_pending(
                    Path(live["log_root"]),
                    live["task_id"],
                    approve=(cmd == "/approve"),
                    scope=scope,
                    policy=policy,
                )
                if decided is None:
                    ui.console().print(
                        f"[vex.muted]no pending approval request for "
                        f"{live['task_id']}[/]"
                    )
                return True
            if cmd == "/quiet":
                _reader_toggle_quiet()
                return True
            if cmd == "/steer":
                if not resolution.args.strip():
                    ui.console().print(
                        "[vex.muted]usage: /steer <instruction> (plain text "
                        "while a run is live does the same)[/]"
                    )
                    return True
                steer_live_run(
                    resolution.args, live["task_id"], Path(live["log_root"])
                )
                return True
            if cmd == "/quit":
                ui.console().print(
                    "[vex.warn]/cancel the active run before /quit[/]"
                )
                return True
            if spec.result_presentation in {"browser", "card", "diff", "settings"}:
                _reader_slash_render(cmd, live, canonical)
                return True
            ui.console().print(
                f"[vex.warn]{escape(cmd)} is unavailable while a run is active â€” "
                "wait or /cancel[/]"
            )
            return True

        # Conversational input is answered inline, never injected.
        from cli.intent import classify

        intent = classify(line)
        if intent.kind == "convo":
            ui.console().print(f"[vex.muted]{intent.reply}[/]")
            return True

        # Everything else steers the live task (the feature).
        steer_live_run(line, live["task_id"], Path(live["log_root"]), source="repl")
        return True


def _fire_task_start(task_id: str) -> None:
    """Notify the embedded UI (if any) that a task's trace is starting."""
    try:
        if _ON_TASK_START is not None:
            _ON_TASK_START(task_id)
    except Exception:
        pass


def _fire_prompt_body(prompt: str, body_lines: List[str]) -> None:
    """Give the embedded UI the rendered context for a blocking prompt."""
    try:
        if _PROMPT_BODY is not None:
            _PROMPT_BODY(prompt, list(body_lines))
    except Exception:
        pass


# The session's quiet flag, shared with the reader thread (its /quiet
# answers mid-run). A plain module global on purpose: the REPL keeps a
# session-scoped flag in `state`, and the reader has no state ref â€”
# ONE toggle both surfaces can see.
_READER_QUIET = {"quiet": False}


def _reader_toggle_quiet() -> None:
    """Reader-side /quiet: toggle + render (best-effort, reader thread)."""
    _READER_QUIET["quiet"] = not _READER_QUIET["quiet"]
    ui.console().print(
        f"[vex.ok]verbosity: {'quiet' if _READER_QUIET['quiet'] else 'normal'}[/]"
    )


def _reader_slash_render(cmd: str, live: Dict[str, Any], line: str = "") -> None:
    """Reader-side render-only slash answers (/status /diff /sessions
    /trace /feed) while a run is live. Best-effort by contract: these
    read the PUBLIC log files only (state.json/trace.jsonl â€” never
    run_task internals); any failure prints one honest line, never
    raises. `line` carries the raw input so /sessions and /feed can
    take a filter query mid-run.
    """
    con = ui.console()
    arg = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
    try:
        log_root = Path(live["log_root"])
        if cmd == "/files":
            try:
                from cli import fileview

                rows = fileview.file_picker_rows(_detect_repo(), arg, limit=40)
                if not rows:
                    con.print("[vex.muted]no repository files or symbols match[/]")
                for row in rows[:40]:
                    label = row.get("qualified") or row.get("path") or row.get("label") or ""
                    con.print(f"  [vex.accent2]{escape(str(label))}[/]")
            except Exception:
                con.print("[vex.muted]file browser unavailable[/]")
        elif cmd == "/checkpoints":
            try:
                records = _checkpoint_lines(log_root, live["task_id"], _detect_repo())
                con.print(f"[vex.accent]checkpoints[/] [vex.muted]({len(records)})[/]")
                for record in records[:20]:
                    label = record.get("checkpoint_id") or record.get("resume_token") or "checkpoint"
                    con.print(f"  [vex.muted]{escape(str(label))}[/]")
            except Exception:
                con.print("[vex.muted]checkpoints unavailable[/]")
        elif cmd == "/context":
            try:
                from cli.runview import read_live_projection

                issue = str(read_live_projection(log_root / live["task_id"]).get("issue") or "")
                for row in context_lines(_detect_repo(), issue):
                    con.print(row)
            except Exception:
                con.print("[vex.muted]context unavailable[/]")
        elif cmd == "/status":
            from cli.runview import read_live_projection, status_lines

            task_dir = log_root / live["task_id"]


            snapshot = read_live_projection(task_dir, mode="agent_task")
            for row in status_lines(snapshot, mode="agent_task", live=True):
                con.print(row)
        elif cmd == "/diff":
            from cli.tracelog import live_diff

            task_dir = log_root / live["task_id"]
            lines = live_diff(
                task_dir / "pristine",
                task_dir / "work",
                max_lines=20,
            )
            if not lines and task_dir.joinpath("pristine").is_dir():
                try:
                    from harness.agent_loop import agent_diff

                    start = _first_event(task_dir / "trace.jsonl", "task_start")
                    data = (start or {}).get("data") or {}
                    repo = str(data.get("repo_path") or "")
                    diff = agent_diff(live["task_id"], log_root, repo) if repo else ""
                    if diff:
                        lines = [(line, "ctx") for line in diff.splitlines()]
                except Exception:
                    lines = None
            if not lines:
                say_empty_state(con.print, "no_diff")
                return
            for text in ui.diff_render_lines(lines):
                con.print(text)
        elif cmd == "/review":
            _reader_slash_render("/diff", live, line)
            con.print("[vex.muted]review uses the live diff; rationale appears when available[/]")
        elif cmd == "/copy-diff":
            try:
                from cli.session import copy_text_to_clipboard
                from harness.agent_loop import agent_diff

                task_dir = log_root / live["task_id"]
                start = _first_event(task_dir / "trace.jsonl", "task_start")
                data = (start or {}).get("data") or {}
                diff = agent_diff(
                    live["task_id"], log_root, str(data.get("repo_path") or "")
                )
                if diff and copy_text_to_clipboard(diff):
                    con.print("[vex.ok]live diff copied[/]")
                else:
                    con.print("[vex.muted]no live diff to copy[/]")
            except Exception:
                con.print("[vex.muted]live diff unavailable[/]")
        elif cmd in ("/sessions", "/feed", "/trace"):
            if cmd == "/sessions":
                # The reader thread only runs inside a real interactive
                # session, which is never the headless-scoped case, so the
                # cross-repo index is the correct read model here.
                _print_sessions(con, log_root, arg)
                return
            _print_feed(con, log_root, live["task_id"], arg)
        elif cmd == "/help":
            # R2-17: `/help <word>` searches; bare `/help` is a grouped
            # index. Both are projections of the command registry, so
            # neither can name a command the product does not accept.
            con.print(render_help(arg))
        elif cmd == "/cost":
            _render_cost(
                {}, log_root, task_id=str(live["task_id"])
            )
        elif cmd == "/skills":
            _render_skills(Path.cwd())
        elif cmd == "/mcp":
            try:
                from cli.vexconfig import merged_settings as _merged

                _mcfg: Optional[Dict[str, Any]] = _merged()
            except Exception:
                _mcfg = None
            _render_mcp(_mcfg, repo_path=Path.cwd())
        elif cmd == "/history":
            con.print("[vex.muted]history is unavailable mid-run â€” retype when idle[/]")
        elif cmd == "/diagnostics":
            values = _diagnostic_lines(log_root, _detect_repo(), live["task_id"])
            if not values:
                con.print("[vex.muted]no diagnostics available[/]")
            for item in values[:40]:
                if isinstance(item, Mapping):
                    con.print(
                        f"  [vex.error]{escape(str(item.get('severity') or 'issue'))}[/] "
                        f"[vex.accent2]{escape(str(item.get('link') or ''))}[/] "
                        f"[vex.muted]{escape(str(item.get('message') or ''))}[/]"
                    )
        elif cmd == "/settings":
            from cli import vexconfig as config_mod

            merged = config_mod.merged_settings()
            for key in sorted(merged):
                if key != "api_key":
                    con.print(
                        f"[vex.muted]{escape(key)}[/] = [vex.accent]"
                        f"{escape(str(merged[key]))}[/]"
                    )
        elif cmd == "/plugins":
            from cli import plugins as plugins_mod

            rows = plugins_mod.list_plugins()
            if not rows:
                con.print("[vex.muted]no plugins installed[/]")
            for row in rows[:40]:
                enabled = "enabled" if row.get("enabled") else "disabled"
                con.print(
                    f"  [vex.accent]{escape(str(row.get('name') or '?'))}[/] "
                    f"[vex.muted]Â· {enabled}[/]"
                )
        elif cmd == "/model":
            try:
                from cli.onboard import format_model_display as _fmt_model
                from cli.vexconfig import merged_settings as _merged2

                con.print(f"[vex.muted]{_fmt_model(None, _merged2())}[/]")
            except Exception:
                con.print("[vex.muted](model unavailable mid-run)[/]")
    except Exception as exc:  # render-only; never raise from the reader
        con.print(
            f"[vex.muted]({cmd.strip('/')} unavailable: "
            f"{escape(ui.strip_ansi(f'{type(exc).__name__}: {exc}'))})[/]"
        )


def normalize_index_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Project a global-index row onto the shape the session UIs already use.

    The index stores `repo_path`/`repo_name`/`updated_at`; the shared
    filter grammar and both renderers read `repo`/`ts`. Normalizing here is
    what lets one index feed `/sessions` in the REPL, the TUI browser, and
    `vex --list-sessions` without a second field vocabulary.

    It also carries `display_status` through when the row has one. It
    deliberately does NOT DERIVE the label: `normalize_index_row` is the
    index projection and runs once per row over a 5,000-row listing, and
    reducing a verdict for every row (rather than for the dozen actually
    displayed) measured 17.8 ms against 2.4 ms — an 8x slowdown of the
    projection step against a 100 ms p95 budget. A renderer calls
    `cli.runview.honest_row_status`, which derives the label ON DEMAND
    from whichever field is present, so nothing is lost and the hot path
    is not a verification pass. See `_honest_row_label`.
    """
    out = dict(row)
    out.setdefault("task_id", out.get("session_id") or "")
    if not out.get("repo"):
        out["repo"] = str(out.get("repo_path") or out.get("repo_name") or "")
    if not out.get("ts"):
        out["ts"] = out.get("updated_at") or 0
    if "resumable" not in out:
        out["resumable"] = bool(row.get("resumable"))
    return out


#: Resolved ONCE. `normalize_index_row` runs this per row, and a
#: function-local `from cli.runview import ...` costs a `__import__` call
#: plus an attribute lookup EVERY time. Measured on the real 5,000-row
#: listing path: 16.4 ms with the per-row import against 2.1 ms without —
#: a 7.8x slowdown of the projection step, against a 100 ms p95 budget.
#: Resolving the reference once makes it a plain call. The `None` default
#: also keeps the helper working on a tree where `cli.runview` cannot
#: import, which is what the bare `def` fallback is for.
_HONEST_ROW_LABEL = None


def _honest_row_label(row: Mapping[str, Any]) -> str:
    """The honest metadata label for one session/index row.

    Routes through `cli.runview.honest_row_status`, which fails CLOSED: a
    bare `completed` / `success` word with no verifier evidence is
    `unverified`, never `verified`. Wrapped because a render path must
    degrade to a printable label rather than raise.

    HOT PATH: this is called once per indexed row, so the authority is
    resolved once and cached (see `_HONEST_ROW_LABEL`).
    """
    global _HONEST_ROW_LABEL
    resolver = _HONEST_ROW_LABEL
    if resolver is None:
        try:
            from cli.runview import honest_row_status

            resolver = honest_row_status
        except Exception:

            def resolver(_row: Any) -> str:  # type: ignore[misc]
                """Fallback when the authority is unavailable: never claim success."""
                return "unknown"

        _HONEST_ROW_LABEL = resolver
    return resolver(row)


def _index_matches(entry: Mapping[str, Any], query: str) -> bool:
    """Filter grammar for global-index rows (same grammar as /sessions)."""
    q = str(query or "").strip().casefold()
    if not q:
        return True
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in (
            "session_id",
            "task_id",
            "issue",
            "repo_path",
            "repo_name",
            "repo_key",
            "status",
            "branch",
        )
    ).casefold()
    for token in q.split():
        if token.startswith("repo:") and len(token) > 5:
            wanted = token[5:]
            if wanted not in haystack:
                return False
            continue
        if token == "resumable":
            if not entry.get("resumable"):
                return False
            continue
        if ":" in token:
            key, _, value = token.partition(":")
            if (
                str(entry.get(key) or "").casefold() != value
                and value not in haystack
            ):
                return False
            continue
        if token not in haystack:
            return False
    return True


def _print_sessions(
    con: Any,
    log_root: Path,
    query: str = "",
    repo: Optional[str] = None,
    root_scoped: bool = False,
) -> None:
    """Print the (optionally filtered) session list.

    Cross-repo by default: the global per-repo index is the read model, so
    `/sessions` works from any CWD and lists every repository's sessions
    without re-reading a single task trace. `repo:` inside `query` and the
    explicit `repo` argument are both honoured as filters. When the global
    index has nothing, the local log root's own index + directory scan
    remains the fallback.

    `root_scoped=True` means the caller supplied an explicit log root (headless
    `vex run`, tests, CI). The global cross-repo index is then NOT consulted:
    the named root is the only read model. Without this, a caller that
    explicitly isolated its log root still received every repository's
    sessions on this machine.
    """
    scoped = repo is not None
    index_rows: List[Dict[str, Any]] = []
    if not root_scoped:
        try:
            from cli.session import index_records

            index_rows = [
                normalize_index_row(row)
                for row in index_records(repo=repo, limit=400)
                if _index_matches(row, query)
            ]
        except Exception:
            index_rows = []
    if index_rows:
        repos = {str(row.get("repo_name") or "") for row in index_rows}
        head = "[vex.accent]recent sessions[/]"
        if query:
            head += f" [vex.muted]matching {escape(query)!r}[/]"
        head += (
            f" [vex.muted]({len(repos)} repo(s); "
            "[vex.running]R[vex.muted] = resumable; "
            "`/resume <id>` continues)[/]"
        )
        con.print(head)
        for row in index_rows[:12]:
            mark = "[vex.running]R[/]" if row.get("resumable") else " "
            short = str(row.get("session_id") or row.get("task_id") or "?")
            status_text = _row_status_text(row)
            repo_label = str(row.get("repo_name") or row.get("repo_key") or "")[:20]
            branch = str(row.get("branch") or "")[:16]
            # Self-closed segments only — see the note on the fallback
            # renderer below about the orphaned `[/]` this replaced.
            con.print(
                f"  {mark} [vex.accent]{escape(short)}[/] "
                f"{status_text} {escape(repo_label)[:20]:20} "
                f"{escape(branch)[:16]:16} "
                f"{escape(str(row.get('issue') or ''))[:44]}"
            )
        return
    if scoped:
        say_empty_state(
            con.print,
            "no_sessions_match" if (query or repo) else "no_sessions",
        )
        return
    sessions = search_sessions(log_root, query, limit=40)
    if not sessions:
        say_empty_state(con.print, "no_sessions_match" if query else "no_sessions")
        return
    head = "[vex.accent]recent sessions[/]"
    if query:
        head += f" [vex.muted]matching {escape(query)!r}[/]"
    con.print(head + " [vex.muted]([vex.running]R[vex.muted] = resumable)[/]")
    for s in sessions[:12]:
        mark = "[vex.running]R[/]" if s.get("resumable") else " "
        status_text = _row_status_text(s)
        # Every segment is self-closed. The trailing `[/]` this line used
        # to carry was an ORPHAN: it only balanced while exactly one tag
        # was open, and adding the status cell's own (balanced) tags made
        # rich raise `MarkupError: closing tag '[/]' ... has nothing to
        # close`. An orphaned close tag is the exact defect this codebase
        # has already been bitten by; it is removed rather than balanced.
        con.print(
            f"  {mark} [vex.accent]{escape(str(s.get('task_id') or '?'))}[/] "
            f"{status_text} "
            f"{escape(ui.strip_ansi(str(s.get('issue') or '')))[:50]}"
        )


def _row_status_text(row: Mapping[str, Any], width: int = 12) -> str:
    """Markup for one session row's honest, width-padded status cell.

    Routes through `cli.runview.honest_row_status`, so the cell reads
    `verified` / `unverified` / `pending` / … and never the bare
    `completed` word that used to stand for both a verified and an
    unverified run. The PLAIN label is padded before styling — padding a
    styled string counts markup characters and silently breaks the
    column. Wrapped so a render path degrades to a printable label rather
    than raising.
    """
    label = _honest_row_label(row)[:width].ljust(width)
    if _honest_row_label(row) == "verified":
        return f"[vex.ok]{escape(label)}[/]"
    if _honest_row_label(row) in ("unverified", "pending"):
        return f"[vex.warn]{escape(label)}[/]"
    if _honest_row_label(row) == "failed":
        return f"[vex.error]{escape(label)}[/]"
    return f"[vex.muted]{escape(label)}[/]"


# ---------------------------------------------------------------------------
# Session lifecycle commands: /fork, /import, /recover
# ---------------------------------------------------------------------------


def _resume_conversation_command(
    con: Any, state: Dict[str, Any], token: str
) -> bool:
    """Resolve `token` as a conversation id and adopt it as this session.

    Handles both the local log root (exact or unique short id) and the
    global per-repo index, so a fork printed in another repository can be
    resumed here. Returns False when the token names no conversation, so
    the caller can report the honest task-id error instead.
    """
    try:
        from cli.session import (
            load_or_create,
            resolve_index_session,
            resolve_session_token,
        )
    except Exception:
        return False
    log_root = state.get("_log_root") or _sessions_root()[0]
    try:
        local = resolve_session_token(log_root, token, state.get("repo"))
        if local.get("status") == "ok":
            session = load_or_create(
                log_root, state.get("repo"), str(local["session_id"]), strict=True
            )
        elif local.get("status") in ("ambiguous", "invalid"):
            con.print(
                f"[vex.error]{local.get('status')} session id:[/] {escape(str(token))} "
                f"[vex.muted]({', '.join(local.get('candidates') or [])[:160]})[/]"
            )
            _set_handler_result(state, "failed", 1)
            return True
        else:
            found = resolve_index_session(token)
            if found.get("status") == "ambiguous":
                con.print(
                    f"[vex.error]ambiguous session id:[/] {escape(str(token))} "
                    f"[vex.muted]({', '.join(found.get('candidates') or [])[:160]})[/]"
                )
                _set_handler_result(state, "failed", 1)
                return True
            if found.get("status") != "ok":
                return False
            session = load_or_create(
                Path(found["log_root"]), None, str(found["session_id"]), strict=False
            )
    except Exception as exc:
        con.print(f"[vex.error]cannot open session {token!r}:[/] {escape(str(exc))}")
        _set_handler_result(state, "failed", 1)
        return True
    state["conversation"] = session
    state["_log_root"] = str(
        session.get("_log_root") or log_root or ""
    )
    turns = len(session.get("turns") or [])
    con.print(
        f"[vex.ok]resumed conversation[/] [vex.muted]{session.get('session_id')} "
        f"Â· {turns} turns Â· {session.get('repo') or 'unknown repo'}[/]"
    )
    summary = str(session.get("summary") or "").strip()
    if summary:
        con.print(f"[vex.muted]{escape(summary[:300])}[/]")
    return True


def _session_arg(rest: str) -> Tuple[str, List[str]]:
    """Split a slash argument into its positional token and its flags."""
    tokens = [token for token in str(rest or "").split() if token]
    positional = [token for token in tokens if not token.startswith("--")]
    flags = [token for token in tokens if token.startswith("--")]
    return (positional[0] if positional else ""), flags


def _fork_command(
    con: Any, log_root: Path, state: Dict[str, Any], line: str
) -> None:
    """`/fork [turn-id]`: fork this conversation with an independent id.

    The fork is a NEW session id with its own journal and snapshot; the
    parent's history is copied up to the fork point and then diverges.
    """
    if _live_run() is not None:
        con.print("[vex.warn]wait for the run to finish before forking[/]")
        return
    conversation = state.get("conversation")
    if not isinstance(conversation, dict):
        con.print("[vex.muted]no conversation to fork yet[/]")
        return
    rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
    at_turn, _flags = _session_arg(rest)
    try:
        from cli.session import fork_session

        fork = fork_session(
            log_root,
            str(conversation.get("session_id") or ""),
            state.get("repo"),
            at_turn_id=at_turn or None,
        )
    except Exception as exc:
        con.print(f"[vex.error]fork failed:[/] {escape(str(exc))}")
        _set_handler_result(state, "failed", 1)
        return
    turns = len(fork.get("turns") or [])
    state["conversation"] = fork
    try:
        from cli.session import save_session

        save_session(log_root, fork)
    except Exception:
        pass
    con.print(
        f"[vex.ok]forked[/] [vex.muted]{fork.get('session_id')} Â· {turns} "
        f"turns copied Â· diverges from "
        f"{fork.get('parent_session_id') or 'the parent'}[/]"
    )
    con.print(
        "[vex.muted]the parent conversation is untouched; "
        f"`/resume {fork.get('session_id')}` returns here[/]"
    )


def _import_command(
    con: Any, log_root: Path, state: Dict[str, Any], line: str
) -> None:
    """`/import <path> [--overwrite]`: load a session export as a conversation."""
    rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
    source, flags = _session_arg(rest)
    if not source:
        con.print("[vex.muted]usage: /import <export.json> [--overwrite][/]")
        return
    path = Path(source)
    if not path.is_file():
        con.print(f"[vex.error]no such export:[/] {escape(str(path))}")
        _set_handler_result(state, "failed", 1)
        return
    try:
        from cli.session import import_session

        imported = import_session(
            str(path),
            log_root,
            state.get("repo"),
            overwrite="--overwrite" in flags,
        )
    except Exception as exc:
        con.print(f"[vex.error]import failed:[/] {escape(str(exc))}")
        _set_handler_result(state, "failed", 1)
        return
    state["conversation"] = imported
    try:
        from cli.session import save_session

        save_session(log_root, imported)
    except Exception:
        pass
    con.print(
        f"[vex.ok]imported[/] [vex.muted]{imported.get('session_id')} Â· "
        f"{len(imported.get('turns') or [])} turns Â· "
        f"{int(imported.get('event_count') or 0)} journal rows[/]"
    )
    con.print(
        "[vex.muted]this is now the active conversation; the previous one is "
        "still on disk[/]"
    )


def _recover_command(
    con: Any, log_root: Path, state: Dict[str, Any], line: str
) -> None:
    """`/recover [session-id] [--fresh|--backup]`: report or quarantine.

    With no argument the newest conversation of this repository is
    inspected. Recovery NEVER deletes: the corrupt snapshot and its event
    journal are renamed to `<name>.corrupt-<ms>` beside the original.
    """
    rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
    token, flags = _session_arg(rest)
    strategy = "report"
    if "--backup" in flags:
        strategy = "backup"
    elif "--fresh" in flags:
        strategy = "fresh"
    try:
        from cli.session import (
            inspect_session,
            list_conversations,
            recover_corrupt_session,
            resolve_session_token,
            startup_recovery_candidate,
        )

        session_id = ""
        if token:
            resolved = resolve_session_token(log_root, token, state.get("repo"))
            if resolved.get("status") == "ok":
                session_id = str(resolved["session_id"])
            else:
                con.print(
                    f"[vex.error]{resolved.get('status')} session id:[/] "
                    f"{escape(str(token))}"
                )
                _set_handler_result(state, "failed", 1)
                return
        else:
            # No id: prefer the newest UNREADABLE conversation (the case
            # recovery exists for), then fall back to the newest healthy one.
            candidate = startup_recovery_candidate(log_root, state.get("repo"))
            records = list_conversations(log_root, state.get("repo"))
            if candidate is None and not records:
                con.print("[vex.muted]no conversation recorded for this repo[/]")
                return
            session_id = str(
                (candidate or {}).get("session_id") or records[0]["session_id"]
            )
        report = inspect_session(log_root, session_id, state.get("repo"))
        con.print(
            f"[vex.accent]{session_id}[/] [vex.muted]{report.get('status')} Â· "
            f"{report.get('turn_count', 0)} turns Â· "
            f"{report.get('event_count', 0)} journal rows[/]"
        )
        if report.get("status") == "ok":
            if strategy == "report":
                con.print("[vex.muted]nothing to recover[/]")
                return
            con.print("[vex.muted]this session is healthy; nothing was changed[/]")
            return
        con.print(f"[vex.warn]{escape(str(report.get('error') or 'unreadable'))}[/]")
        if strategy == "report":
            con.print(
                "[vex.muted]nothing was changed â€” `/recover "
                f"{session_id} --fresh` quarantines the file (never deletes "
                "it) and starts a clean conversation[/]"
            )
            return
        result = recover_corrupt_session(
            log_root, session_id, state.get("repo"), strategy=strategy
        )
        if str(result.get("action") or "") == "quarantined_and_recreated":
            con.print(
                f"[vex.ok]recovered[/] [vex.muted]quarantined to "
                f"{escape(str(result.get('quarantine_path') or ''))}[/]"
            )
            if state.get("conversation") is not None and str(
                (state.get("conversation") or {}).get("session_id") or ""
            ) == session_id:
                from cli.session import load_or_create

                state["conversation"] = load_or_create(
                    log_root, state.get("repo"), session_id, strict=False
                )
        else:
            con.print(
                f"[vex.ok]restored[/] [vex.muted]{escape(str(result.get('action')))}[/]"
            )
    except Exception as exc:
        con.print(f"[vex.error]recover failed:[/] {escape(str(exc))}")
        _set_handler_result(state, "failed", 1)


def _print_feed(con: Any, log_root: Path, task_id: str, query: str = "") -> None:
    """The plain-text trace-feed view (REPL /feed and /trace): rebuilt
    from the run's own trace.jsonl, reasoning lines italic+dimmed vs
    upright accent actions (visual-distinction Task E)."""
    from cli.tracelog import FeedBuilder

    feed = FeedBuilder(task_id)
    tf = log_root / task_id / "trace.jsonl"
    if tf.is_file():
        for ln in tf.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                feed.consume(json.loads(ln))
            except ValueError:
                continue
    entries = feed.entries
    q = (query or "").lower()
    if q:
        entries = [
            e for e in entries if q in e.summary.lower() or q in e.category.lower()
        ]
    if not entries:
        con.print("[vex.muted]no feed entries yet[/]")
        return
    for ent in entries[-40:]:
        style = _feed_style(ent)
        con.print(f"[vex.muted]{ent.index:>2}[/] [{style}]{ent.summary}[/]")


def _feed_style(entry: Any) -> str:
    """The reasoning/action distinction for one feed entry (shared
    contract with the TUI, cli.tui.feed_style â€” the italic-dim vs
    bold-accent table lives there; this plain variant keeps the REPL's
    idiom without importing the UI module at module load)."""
    if entry.category == "reason":
        return f"italic {ui.TEXT_SECONDARY}"
    if entry.category in ("tool", "diff"):
        return f"bold {ui.ACCENT_TEXT}"
    if entry.category == "verify":
        s = entry.summary.lower()
        if any(m in s for m in _VERIFY_PASS_MARKS):
            return ui.SUCCESS
        if any(m in s for m in _VERIFY_FAIL_MARKS):
            return ui.ERROR
    return ui.TEXT_SECONDARY


_VERIFY_PASS_MARKS = (
    "checkpoint passed",
    "final verification â€” target + full suite pass",
    "task success",
    "result: success",
)
_VERIFY_FAIL_MARKS = (
    "still failing",
    "regressed",
    "flaky",
    "tool error",
    "static check failed",
    "edit policy violation",
    "self-critique rejected",
    "rejected (entry not read-only)",
)


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
        self._thinking = False  # between model_request and its response
        self._think_started = 0.0

    # -- lifecycle --------------------------------------------------------

    def start(self, quiet: bool = False) -> "LiveMonitor":
        """Begin rendering (quiet=True: track but don't render â€” for tests)."""
        self._quiet = quiet
        if not quiet and ui.motion_enabled():
            self._status = self._console.status(
                f"[vex.running]{self._last_label}[/]", spinner=ui.spinner_name()
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
            con = self._console
            con.print(
                f"[vex.muted]run[/] [{ui.TEXT_PRIMARY}]{self._events_seen} events[/] "
                f"[vex.muted]{ui.DOT}[/] [{ui.TEXT_PRIMARY}]{self._calls} model calls[/] "
                f"[vex.muted]{ui.DOT}[/] [{ui.TEXT_PRIMARY}]{self._tokens:,} tokens[/] "
                f"[vex.muted]{ui.DOT}[/] [vex.accent2]{ui.fmt_cost(self._cost_usd)}[/]"
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
        from cli.runview import event_parts

        kind, data, _timestamp, _identity = event_parts(obj)
        kind = {
            "run_started": "task_start",
            "run_finished": "task_end",
            "model_completed": "model_response",
            "verification": "verify",
        }.get(kind, kind)
        if not kind:
            return
        self._events_seen += 1
        self._last_event_at = time.monotonic()

        if kind == "model_request":
            self._thinking = True
            self._think_started = time.monotonic()
        elif kind == "model_response":
            self._thinking = False
        if kind == "model_response":
            usage = data.get("usage") or data
            self._calls += 1
            try:
                self._tokens += int(
                    usage.get("tokens", usage.get("total_tokens", usage.get("completion_tokens", 0))) or 0
                )
            except (TypeError, ValueError):
                pass
            try:
                self._cost_usd += float(
                    usage.get("cost", usage.get("cost_usd", usage.get("usd", 0.0))) or 0.0
                )
            except (TypeError, ValueError):
                pass

        label = _EVENT_LABELS.get(kind)
        if label:
            for field in _LABEL_FIELDS.get(kind, []):
                if data.get(field) is not None:
                    label = label.format(**{field: data[field]})
            self._last_label = label
            self._render()

    def _render(self) -> None:
        if self._quiet or self._status is None or _READER_QUIET["quiet"]:
            return
        try:
            bits = f"[vex.running]{self._last_label}[/]"
            # While the model thinks, add the rotating tech joke
            # (Qwen-Code-style flavor â€” same behavior as the TUI run-line).
            if self._thinking and ui.motion_enabled():
                elapsed_think = time.monotonic() - self._think_started
                joke = ui.joke_at(int(elapsed_think // 4.5))
                bits += (
                    f" [vex.muted]{ui.DOT}[/] [i {ui.TEXT_SECONDARY}]{escape(joke)}[/]"
                )
            self._status.update(
                bits + f" [vex.muted]{ui.DOT} {self._events_seen} events "
                f"{ui.DOT} {ui.fmt_cost(self._cost_usd)}[/]"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Approval watcher (Task E: interactive prompts with rendered diff)
# ---------------------------------------------------------------------------


def watch_for_approvals(
    log_root: Path,
    task_ids: List[str],
    stop: threading.Event,
    poll_s: float = 0.5,
    policy: Optional["_commands.ApprovalPolicy"] = None,
) -> None:
    """While runs are live, surface pending approval requests inline.

    The worker's gate is file-based (runtime/approval.py): the request
    appears at logs/{task_id}.runtime/approval/request.json and the worker
    blocks. This watcher renders the request's diff with the Vex theme and
    prompts the human; the typed decision goes to decision.json via the
    module's `decide()` helper (same protocol the tests use). Runs in the
    interactive session's monitor thread; a no-op when no task parks.

    `policy` is the CALLER's session policy when it has one, so a grant
    reaches the rest of that session. The flag-path callers (`vex fix
    --approval`, the benchmark) have no session object and fall back to the
    process-scoped policy, whose lifetime is the process.

    Four things are load-bearing here, and all four were wrong before this
    round:

    1. **The exact effect is shown, not only the diff.** The prompt names the
       command, the MCP server, the paths, and the side effect through the
       shared `ApprovalRequestView`, so a REPL user sees the same object the
       TUI shows. A diff alone does not say whether a tool call is about to
       reach a network or a server.
    2. **The scope menu is here too.** `y/s/p/c/n` through the shared
       `approval_from_answer` + `ApprovalPolicy`, so once / session / path /
       command-scope work identically on both shells. This watcher previously
       offered only y/N, so a REPL user could never grant and never needed
       to be re-prompted.
    3. **A settled gate is not re-prompted.** `request.json` outlives a
       timed-out gate, so a watcher that only polls for the file asks a
       second surface to decide something already decided.
    4. **The outcome reported is the DECISION, not the run's result.** The
       old "approved — the run continues with the fix" claimed a fact this
       thread cannot observe: the gate applies a diff only if the run's own
       verification passes. A run that is approved and then fails
       verification would have been reported here as continuing with the fix.
    """
    from runtime import approval as approval_mod

    if policy is None:
        policy = _commands.session_approval_policy()
    handled: Dict[str, bool] = {tid: False for tid in task_ids}
    while not stop.is_set():
        for tid, done in handled.items():
            if done:
                continue
            gate = Path(log_root) / f"{tid}.runtime" / "approval"
            try:
                req = approval_mod.pending_request(str(gate))
            except Exception:
                req = None
            if req is None:
                continue
            handled[tid] = True  # one prompt per task per watcher
            settled = _commands.approval_gate_outcome(gate)
            if settled["decision"] in {"approved", "rejected", "timeout"}:
                _render_settled_approval(gate, settled, tid)
                continue
            view = _commands.approval_request_view(req)
            con = ui.console()
            con.print()
            ui.rule(f"[vex.accent]approval needed — {tid}[/]")
            if view.summary:
                con.print(f"[vex.muted]{escape(view.summary)}[/]")
            matching = policy.matching(view)
            if matching is not None:
                con.print(
                    f"[vex.ok]a {escape(matching.scope)} approval for this exact "
                    "effect is already in force[/] — not asking again"
                )
                continue
            con.print(
                f"[vex.warn]{escape(view.effect_summary())}[/]"
            )
            con.print("[vex.muted]proposed diff:[/]")
            ui.print_diff(view.diff or "(no diff in request)")
            con.print("[vex.muted]issue:[/] " + escape(view.issue_text[:300]))
            deadline = ""
            if view.timeout_s:
                deadline = (
                    f"  (no answer within {view.timeout_s:g}s and the gate "
                    "gives up on its own)"
                )
            while True:
                try:
                    answer = input(
                        "allow this? [y=once s=session p=path c=command n=reject]"
                        f"{deadline} "
                    )
                except (EOFError, KeyboardInterrupt):
                    # EOF is the safe answer, and the gate's own record is the
                    # authority on what it became.
                    answer = "n"
                approved, scope = _commands.approval_from_answer(answer, scoped=True)
                if approved or scope == "once":
                    break
                if stop.is_set():
                    # A re-prompt loop that never re-checks shutdown is a hang
                    # with a prompt on screen: closing the session mid-answer
                    # must end the question, not trap it.
                    return
                con.print("please answer y, s, p, c, or n")
            after = _commands.approval_gate_outcome(gate)
            if after["decision"] == "timeout":
                con.print(
                    "[vex.error]the gate had already timed out — this decision "
                    "has no effect[/]"
                )
                continue
            try:
                approval_mod.decide(str(gate), approve=approved)
            except Exception as exc:
                con.print(f"[vex.error]could not record the decision: {escape(exc)}[/]")
                continue
            if approved and scope != "once":
                policy.record(view, scope)
                con.print(
                    f"[vex.ok]approved once and remembered for this exact "
                    f"effect ({escape(scope)} scope)[/]"
                )
            if approved:
                con.print(
                    "[vex.ok]approval recorded[/] — the gate applies the diff "
                    "only if the run's own verification passes"
                )
            else:
                con.print("[vex.error]rejected[/] — the gate will not apply the diff")
        # Every task has been decided (or its gate was already settled), and
        # this watcher is documented as one prompt per task, so there is
        # nothing left to watch. Returning here rather than polling until the
        # session's stop event turns a finished watcher into a busy loop; the
        # two are otherwise identical.
        if handled and all(handled.values()):
            return
        stop.wait(poll_s)


def _render_settled_approval(gate: Path, settled: Dict[str, Any], task_id: str) -> None:
    """Say what a gate that is already settled did, and ask nothing.

    `request.json` survives a timed-out gate, so a surface that only polls for
    the file will prompt again for a decision that can no longer be made.
    """
    con = ui.console()
    if settled["decision"] == "timeout":
        con.print(
            f"[vex.warn]the approval request for {task_id} expired before a "
            "decision arrived — the diff was NOT applied[/]"
        )
    elif settled["decision"] == "approved":
        con.print(
            f"[vex.muted]the approval request for {task_id} was already "
            "approved by another surface[/]"
        )
    elif settled["decision"] == "rejected":
        con.print(
            f"[vex.muted]the approval request for {task_id} was already "
            "rejected[/]"
        )


# ---------------------------------------------------------------------------
# Session persistence (Task A) â€” the CLI's memory of past runs
# ---------------------------------------------------------------------------

_SESSION_FILE = "sessions.jsonl"

#: Ring the completion bell from the REPL's run paths (Task F,
#: interaction-polish round). The full-screen TUI owns its OWN ring
#: (VexApp._finish_run) and disables this one on mount so a TUI fix â€”
#: which reuses _execute_task â€” never double-beeps. VEX_NOTIFY=0 (ui.
#: bell) silences both for CI / ssh / audio-free machines.
NOTIFY = True


def notify_done(status: str = "", *, detail: str = "", label: str = "task") -> Any:
    """Signal that a run finished â€” completed OR failed.

    Before VEX-CEILING-10 this rang only when a run completed, so a run
    that died was indistinguishable from a run nobody noticed. Failure now
    escalates: a repeated bell plus, when enabled, a distinct desktop
    toast. The escalation is policy in :mod:`cli.notify`, not a guess
    here, and the returned receipt names exactly what was emitted and what
    was suppressed (a non-TTY never gets a bell byte, so piped and
    ``--json`` output stays byte-clean).

    When the full-screen TUI is mounted (``_ON_TASK_START`` is its hook),
    the REPL-side notification is skipped: the TUI notifies at the true
    end of the run (``VexApp._finish_run``, which also covers the question
    / research modes) â€” one notification per finished run, never two.
    """
    if not NOTIFY or _ON_TASK_START is not None:
        return None
    try:
        from cli import notify as _notify

        return _notify.notify_run(status, label=label, detail=detail)
    except Exception:
        # A notification failure must never take down a run's reporting.
        try:
            label_text = "done" if status == "success" else (status or "finished")
            ui.bell(f"vex task {label_text}")
        except Exception:
            pass
        return None


def _terminal_result_display(
    status: Any, verification: Any
) -> Tuple[str, str, str]:
    """Return the journal-gated label, Rich style, and glyph for one result."""
    from cli.runview import (
        effective_terminal_status,
        status_is_completed,
        status_is_verified,
        status_label,
    )

    evidence = verification
    if isinstance(evidence, Mapping):
        evidence = [evidence]
    elif not isinstance(evidence, (list, tuple)):
        target = getattr(evidence, "target_test_passed", None)
        regression = getattr(evidence, "regression_passed", None)
        evidence = (
            [
                {
                    "target_passed": target,
                    "regression_passed": regression,
                    "flaky": getattr(evidence, "flaky", False),
                }
            ]
            if target is not None or regression is not None
            else []
        )
    canonical = effective_terminal_status(status, evidence)
    label = status_label(canonical)
    if status_is_verified(canonical):
        return label, "vex.ok", ui.GLYPHS["ok"]
    if status_is_completed(canonical):
        return label, "vex.warn", ui.GLYPHS["wait"]
    return label, "vex.error", ui.GLYPHS["fail"]


def print_run_recovery(
    con: Any,
    log_root: Any,
    task_id: str,
    status: Any,
    note: str = "",
) -> bool:
    """Render the failure/recovery card IN the surface where it happened.

    R2-17 (item 6). Closes the gap cli/AGENTS.md named: the classifier
    produced a record and the registry advertised the actions, but no
    surface drew the card, so a failed run ended with a red word and
    nothing to do about it.

    Shown for a FAILED or CANCELLED run, and for an UNVERIFIED one whose
    own note names a problem — an unverified run is not a failure, so it
    gets the honest note rather than a "what failed" card it did not
    earn. Returns True when a card was drawn. Never raises.
    """
    try:
        from cli.runview import failure_lines, run_verdict

        verdict = run_verdict(status)
        if verdict not in ("failed", "cancelled") and (
            verdict != "unverified" or not str(note or "").strip()
        ):
            return False
        excerpt = str(note or "").strip()
        if not excerpt:
            excerpt = _last_run_error(log_root, task_id)
        for line in failure_lines(excerpt, log_root=log_root, task_id=str(task_id)):
            con.print(line)
        return True
    except Exception:
        return False


def _last_run_error(log_root: Any, task_id: str) -> str:
    """The newest error-ish text in a run's journal, or "".

    Reads the run's OWN records rather than a passed-in string, so the
    card can be drawn at a point where the caller has no exception in
    hand — which is the common case: a run returns a failed status, not
    an exception. Never raises.
    """
    try:
        path = Path(log_root) / str(task_id) / "trace.jsonl"
        if not path.is_file():
            return ""
        newest = ""
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            from cli.runview import event_parts

            kind, data, _ts, _identity = event_parts(event)
            if kind in ("tool_error", "error", "verification", "verify"):
                for key in ("error", "detail", "summary", "output", "raw"):
                    value = str(data.get(key) or "").strip()
                    if value:
                        newest = value
                        break
            elif kind in ("result", "run_finished", "task_end"):
                for key in ("note", "reason", "error"):
                    value = str(data.get(key) or "").strip()
                    if value:
                        newest = value
                        break
        return newest[:300]
    except Exception:
        return ""


def session_store_path(log_root: Path) -> Path:
    """logs/.vex-sessions.jsonl â€” the interactive session index."""
    return Path(log_root) / ".vex-sessions.jsonl"


def record_session(
    log_root: Path, task_id: str, issue: str, repo: str, status: str
) -> None:
    """Append one completed/attempted interactive run to the session index.

    Never raises: the index is an enhancement — a failed append must not
    take down a verified fix's reporting. Also ingests the run's facts
    into memory (memory-first: Boundary-4 poll + one session row) so
    future sessions recall this one with no manual `vex memory` call.

    The SAME row is upserted into the global per-repo index
    (``cli.session.index_run``) so `--continue` and `/sessions` can find
    this run from any CWD without re-reading its trace.

    Both rows carry ``display_status``: the HONEST verdict label derived
    from this run's own journal (see ``_session_display_status``). The
    lifecycle ``status`` answers "can this be continued"; the display
    label answers "was it verified". Keeping them apart is the R2-17
    honesty fix — a `completed_unverified` run used to be listed with the
    same word as a verified one.
    """
    display_status = _session_display_status(
        Path(log_root), str(task_id), {"status": status}
    )
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
                        "display_status": display_status,
                    }
                )
                + "\n"
            )
    except OSError:
        pass
    try:
        from cli.session import index_run

        index_run(
            log_root,
            task_id,
            issue=issue,
            repo=repo,
            status=str(status or "completed"),
            resumable=str(status or "").lower() in ("running", "resumable"),
            display_status=display_status,
        )
    except Exception:
        pass
    try:
        from cli.session import ingest_session_facts

        if (Path(log_root) / str(task_id)).is_dir():
            ingest_session_facts(log_root, task_id, issue, repo, status)
    except Exception:
        pass


def _safe_task_dir(task_id: str, log_root: Path) -> Optional[Path]:
    try:
        from memory.paths import safe_task_dir

        return safe_task_dir(task_id, Path(log_root))
    except Exception:
        return None


def _session_status_from_trace(log_root: Path, task_id: str) -> str:
    """Classify a task from the authoritative event journal."""
    d = _safe_task_dir(task_id, log_root)
    if d is None:
        return "blocked"
    result: Optional[Dict[str, Any]] = None
    end: Optional[Dict[str, Any]] = None
    try:
        lines = (d / "trace.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    from cli.runview import event_parts, terminal_status

    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, data, _timestamp, _identity = event_parts(event)
        if kind in ("result", "run_finished", "completion_decision"):
            nested = data.get("result")
            result = {**dict(nested), **dict(data)} if isinstance(nested, Mapping) else dict(data)
        elif kind in ("task_end", "cancellation_requested"):
            end = dict(data)
    terminal = result or end
    if not terminal:
        return "resumable"
    status = terminal_status(terminal.get("status") or "failed")
    if terminal.get("aborted"):
        return "cancelled"
    if status in ("completed_verified", "completed_unverified"):
        return "completed"
    if status in ("needs_input", "blocked"):
        return "blocked"
    if status in ("cancelled", "timeout", "failed"):
        return "cancelled" if status == "cancelled" else "failed"
    return "failed"


def session_status(log_root: Path, task_id: str, recorded: str = "") -> str:
    """Return one of running, resumable, completed, failed, cancelled, blocked."""
    try:
        live = _live_run()
        if live and str(live.get("task_id")) == str(task_id):
            return "running"
        trace_status = _session_status_from_trace(Path(log_root), str(task_id))
        if trace_status != "resumable":
            return trace_status
        if _is_resumable(Path(log_root), str(task_id)):
            return "resumable"
        value = str(recorded or "").lower()
        if value in ("running", "resumable", "completed", "failed", "cancelled", "blocked"):
            return value
        if value in ("success", "passed"):
            return "completed"
        if value in ("error", "failure"):
            return "failed"
        if value in ("interrupted", "aborted", "cancelled"):
            return "cancelled"
        return "resumable"
    except Exception:
        return "blocked"


def is_session_resumable(log_root: Path, task_id: str, recorded: str = "") -> bool:
    """Whether a session can be continued without bypassing its trace contract."""
    return session_status(log_root, task_id, recorded) in ("running", "resumable")


def active_task_id(
    log_root: Path,
    last: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the live task first, then the last completed task."""
    if live and live.get("task_id"):
        return str(live["task_id"])
    try:
        registered = _live_run()
        if registered and registered.get("task_id"):
            return str(registered["task_id"])
    except Exception:
        pass
    if last and last.get("task_id"):
        return str(last["task_id"])
    return None


def list_sessions(log_root: Path, limit: int = 15) -> List[Dict[str, Any]]:
    """Most-recent-first session entries (index + directory fallback).

    The index (logs/.vex-sessions.jsonl) carries metadata but only exists
    for runs THIS CLI version made AND recorded â€” including interrupted
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
            if not isinstance(obj, dict):
                continue
            task_id = obj.get("task_id", "")
            if _safe_task_dir(task_id, log_root) is None:
                continue
            obj["status"] = session_status(log_root, task_id, str(obj.get("status") or ""))
            obj["resumable"] = is_session_resumable(log_root, task_id, str(obj.get("status") or ""))
            obj["display_status"] = _session_display_status(
                log_root, task_id, obj
            )
            entries[task_id] = obj

    root = Path(log_root)
    if root.is_dir():
        for d in root.iterdir():
            if d.name.startswith("."):
                continue
            if d.name in entries:
                continue
            task_dir = _safe_task_dir(d.name, root)
            if task_dir is None or not task_dir.is_dir() or not is_session_resumable(
                root, d.name
            ):
                continue
            start = _first_event(task_dir / "trace.jsonl", "task_start")
            if start is None:
                continue
            data = start.get("data") or {}
            entries[d.name] = {
                "ts": task_dir.stat().st_mtime,
                "task_id": d.name,
                "issue": (data.get("issue_text") or "")[:200],
                "repo": data.get("repo_path") or "",
                "status": "resumable",
                "display_status": _session_display_status(root, d.name, {}),
                "resumable": True,
                "source": "dir-scan",
            }

    out = sorted(entries.values(), key=lambda e: e.get("ts", 0), reverse=True)
    return out[:limit]


def _session_display_status(
    log_root: Path, task_id: str, row: Mapping[str, Any]
) -> str:
    """Derive one session row's honest label from the run's OWN journal.

    This is the R2-G45 fix. `session_status` answers "can this run be
    continued" and therefore collapses `completed_verified` and
    `completed_unverified` into the single word `completed` — correct for
    its question, fatal for a renderer. This reads the terminal event plus
    its verification evidence and returns `cli.runview.run_verdict_label`,
    so a verified run reads `verified` and an unverified one reads
    `unverified`.

    Falls back to `cli.runview.honest_row_status(row)` when the journal
    cannot be read, which still never yields `verified` without evidence.
    Never raises.
    """
    tid = str(task_id or "")
    if not tid:
        return "unknown"
    try:
        from cli.runview import run_verdict, run_verdict_label

        task_dir = _safe_task_dir(tid, Path(log_root))
        if task_dir is not None and task_dir.is_dir():
            trace = task_dir / "trace.jsonl"
            try:
                text = (
                    trace.read_text(encoding="utf-8", errors="replace")
                    if trace.is_file()
                    else ""
                )
            except OSError:
                text = ""
            from cli.runview import event_parts

            terminal: Dict[str, Any] = {}
            evidence: List[Mapping[str, Any]] = []
            saw_start = False
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind, payload, _ts, _identity = event_parts(event)
                if kind in ("run_started", "task_start"):
                    saw_start = True
                elif kind in ("verify", "verification", "final_verify"):
                    nested = payload.get("result")
                    evidence.append(
                        nested if isinstance(nested, Mapping) else payload
                    )
                elif kind in (
                    "result",
                    "run_finished",
                    "task_end",
                    "completion_decision",
                ):
                    nested = payload.get("result")
                    terminal = (
                        {**dict(nested), **dict(payload)}
                        if isinstance(nested, Mapping)
                        else dict(payload)
                    )
            if terminal or saw_start:
                if terminal.get("aborted"):
                    return run_verdict_label("cancelled")
                if not terminal:
                    # Started, no terminal event: still open, not unverified.
                    return run_verdict_label("pending")
                return run_verdict_label(
                    run_verdict(terminal.get("status"), evidence=evidence)
                )
        return _honest_row_label(row)
    except Exception:
        return "unknown"


def search_sessions(
    log_root: Path, query: str = "", limit: int = 200
) -> List[Dict[str, Any]]:
    """Session records matching `query` (see session_matches for the
    filter grammar), newest first. A wider scan than list_sessions'
    default cap so filtering a long history actually finds the needle
    instead of searching only the 15 newest runs.

    Assumes log_root is a logs directory; never raises. Empty query
    returns the newest `limit` (the plain listing).
    """
    sessions = list_sessions(log_root, limit=max(limit, 1) * 4)
    return filter_sessions(sessions, query)[:limit]


#: The /sessions + --list-sessions filter grammar (searchable sessions,
#: interaction-polish round Task C): whitespace-separated tokens, ALL of
#: which must match (AND). A `key:value` token filters that field; a
#: bare token fuzzy-free substring-matches any of task_id / issue /
#: repo / status. Recognized keys: status, repo, task, day (YYYY-MM-DD
#: exact date), since/until (ISO date bounds on the run's timestamp),
#: and the valueless `resumable`. Unknown `key:value` tokens match
#: against the whole line so a typo degrades to "narrow" never "all".
_SESSION_KEYS = ("status", "repo", "task", "day", "since", "until")


def _session_day(ts: Any) -> str:
    """The local YYYY-MM-DD for a session timestamp ('' when unusable)."""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return ""


def session_matches(entry: Dict[str, Any], query: str) -> bool:
    """True when a session record satisfies the filter `query`.

    Empty/whitespace query matches everything. Never raises; a
    malformed value (bad date, non-string field) simply fails to match
    that token (=> the record drops), which is the honest behavior for
    a filter that can't be evaluated.
    """
    q = (query or "").strip()
    if not q:
        return True
    hay = " ".join(
        str(entry.get(k) or "") for k in ("task_id", "issue", "repo", "status")
    ).lower()
    for tok in q.split():
        low_tok = tok.lower()
        if ":" in tok and tok.split(":", 1)[0].lower() in _SESSION_KEYS:
            key, _, val = tok.partition(":")
            key = key.lower()
            val = val.lower()
            if key == "status":
                if val not in str(entry.get("status") or "").lower():
                    return False
            elif key == "repo":
                repo = str(entry.get("repo") or "")
                if val not in repo.lower() and val not in Path(repo).name.lower():
                    return False
            elif key == "task":
                if val not in str(entry.get("task_id") or "").lower():
                    return False
            elif key in ("day", "since", "until"):
                day = _session_day(entry.get("ts"))
                if not day:
                    return False
                if key == "day":
                    if not day.startswith(val):
                        return False
                elif (key == "since" and day < val) or (key == "until" and day > val):
                    return False
            continue
        if low_tok == "resumable":
            if not entry.get("resumable"):
                return False
            continue
        # bare token: substring over the whole line
        if low_tok not in hay:
            return False
    return True


def filter_sessions(sessions: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """The sessions satisfying `query` (order preserved â€” newest first
    as list_sessions returns them). Total: an empty list or query is
    handled; never raises."""
    try:
        return [s for s in sessions if session_matches(s, query)]
    except Exception:
        return list(sessions)


def _is_resumable(log_root: Path, task_id: str) -> bool:
    """True when a run for task_id exists and has unfinished work.

    Uses ONLY public on-disk state (state.json + plan.json + trace.jsonl):
    completed steps present AND remaining steps present AND no final
    result event â€” i.e. exactly the state the harness's resume contract
    (config["resume"]=True + same task_id) can continue from.
    """
    if not task_id:
        return False
    d = _safe_task_dir(task_id, log_root)
    if d is None:
        return False
    state_file = d / "state.json"
    plan_file = d / "plan.json"
    trace = d / "trace.jsonl"
    if not state_file.is_file() or not plan_file.is_file():
        agent_mode = False
        terminal = False
        try:
            for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                data = event.get("data") or {}
                if event.get("kind") == "task_start" and str(data.get("mode")) in ("agent", "agent_task"):
                    agent_mode = True
                if event.get("kind") in ("result", "task_end"):
                    terminal = True
        except OSError:
            pass
        return agent_mode and not terminal
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
            agent_mode = False
            terminal = False
            for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                data = o.get("data") or {}
                if o.get("kind") == "task_start" and str(data.get("mode")) in ("agent", "agent_task"):
                    agent_mode = True
                if o.get("kind") in ("result", "task_end"):
                    terminal = True
            if terminal:
                return False
            if agent_mode:
                return True
        except OSError:
            pass
    return True


def _resume_task(
    task_id: str, log_root: Path, state: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Continue a previous interactive run by task id (Task A).

    Rebuilds the Task from the run's own trace (task_start carries
    repo_path/issue_text/config verbatim â€” the harness's public
    observability surface) and re-enters _run_one_fix with
    config["resume"]=True, which makes harness.core.run_task continue
    from state.json + plan.json (skip completed steps, keep work/).
    Agent tasks resume IN PLACE under the same task id: pristine/orig
    references are kept and the prior session's turns are replayed as
    resume history (not a restart). Returns the run's result info
    dict (or None when the run couldn't start) so callers can fold
    it into /diff + the conversation transcript. Never raises
    (callers render honest lines for bad ids / missing repos).
    """
    con = ui.console()
    d = _safe_task_dir(task_id, log_root)
    if d is None:
        con.print(
            f"[vex.error]invalid task id: {task_id!r} "
            "(expected a single contained path segment)[/]"
        )
        return None
    current_status = session_status(log_root, task_id)
    if current_status in ("completed", "failed", "cancelled", "blocked"):
        con.print(
            f"[vex.warn]cannot resume {task_id}: session is {current_status}[/]"
        )
        return None
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
        return None
    if data.get("mode") == "agent":
        # Agent sessions edit the live repo; "resume" continues the same
        # task id (pristine/orig references are kept, the loop starts
        # from the current tree with the prior turns replayed as
        # resume history â€” history replay, not a restart).
        try:
            from harness.agent_loop import load_resume_history
        except ImportError:
            load_resume_history = None  # type: ignore[assignment]
        history = ""
        try:
            if load_resume_history is not None:
                history = load_resume_history(task_id, log_root) or ""
        except Exception:
            history = ""
        conv_turns = ""
        try:
            conv = (state or {}).get("conversation")
            if isinstance(conv, dict):
                turns = conv.get("turns") or []
                related = [
                    t for t in turns if str((t or {}).get("task_id") or "") == task_id
                ][-6:]
                bits = [
                    f"{t.get('role')}: {str(t.get('text') or '')[:200]}"
                    for t in related
                ]
                conv_turns = "\n".join(b for b in bits if b.strip())
        except Exception:
            conv_turns = ""
        if conv_turns and conv_turns not in history:
            history = (history + "\nconversation turns:\n" + conv_turns).strip()[:4000]
        if history:
            con.print("[vex.muted]replaying prior turns (resumed, not restarted)[/]")
        try:
            return _run_one_agent(
                issue,
                Path(repo),
                state,
                log_root,
                task_id=task_id,
                resume_history=history or None,
            )
        except TypeError:
            # Legacy _run_one_agent fakes (test scaffolding) without the
            # resume_history kwarg: same resume, no history replay.
            return _run_one_agent(issue, Path(repo), state, log_root, task_id=task_id)
    prior_cfg = data.get("config") or {}
    cfg = {k: v for k, v in prior_cfg.items() if k != "api_key"}
    cfg["resume"] = True
    # session-state pins (model) override the prior run's config
    for key in ("model", "provider"):
        if state.get(key):
            cfg[key] = state[key]
    from shared.types import Task

    task = Task(task_id=task_id, repo_path=repo, issue_text=issue, config=cfg)

    # Environment snapshot restore (Round 8, Task C): if the original
    # run snapshotted its dep image (logs/{task_id}.runtime/env_snapshot
    # .json) and the image cache has since been pruned, restore it with
    # an O(1) retag so the resumed run doesn't rebuild the environment.
    # Best-effort: no snapshot / stale fingerprint / docker down just
    # means a normal lazy rebuild.
    try:
        from execution import env_snapshot as envs

        runtime_dir = d.parent / f"{task_id}.runtime"
        restored = envs.restore_environment(task_id, repo, runtime_dir)
        if restored:
            ui.console().print(
                f"[vex.ok]environment snapshot restored "
                f"(image {restored.split(':')[-1]})[/]"
            )
    except Exception:
        pass

    return _execute_task(
        task, log_root, preview=False, state=state
    )  # plan already approved once


def _fold_resumed(
    result_info: Optional[Dict[str, Any]],
    last: Dict[str, Any],
    log_root: Path,
    state: Dict[str, Any],
) -> None:
    """Fold a resumed run's result into the session (never raises).

    Assumes result_info is a _run_one_* result dict (or None when the
    run couldn't start). Updates the session's `last` (so /diff +
    /sessions see the resumed run) and records the assistant turn in
    the persistent conversation â€” the "keeps talking" half of resume.
    """
    try:
        if not isinstance(result_info, dict):
            return
        last.update(result_info)
        conv = (state or {}).get("conversation")
        if isinstance(conv, dict):
            from cli.session import append_turn, save_session

            append_turn(
                conv,
                "assistant",
                str(result_info.get("answer") or result_info.get("status") or ""),
                task_id=result_info.get("task_id"),
            )
            save_session(log_root, conv)
    except Exception:
        pass


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
            from cli.runview import event_parts

            event_kind, _data, _timestamp, _identity = event_parts(o)
            if event_kind == kind or (kind == "task_start" and event_kind == "run_started"):
                return o
    except OSError:
        pass
    return None


def _index_verify(row: Dict[str, Any]) -> bool:
    """Re-check ONE indexed run against its own journal.

    The global index is a lookup hint, never a status authority: a row that
    said "resumable" can be stale (the run finished in another process).
    Every surface that ACTS on a row re-verifies that single run against
    its authoritative trace, so listing stays fast and acting stays honest.
    """
    root = str(row.get("log_root") or "")
    task_id = str(row.get("task_id") or "")
    if not root or not task_id:
        return False
    try:
        return is_session_resumable(Path(root), task_id, str(row.get("status") or ""))
    except Exception:
        return False


def _sessions_root(
    file_config: Optional[Dict[str, Any]] = None,
) -> Tuple[Path, bool]:
    """The one artifact-root authority shared by every session surface.

    Returns ``(root, explicit)``. ``explicit`` is True when the caller
    chose the root (a ``--log-root`` flag or a configured ``log_root``),
    which is the isolation contract: an explicitly chosen root must not
    report the machine's OTHER repositories' sessions.
    """
    try:
        from cli.session import resolve_artifact_root

        resolved = resolve_artifact_root(None, None, file_config)
        return Path(resolved["log_root"]), str(resolved.get("source")) in ("flag", "config")
    except Exception:
        return Path("logs"), False


def most_recent_resumable(
    log_root: Optional[Path] = None,
    repo: Optional[str] = None,
    *,
    cross_root: bool = False,
) -> Optional[Dict[str, Any]]:
    """The newest resumable session for one root, or across every root.

    `cross_root=True` widens the search to every artifact root this machine
    has indexed, which is what makes `vex --continue` work from any working
    directory.

    The global per-repo index is consulted first (exactly one journal read,
    for the chosen candidate); the local log root's own index plus directory
    scan stays as the fallback, so a machine with no global index behaves
    exactly as it did before.

    The default confines the search to the log root that owns the run.
    That is the isolation contract for an EXPLICITLY chosen root
    (``--log-root`` / a configured ``log_root``): such a caller must never
    be handed a session from another repository it never asked about.
    """
    try:
        from cli.session import index_newest_resumable

        row = index_newest_resumable(
            repo=repo,
            verify=_index_verify,
            scan=300,
            log_root=None if cross_root else str(log_root or ""),
        )
        if row is not None:
            return {
                "task_id": row.get("task_id"),
                "issue": row.get("issue") or "",
                "repo": row.get("repo_path") or "",
                "status": row.get("status") or "resumable",
                "resumable": True,
                "ts": row.get("updated_at") or 0,
                "source": "global-index",
                "log_root": row.get("log_root") or "",
            }
    except Exception:
        pass
    resolved = _sessions_root() if log_root is None else (Path(log_root), True)
    root = Path(resolved[0])
    for entry in list_sessions(root, limit=300):
        if entry.get("resumable"):
            entry.setdefault("source", "local-index")
            entry["log_root"] = str(root)
            return entry
    return None


def cmd_continue(
    log_root: Optional[Path] = None, repo: Optional[str] = None
) -> int:
    """`vex --continue`: resume the most recent resumable session.

    Searches every indexed artifact root, so this works from any working
    directory; the selected run is re-verified against its own journal
    before it is resumed.
    """
    con = ui.console()
    explicit = log_root is not None
    root = Path(log_root) if log_root is not None else _sessions_root()
    if isinstance(root, tuple):
        root, explicit = root[0], bool(root[1]) or explicit
    root = Path(root)
    s = most_recent_resumable(root, repo=repo, cross_root=not explicit)
    if s is None:
        con.print(
            "[vex.muted]no resumable sessions in any known artifact root "
            f"(searched {root.resolve()}) - nothing to continue[/]"
        )
        con.print(
            "[vex.muted]start one with plain `vex`, or list with "
            "`vex --list-sessions`[/]"
        )
        return 1
    task_id = s.get("task_id")
    target_root = Path(str(s.get("log_root") or root))
    if _safe_task_dir(task_id, target_root) is None:
        con.print(
            f"[vex.error]invalid task id: {task_id!r} "
            "(expected a single contained path segment)[/]"
        )
        return 2
    con.print(
        f"[vex.accent]resuming[/] [vex.muted]{task_id} - "
        f"{s.get('issue', '')[:80]}[/]"
    )
    _resume_task(task_id, target_root, {})
    return 0


def cmd_list_sessions(
    log_root: Optional[Path] = None,
    repo: Optional[str] = None,
    *,
    limit: int = 15,
) -> int:
    """`vex --list-sessions`: sessions from ANY repository, newest first.

    Reads the global per-repo index (no task trace is re-read to list) and
    falls back to the local log root when the index has nothing. `repo` is
    the explicit cross-repo filter: a path, a repo key, or a directory name.
    """
    con = ui.console()
    explicit = log_root is not None
    root = Path(log_root) if log_root is not None else _sessions_root()
    if isinstance(root, tuple):
        root, explicit = root[0], bool(root[1]) or explicit
    root = Path(root)
    rows: List[Dict[str, Any]] = []
    try:
        from cli.session import index_records

        # An explicitly chosen root is an isolation boundary: only rows
        # under it are reported, so `--list-sessions` with a configured
        # log_root can never surface another repository's sessions.
        rows = index_records(
            repo=repo,
            limit=limit,
            log_root=root if explicit else None,
        )
    except Exception:
        rows = []
    if rows:
        repo_count = len({str(r.get("repo_name") or "") for r in rows})
        con.print(
            "[vex.accent]recent sessions[/] [vex.muted](newest first, across "
            f"{repo_count} repos; [vex.running]R[/][vex.muted] = resumable - "
            "`vex --resume <task_id>`, `--repo <name>` to filter)[/]"
        )
        for row in rows:
            row = normalize_index_row(row)
            mark = "[vex.running]R[/]" if row.get("resumable") else " "
            stamp = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(row.get("updated_at") or 0)
            )
            short = str(row.get("session_id") or row.get("task_id") or "?")
            repo_label = str(row.get("repo_name") or "")[:24]
            status_label = str(row.get("status") or "?")
            con.print(
                f"  {mark} [vex.accent]{short}[/] "
                f"[vex.muted]{stamp}  {status_label:9} "
                f"{repo_label:24} {escape(str(row.get('issue') or ''))[:48]}[/]"
            )
        return 0
    sessions = list_sessions(root, limit=limit)
    if not sessions:
        con.print(f"[vex.muted]no recorded sessions under {root.resolve()}[/]")
        return 0
    con.print(
        "[vex.accent]recent sessions[/] [vex.muted](newest first; "
        "[vex.running]R[/][vex.muted] = resumable - "
        "`vex --resume <task_id>`)[/]"
    )
    for s in sessions:
        mark = "[vex.running]R[/]" if s.get("resumable") else " "
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("ts", 0)))
        con.print(
            f"  {mark} [vex.accent]{s.get('task_id', '?')}[/] "
            f"[vex.muted]{stamp}  {s.get('status', '?'):8} "
            f"{(s.get('issue') or '')[:60]}[/]"
        )
    return 0


def _resume_conversation(session_id: str, log_root: Path) -> int:
    """Report a resumable conversation and how to continue it.

    The interactive shells own the live conversation handle, so this
    headless path shows the recovered context honestly and names the
    in-session `/resume <id>` that continues it.
    """
    try:
        from cli.session import load_or_create

        conversation = load_or_create(log_root, None, session_id, strict=False)
    except Exception as exc:
        ui.err_console().print(
            f"[vex.error]cannot open session {session_id!r}: {escape(str(exc))}[/]"
        )
        return 2
    turns = len(conversation.get("turns") or [])
    summary = str(conversation.get("summary") or "").strip()
    ui.console().print(
        f"[vex.accent]session[/] [vex.muted]{session_id} - {turns} turns - "
        f"{conversation.get('repo') or 'unknown repo'}[/]"
    )
    if summary:
        ui.console().print(f"[vex.muted]{escape(summary[:400])}[/]")
    ui.console().print(
        "[vex.muted]open `vex` in that repository and type /resume "
        f"{session_id} to continue the conversation[/]"
    )
    return 0


def cmd_resume(task_id: str, log_root: Optional[Path] = None) -> int:
    """`vex --resume <task_id>`: resume one run or conversation by id.

    A conversation short id (what `/fork` prints) resolves across every
    indexed root; a task id resolves against the log root that owns it.
    """
    root = Path(log_root) if log_root is not None else _sessions_root()[0]
    token = str(task_id or "").strip()
    if _safe_task_dir(token, root) is None:
        found: Dict[str, Any] = {"status": "missing"}
        try:
            from cli.session import resolve_index_session

            found = resolve_index_session(token)
        except Exception:
            pass
        if found.get("status") == "ok" and found.get("log_root"):
            ui.console().print(
                f"[vex.accent]resuming conversation[/] [vex.muted]"
                f"{found['session_id']} from {found['log_root']}[/]"
            )
            return _resume_conversation(found["session_id"], Path(found["log_root"]))
        if found.get("status") == "ambiguous":
            ui.err_console().print(
                f"[vex.error]ambiguous session id: {token!r} matches "
                f"{', '.join(found.get('candidates') or [])[:200]}[/]"
            )
            return 2
        ui.err_console().print(
            f"[vex.error]invalid task id: {token!r} "
            "(expected a single contained path segment)[/]"
        )
        return 2
    _resume_task(token, root, {})
    return 0


# ---------------------------------------------------------------------------
# The interactive session
# ---------------------------------------------------------------------------

_BANNER = r"""
[vex.accent]    __      __       _    _       [/]
[vex.accent]    \ \    / /__ _ _| |__| |___ _ _ [/]
[vex.accent]     \ \/\/ / - _) '_| / _` / -_) '_|[/]
[vex.accent]      \___/\___\_|_|_\__,_\___|_|  [/]
[vex.muted]  the AI harness that fixes bugs â€” type what's wrong[/]
"""
# ^ superseded by the block wordmark + splash/compact split (branding
# round, Task C) â€” kept one release for any external script that
# greps for it; run_interactive no longer prints it.


def _session_model_label(state: Dict[str, Any], file_config: Dict[str, Any]) -> str:
    """The model shown in headers: session pin > config pin > router."""
    return state.get("model") or (file_config or {}).get("model") or "router (adaptive)"


def _print_session_head(
    repo: Path,
    log_root: Path,
    state: Dict[str, Any],
    file_config: Dict[str, Any],
    first_launch: bool,
) -> None:
    """Two-tier session head (Task C, branding round): the blocky
    wordmark splash ONCE â€” first launch into an empty session
    (OpenCode's empty-state pattern); the compact one-line header
    every other session start (Claude Code's per-session pattern)."""
    from cli.main import _get_version

    con = ui.console()
    model = _session_model_label(state, file_config)
    if first_launch:
        ui.print_splash(
            repo=repo,
            log_root=Path(log_root).resolve(),
            model=model,
            version=_get_version(),
        )
        # R2-17 (item 5): a real first-run path. The splash is the brand;
        # this is the ORIENTATION — what to type, what the product promises
        # about verification, and where the evidence lands. Three beats,
        # because a first-run screen that is itself a wall fails exactly
        # the way the old `/help` did.
        print_first_run(
            con, repo=repo, log_root=Path(log_root).resolve(), model=model
        )
        con.print()
    else:
        con.print()
        ui.print_compact_header(repo=repo, model=model, version=_get_version())
        con.print(
            f"[vex.muted]logs[/] [{ui.TEXT_PRIMARY}]{Path(log_root).resolve()}[/] "
            f"[vex.muted]{ui.DOT} plain language just works[/]"
        )
        con.print()


_HELP = """[vex.accent]what you can say[/]
  [vex.muted]<anything>[/]          just say it â€” explain, change, run, or debug
  [vex.accent]/mode <name>[/]       select Plan, Build, Explore, Review, Debug, or Ask
  [vex.accent]/build <text>[/]      run a build/feature task end to end
  [vex.accent]/ask <question>[/]    read-only answer grounded in this repo
  [vex.accent]/theme[/] [vex.muted][<name>][/]  show or switch the terminal theme (saved)
  [vex.accent]/settings[/] [vex.muted][<key>][/]  list or read effective settings
  [vex.accent]/plugins[/]          list installed plugins and their state
  [vex.accent]/steer <text>[/]      steer the RUNNING task (also: plain text while a run is live;
                                    "replan: â€¦" replaces the plan; "abort" stops cleanly)
  [vex.muted]TUI keys during a run:[/] [vex.accent]ctrl+g[/] steer at the next safe boundary ·
                                    [vex.accent]ctrl+b[/] queue without interrupting ·
                                    [vex.accent]ctrl+x[/]/[vex.accent]ctrl+c[/] cancel
                                    [vex.muted](the cancel hint stays visible down to 62 columns)[/]
  [vex.accent]/detach[/]            leave this run ALIVE and stop watching it
                                    [vex.muted](follow from another terminal:[/] vex watch <task-id>)
  [vex.accent]/attach[/] [vex.muted][<task-id>][/]  rebind to a detached run, replaying its journal
  [vex.accent]/status[/]            current/last task's structured state
  [vex.accent]/trace[/]             live action feed (TUI: /trace <n> expands an entry)
  [vex.accent]/feed <text>[/]       the whole trace feed as a scrollable, searchable history
                                    (TUI opens a browser; type to filter; enter expands one)
  [vex.accent]/diff[/]              re-render the last run's diff (syntax-highlighted)
  [vex.accent]/diff undo[/]         undo the last agent edit ([vex.muted]/diff undo all[/] reverts all)
  [vex.accent]/sessions <query>[/]  search previous sessions ACROSS repos â€” filters:
                                    [vex.muted]status:failed[/] [vex.muted]repo:name[/]
                                    [vex.muted]since:YYYY-MM-DD[/] [vex.muted]resumable[/][vex.muted]
                                    + free text (TUI: a browser)[/]
  [vex.accent]/resume[/] [vex.muted][<task_id|session_id>][/]  continue an interrupted task or
                                     conversation; a short session id resolves from any
                                     repository (no id = newest resumable anywhere)
  [vex.accent]/fork[/] [vex.muted][<turn-id>][/]  fork this conversation at a turn boundary
                                     (new independent session id, diverging history)
  [vex.accent]/import[/] [vex.muted]<export.json>[/]  load a session export as this conversation
  [vex.accent]/recover[/] [vex.muted][<session-id>] [--fresh|--backup][/]  report or
                                     quarantine a corrupt session (the bytes are kept)
  [vex.accent]/plan [<text>][/]      preview steps before edits (approve/reject);
                                     with text, runs that fix with preview forced
  [vex.accent]/review[/]             last fix's diff + rationale together
  [vex.accent]/compact[/]            compact the conversation (recall-backed
                                     summary kept, old turns dropped)
  [vex.accent]/copy-diff[/]          copy the last diff to the clipboard
  [vex.accent]/history [text][/]     search this session's input history
                                     (same filter grammar as /sessions)
  [vex.accent]/init[/]              scaffold .vex/ in the session repo
                                     (settings + example command/skill)
  [vex.accent]/login[/] [vex.muted][global|project][/]  configure a model (wizard)
  [vex.accent]/logout[/]            remove the stored api_key
  [vex.accent]/mcp[/] [vex.muted][<label>][/]  list configured MCP servers
                                     (with a label: list that server's tools)
  [vex.accent]/skills[/] [vex.muted][<filter>][/]  list discovered skills + origins
  [vex.accent]/cost[/]              spend: last run + session total (trace usage-sum)
  [vex.accent]/undo[/] [vex.muted][<file>][/]    stage a revert; repeat to widen it
                                     [vex.muted](code|task|all)[/] pick the granularity
                                     [vex.muted](commit|discard|plan|force)[/]
                                     your next prompt commits the staged revert
  [vex.accent]/redo[/]              redo the last undone agent edit
  [vex.accent]/checkpoints[/]        browse durable run checkpoints
  [vex.accent]/context[/]            inspect cited repository, skill, and memory sources
  [vex.accent]/diagnostics[/]        show language-server diagnostics
  [vex.accent]/export[/] [vex.muted][<path>][/]  export a redacted session artifact
  [vex.accent]/share[/] [vex.muted][<path>][/]  export metadata-only shareable view
  [vex.accent]/files[/]              browse repository files
  [vex.accent]/open[/] [vex.muted][path[:line]][/]  open a file in your editor
                                    ($VISUAL > $EDITOR > code -g / vi; detached)
  [vex.accent]/doctor[/] [vex.muted][--json][/]  read-only health checks
                                    (docker · git · provider · litellm ·
                                    textual · .vex · worktree · MCP) with fixes
  [vex.accent]/repo[/] [vex.muted]<path>[/]  switch repository: reloads project
                                    settings, model/provider, log root, caches
                                    and prints the effective-settings diff
  [vex.accent]/theme[/] [vex.muted][reset][/]  back to the default terminal theme
  [vex.accent]/settings[/] [vex.muted]reset|unset <key>[/]  back to the default
  [vex.accent]/clear[/]             fresh conversation (the old one is kept)
  [vex.accent]/approve[/]           approve a pending approval request
  [vex.accent]/reject[/]            reject a pending approval request
  [vex.accent]/cancel[/]            stop the current run cleanly (resumable)
  [vex.accent]/quiet[/]             toggle live feed + spinner verbosity
  [vex.accent]@path[/]               mention a repo file (its content is attached)
  [vex.accent]/<custom> [args][/][vex.muted]  run a custom command from .vex/commands/
                                    or ~/.config/vex/commands/ ($ARGUMENTS = args)[/]
  [vex.accent]repo <path>[/]        switch the target repo (default: current dir)
  [vex.accent]model <name>[/]        pin a model for subsequent runs
  [vex.accent]/model[/] [vex.muted][<name>][/]  show the effective model (+ source),
                                    or pin <name> for subsequent runs
  [vex.accent]help[/] / [vex.accent]/help[/]  this text
  [vex.accent]exit[/] / [vex.accent]/quit[/] / Ctrl+D   quit
"""


# ---------------------------------------------------------------------------
# SEARCHABLE HELP (R2-17 item 5)
#
# `/help` was a ~70-line aligned wall. A person cannot use a wall; a person
# can search. The index is built from `cli.commands.COMMAND_SPECS` — the
# ONE command authority the palette, the preflight, and the headless
# resolver already use — so help cannot drift from what the product
# actually accepts, and adding a command needs no help edit.
#
# `_HELP` above is retained as the LONG reference and is still what
# `python -m cli`'s docs read; `render_help()` is what a person gets. Both
# are projections of the same registry, so a command present in one is
# present in the other.
# ---------------------------------------------------------------------------

#: Curated grouping. A command not named here lands in "more", which is a
#: deliberate bucket rather than a silent omission: a new command shows up
#: in help the moment it is registered, before anyone groups it.
_HELP_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "start here",
        ("/help", "/ask", "/plan", "/build", "/mode", "/review"),
    ),
    (
        "the run",
        ("/status", "/steer", "/trace", "/feed", "/detach", "/attach", "/cancel", "/quiet"),
    ),
    (
        "the change",
        ("/diff", "/undo", "/redo", "/checkpoints", "/copy-diff", "/open", "/files", "/diagnostics"),
    ),
    (
        "history",
        ("/sessions", "/resume", "/fork", "/import", "/recover", "/history", "/compact", "/clear", "/share", "/export"),
    ),
    (
        "what it cost",
        ("/cost",),
    ),
    (
        "your setup",
        (
            "/login", "/logout", "/model", "/effort", "/settings", "/theme", "/plugins",
            "/mcp", "/skills", "/init", "/repo", "/doctor", "/context",
        ),
    ),
    ("stop", ("/approve", "/reject", "/quit")),
)

_HELP_GROUP_OF: Dict[str, str] = {
    name: group for group, names in _HELP_GROUPS for name in names
}


@dataclass(frozen=True)
class HelpEntry:
    """One searchable help row.

    `search_text` is precomputed at import so ranking never re-joins the
    row's fields, and so the searchable surface includes the argument hint
    and the summary's synonyms (`"money"`, `"undo"`) rather than only the
    command's name.
    """

    name: str
    group: str
    summary: str
    argument_hint: str
    search_text: str
    shortcut: str = ""
    #: The task PHRASING that reached this row, when the search matched one.
    #: Empty for the bare index. Additive, so a caller constructing a
    #: `HelpEntry` positionally is unaffected.
    task: str = ""

    def render_line(self, width: int = 78) -> str:
        """One aligned markup row for the grouped help index.

        The key rides inside the existing summary column rather than in a
        column of its own. That is deliberate and was measured: a separate
        column is the first thing to wrap at 80 columns, and a wrapped key
        renders as text on the next row that reads like a note about the
        command above it. The key is also charged against the body budget, so
        adding it can only shorten the summary, never the row.
        """
        label = f"{self.name} {self.argument_hint}".strip()
        padded = label.ljust(24)
        key = f" {self.shortcut}" if self.shortcut else ""
        body = escape(self.summary)
        room = max(8, int(width) - len(padded) - 4 - len(key))
        if len(body) > room:
            body = body[: max(1, room - 1)] + "…"
        return f"  [vex.accent]{escape(padded)}[/] [vex.muted]{body}{escape(key)}[/]"

    def render_task_line(self, width: int = 78) -> str:
        """The row as it reads when a TASK reached it, not a name.

        `"what did you change"  ->  /diff  review what the run did…` — the
        phrasing the person actually typed leads, because a match reached
        through a task is a different answer from one reached by guessing the
        command name, and the reader deserves to see which one happened.

        The whole PLAIN line is built first and bounded, and only then are its
        parts escaped: a line assembled from three already-styled fragments
        overflows whenever one of them grows, which is how a help row became
        179 characters in an earlier round of this tree.
        """
        plain = f"{self.task or self.name}  ->  {self.name}"
        if self.summary:
            room = max(12, int(width) - len(plain) - 2)
            tail = self.summary if len(self.summary) <= room else (
                self.summary[: max(1, room - 1)] + "…"
            )
            plain = f"{plain}  {tail}"
        return f"  [vex.accent]{escape(plain)}[/]"


def help_index() -> List[HelpEntry]:
    """Every registered command as a searchable help row, grouped.

    Built from `cli.commands.COMMAND_SPECS`, so it cannot describe a
    command the product does not have or omit one it does. Total and
    never raises: a registry that fails to import yields an empty index
    and a help surface that says so, rather than a traceback in the middle
    of a session.
    """
    try:
        from cli.commands import COMMAND_SPECS
    except Exception:
        return []
    out: List[HelpEntry] = []
    for spec in COMMAND_SPECS:
        name = str(getattr(spec, "name", "") or "")
        if not name:
            continue
        summary = str(getattr(spec, "summary", "") or "")
        hint = str(getattr(spec, "argument_hint", "") or "")
        shortcut = str(getattr(spec, "shortcut_label", lambda: "")() or "")
        out.append(
            HelpEntry(
                name=name,
                group=_HELP_GROUP_OF.get(name, "more"),
                summary=summary,
                argument_hint=hint,
                shortcut=shortcut,
                search_text=f"{name} {hint} {summary}".casefold(),
            )
        )
    out.sort(key=lambda entry: (entry.group, entry.name))
    return out


#: Extra words a daily user would type that are not in any summary. This
#: is the difference between "searchable" and "technically searchable": the
#: question is usually about a PROBLEM ("money", "crash", "undid") rather
#: than a command name.
_HELP_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "/cost": ("money", "spend", "price", "billing", "budget", "tokens", "receipts", "ledger"),
    "/doctor": ("broken", "crash", "health", "stuck", "why", "help", "wrong"),
    "/undo": ("undid", "revert", "mistake", "oops", "regret"),
    "/diff": ("change", "changes", "what changed", "patch"),
    "/resume": ("continue", "interrupted", "stopped", "crashed"),
    "/sessions": ("history", "past", "previous", "runs"),
    "/help": ("commands", "what can i", "usage", "how do i"),
    "/steer": ("redirect", "change my mind", "actually", "instead"),
    "/trace": ("logs", "debug", "detail", "evidence"),
    "/feed": ("logs", "history", "debug", "detail"),
    "/cancel": ("stop", "kill", "abort", "halt"),
    "/status": ("progress", "where are we", "state"),
}


#: Words that carry no search signal. A daily user types a QUESTION
#: ("how much money did I spend"), not a command, and requiring every
#: token to match makes "how" and "much" fail the whole query. Dropping
#: them is what makes the surface answer questions instead of keywords.
_HELP_STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "can", "did", "do", "does", "for",
        "how", "i", "if", "in", "is", "it", "much", "my", "of", "on", "or",
        "please", "show", "that", "the", "to", "was", "were", "what",
        "when", "which", "with", "you", "your",
    }
)


def help_search(query: str, *, limit: int = 12) -> List[HelpEntry]:
    """Rank help rows against a free-text query.

    Uses `cli.fuzzy` — the same subsequence matcher the command palette
    and the sessions browser already use — so "help behaves the same way
    everywhere" is a property of the code rather than a convention. An
    empty query returns `[]` (the caller renders the grouped index
    instead); a query matching nothing returns `[]` too, and the caller
    says so honestly rather than falling back to the whole wall.

    Multi-word queries AND their signal tokens, after dropping a
    stop-word list so a whole QUESTION ranks. If AND finds nothing the
    query is retried as an OR, so a two-word question never returns an
    empty help when one of its words would have found the command. A
    token matches the row's name, its argument hint, its summary, or its
    documented synonyms, so "how much money" finds `/cost`.
    """
    needle = str(query or "").strip().casefold()
    if not needle:
        return []
    try:
        from cli import fuzzy
    except Exception:
        return []
    entries = help_index()
    if not entries:
        return []
    tokens = [
        token for token in needle.split() if token and token not in _HELP_STOPWORDS
    ] or needle.split()
    if not tokens:
        return []
    haystacks = {
        entry.name: (
            f"{entry.search_text} {' '.join(_HELP_SYNONYMS.get(entry.name, ()))}"
        )
        for entry in entries
    }

    def rank(require_all: bool) -> List[HelpEntry]:
        scored: List[Tuple[float, str, HelpEntry]] = []
        for entry in entries:
            hay = haystacks[entry.name]
            words = set(hay.split())
            bare = entry.name.lstrip("/")
            total = 0.0
            hits = 0
            for token in tokens:
                score = fuzzy.fuzzy_score(token, entry.search_text)
                if score is None:
                    score = fuzzy.fuzzy_score(token, hay)
                if score is None:
                    continue
                hits += 1
                if token in words:
                    score += 12.0
                # NAME affinity, and the general answer to "I typed a
                # question, which command do I want". A whole-word hit on
                # the command's own name outranks a hit in a summary or a
                # synonym, and a 3+ char prefix of the name counts for
                # something: "undid" should reach /undo, not /diff.
                if token == bare:
                    score += 40.0
                elif len(token) >= 3 and bare.startswith(token):
                    score += 24.0
                total += score
            if not hits or (require_all and hits < len(tokens)):
                continue
            # In AND mode every token matched, so normalising by the token
            # count is the token count — and averaging by `hits` would let
            # ONE strong hit beat TWO real ones, which is exactly how
            # "undid that change" reached /diff instead of /undo. In OR
            # mode the raw total is right: covering more of the question
            # is the goal.
            divisor = len(tokens) if require_all else 1
            scored.append((-(total / max(1, divisor)), entry.name, entry))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored]

    matched = rank(True)
    if len(matched) < max(1, int(limit or 1)):
        # MERGE, do not fall back. An AND that found something still hides
        # the command that answers HALF the question, which is how
        # "undid that change" lost /undo entirely: /diff matched both
        # words, so the OR pass never ran. Partial answers rank BEHIND
        # complete ones, so a two-word question never returns an empty
        # help while also never hiding the other half's command.
        seen = {entry.name for entry in matched}
        for entry in rank(False):
            if entry.name not in seen:
                seen.add(entry.name)
                matched.append(entry)
    return _prefer_task_matches(needle, matched, limit)


def _prefer_task_matches(
    needle: str, matched: List[HelpEntry], limit: int
) -> List[HelpEntry]:
    """Re-order a command-name ranking by the TASK phrasings that reached it.

    VEX-PF-06. The measurement this fixes, taken on this tree before the
    change: `help_search("show me what changed")` returned `/help`, `/cost`,
    `/steer` — and `help_search("stop the run")` returned `/detach`, `/cost`,
    `/ask`. Both are questions a first-day user types and neither names a
    command, because `fuzzy_score` ANDs each query WORD against one long
    summary and any 200-character summary satisfies "show"/"me"/"what"/
    "changed" on order alone.

    So a task-phrasing hit is placed AHEAD of the name ranking, and the row
    carries the phrasing that reached it (`HelpEntry.task`) so `/help` can
    show "what did you change -> /diff" rather than a bare command name.

    A phrasing that resolves to a command the name ranking did not produce is
    ADDED from the registry index, because the catalogue is curated and a
    command the fuzzy ranking missed is still a real answer to a real
    question — that miss is the whole defect. The catalogue is only consulted
    when the query is non-empty, and an unavailable catalogue leaves the
    historical ranking byte-identical.
    """
    cap = max(1, int(limit or 1))
    try:
        from cli import onboarding
    except Exception:
        return matched[:cap]
    try:
        rows = onboarding.task_search(needle, limit=cap)
    except Exception:
        return matched[:cap]
    index = {entry.name: entry for entry in help_index()}
    by_name: Dict[str, HelpEntry] = {entry.name: entry for entry in matched}
    ordered: List[HelpEntry] = []
    seen: List[str] = []
    for row in rows:
        name = row.command
        if name in seen:
            continue
        base = by_name.get(name) or index.get(name)
        if base is None:
            # A phrasing naming a command this build does not have is a
            # catalogue bug, not a user-facing answer; skip it rather than
            # teach a door with no room behind it.
            continue
        seen.append(name)
        ordered.append(
            HelpEntry(
                name=base.name,
                group=base.group,
                summary=base.summary,
                argument_hint=base.argument_hint,
                search_text=base.search_text,
                shortcut=base.shortcut,
                task=row.phrase,
            )
        )
    for entry in matched:
        if entry.name in seen:
            continue
        seen.append(entry.name)
        ordered.append(entry)
    return ordered[:cap]


def render_task_help_index(*, width: int = 78) -> List[str]:
    """The TASK half of a bare `/help`: phrasings first, then command names.

    VEX-PF-06. A bare `/help` used to be a wall of command names, which
    answers a question nobody asked — a person who wants to see what changed
    does not know the command is called `/diff`. So the index now opens with
    the phrasings, grouped by the TASK, and the command-name index follows
    underneath for the person who already knows what they want.

    Every group renders as ONE line, listing the phrasings it covers and the
    commands they reach, and a group below the anti-clutter threshold is
    dropped by `cli.onboarding.task_groups` rather than rendered short. A
    per-row listing was measured at 30 extra lines, which is a wall again.
    """
    try:
        from cli import onboarding
    except Exception:
        return []
    try:
        groups = onboarding.task_groups()
    except Exception:
        return []
    room = max(32, int(width))
    out: List[str] = [
        "[vex.accent]by what you want to do[/] "
        "[vex.muted](type any phrase after /help)[/]"
    ]
    for group, rows in groups:
        commands = " ".join(dict.fromkeys(row.command for row in rows))
        # One PLAIN row, budgeted ONCE, and budgeted in the order that matters:
        # the canonical phrasing first (it is what a person TYPES), the command
        # list next (it is already in the index below), and the group label out
        # of whatever is left. Sizing the phrase column against `room` and the
        # command column separately is how a row ends up six columns over; and
        # clipping the phrasing while the commands have room teaches a phrase
        # nobody can type.
        fixed = len(commands) + 4  # " -> " plus the commands column
        phrase_min = len(rows[0].phrase)
        # The omission marker is RESERVED before the group label is sized. It
        # is a 3-character suffix that silently ate the tail of the canonical
        # phrasing when it was appended afterwards, which is the one string on
        # this row a person is expected to type.
        marker_room = f" +{len(rows) - 1}" if len(rows) > 1 else ""
        group_room = max(0, room - fixed - phrase_min - len(marker_room) - 1)
        head = "  " + (
            group
            if len(group) + 2 <= group_room
            else group[: max(1, group_room - 1)].rstrip() + "…"
        )
        phrase_room = max(phrase_min + len(marker_room), room - fixed - len(head) - 1)
        # The FIRST phrase is the canonical one and is always shown, bounded
        # rather than dropped; the rest are dropped as a COUNTED list, because
        # a silently shortened list of phrasings reads as the complete list
        # and a person who typed the one that vanished gets nothing back.
        shown: List[str] = []
        for row in rows:
            candidate = " · ".join([*shown, row.phrase])
            if shown and len(candidate) > phrase_room - len(marker_room):
                break
            shown.append(row.phrase)
        phrases = " · ".join(shown)
        dropped = len(rows) - len(shown)
        if dropped:
            # The RESERVED width and the PRINTED count are deliberately
            # different numbers: the reservation is the worst case so the row
            # cannot grow, and the printed count is the truth. Reusing the
            # reservation as the count claimed "+4" on a row that dropped two,
            # which is a disclosure marker that lies.
            phrases = phrases[: max(0, phrase_room - len(marker_room))].rstrip()
            phrases = f"{phrases} +{dropped}"
        elif len(phrases) > phrase_room:
            phrases = phrases[: max(1, phrase_room - 1)].rstrip() + "…"
        out.append(
            f"[{ui.TEXT_PRIMARY}]{escape(head)}[/] "
            f"[vex.muted]{escape(phrases)}[/] "
            f"[vex.accent]-> {escape(commands)}[/]"
        )
    return out


def render_help(query: str = "", *, width: int = 78) -> str:
    """The human help surface: a grouped index, or ranked search results.

    A bare `/help` returns a GROUPED index — one aligned row per command,
    grouped by what a person is trying to do — plus the search hint. A
    query returns the ranked matches. Either way the answer names real
    commands, because the index is the registry.

    Markup is assembled from `escape()`d fields only; no user or registry
    text is ever interpolated into a style tag. Never raises.
    """
    dot = ui.DOT
    q = str(query or "").strip()
    if q:
        matches = help_search(q)
        if not matches:
            return (
                f"[vex.muted]no command matches {escape(repr(q))}[/]\n"
                f"[vex.muted]try a word like {escape('cost')}, "
                f"{escape('resume')}, {escape('undo')}, or {escape('doctor')}[/]"
            )
        lines = [
            f"[vex.accent]help[/] [vex.muted]{dot}[/] "
            f"[{ui.TEXT_PRIMARY}]{len(matches)} match(es)[/] for "
            f"[vex.accent]{escape(q)}[/]"
        ]
        for entry in matches:
            # A row reached through a TASK leads with the phrasing; a row
            # reached by guessing the command name leads with the name. Both
            # are bounded, and both are assembled from plain text before any
            # markup is added, so neither can grow past the terminal.
            if entry.task:
                lines.append(entry.render_task_line(width))
            else:
                lines.append(entry.render_line(width))
        lines.extend(render_keyboard_shortcuts(width=width))
        return "\n".join(lines)
    entries = help_index()
    if not entries:
        return "[vex.muted]no command registry available; run vex --help instead[/]"
    lines = [
        f"[vex.accent]what you can say[/] [vex.muted]{dot}[/] "
        f"[vex.muted]type[/] [vex.accent]/help <word>[/] [vex.muted]to search "
        f"these {_count_words()} commands[/]"
    ]
    for line in render_task_help_index(width=width):
        lines.append(line)
    lines.append("")
    lines.append("[vex.muted]every command, by name[/]")
    current = ""
    for entry in entries:
        if entry.group != current:
            current = entry.group
            lines.append("")
            lines.append(f"[vex.accent]{escape(current)}[/]")
        lines.append(entry.render_line(width))
    lines.append("")
    lines.extend(render_keyboard_shortcuts(width=width))
    lines.append(
        "[vex.muted]say anything to start a run "
        "(explain, change, run, or debug) [vex.muted]·[/] "
        "[vex.accent]@path[/] attaches a file [vex.muted]·[/] "
        "[vex.accent]exit[/] or Ctrl+D quits[/]"
    )
    return "\n".join(lines)


def render_keyboard_shortcuts(*, width: int = 78) -> List[str]:
    """Render the registry's declared shortcuts as one aligned block.

    The keys are the same values the palette prints and the TUI binds, so
    help cannot teach a key that does nothing. Built from
    `cli.commands.keyboard_shortcuts()`; an unavailable registry renders
    nothing rather than raising in the middle of a `/help`.
    """
    try:
        from cli import commands as commands_mod

        table = commands_mod.keyboard_shortcuts()
    except Exception:
        return []
    rows: List[str] = []
    for row in table.get("commands", []):
        keys = row.get("keys", [])
        purposes = row.get("purposes", [])
        for key, purpose in zip(keys, purposes, strict=False):
            rows.append((f"{key} {row['command']}", purpose or row.get("summary", "")))
    for group in ("surface", "navigation"):
        for row in table.get(group, []):
            rows.append(
                (
                    f"{row.get('key')} {row.get('surface', group)}",
                    row.get("purpose", ""),
                )
            )
    if not rows:
        return []
    key_width = max(len(label) for label, _ in rows) + 1
    out = ["", "[vex.accent]keyboard[/]"]
    for label, purpose in rows:
        room = max(8, int(width) - key_width - 2)
        body = escape(purpose)
        if len(body) > room:
            body = body[: max(1, room - 1)] + "…"
        out.append(
            f"  [vex.accent]{escape(label.ljust(key_width))}[/] "
            f"[vex.muted]{body}[/]"
        )
    return out


def _count_words() -> int:
    """The registry size, for the help header. Never raises."""
    try:
        return len(help_index())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# THE RESUME BRIEFING (R2-17 item 3)
#
# On start, ONE honest screen: what happened, what it cost, what is
# UNVERIFIED, what happens next. This is the highest-value single change
# for a daily user, because it is the first thing they see and the
# product's own invariant is honesty about verification.
#
# Every number is re-derived from the run's own journal by
# `cli.runview.briefing_facts` at RENDER time — the same discipline as the
# completion card. The briefing therefore cannot claim a number the fold
# did not read, and it cannot go stale between runs.
#
# The briefing is SKIPPED on a genuine first launch (there is nothing to
# brief) and shown whenever a previous run exists. A briefing of nothing
# is noise; a briefing that says "nothing yet" is a lie about the run you
# just had.
# ---------------------------------------------------------------------------


def _last_briefable_run(
    log_root: Path, *, repo: Any = None
) -> Optional[Dict[str, Any]]:
    """The most recent run worth briefing on, or None.

    Prefers the newest session row for THIS repository and falls back to
    the newest recorded run anywhere, which is what makes the briefing
    useful after `cd`. Returns None when there is nothing to brief.
    """
    try:
        rows = search_sessions(Path(log_root), "", limit=8)
    except Exception:
        rows = []
    if repo is not None:
        wanted = str(Path(repo).resolve()) if repo else ""
        for row in rows:
            row_repo = str(row.get("repo") or "")
            if row_repo and wanted and Path(row_repo).resolve() == Path(wanted):
                return row
    for row in rows:
        if str(row.get("task_id") or ""):
            return row
    return None


def render_resume_briefing(
    log_root: Path,
    *,
    repo: Any = None,
    width: int = 78,
) -> List[str]:
    """The resume briefing's markup lines, or ``[]`` when there is nothing.

    Empty list means "no previous run to brief" — a true first launch.
    That distinction is the contract: a briefing that renders zeros when
    no run exists would be a fabricated status, which is precisely the
    defect class this whole feature exists to remove.
    """
    try:
        from cli.runview import briefing_facts, briefing_lines

        row = _last_briefable_run(log_root, repo=repo)
        if not row:
            return []
        task_id = str(row.get("task_id") or "")
        if not task_id:
            return []
        facts = briefing_facts(log_root, task_id)
        if not facts.get("available"):
            return []
        return briefing_lines(facts, width=width)
    except Exception:
        return []


def print_resume_briefing(
    con: Any, log_root: Path, *, repo: Any = None, width: int = 78
) -> bool:
    """Print the resume briefing. Returns True when one was shown.

    Never raises: a briefing that cannot be built is silence, not a
    traceback in front of a person who just typed `vex`.
    """
    try:
        lines = render_resume_briefing(log_root, repo=repo, width=width)
        if not lines:
            return False
        con.print("")
        for line in lines:
            con.print(line)
        return True
    except Exception:
        return False


def _resolve_session_artifact_root(
    repo: Any = None, file_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Resolve this session's artifact root and its placement warnings.

    Both shells (REPL and TUI) and the headless `command_exec` path go
    through this one function, so a forced in-repo log root is gitignored
    and reported identically everywhere.
    """
    try:
        from cli.session import resolve_artifact_root

        return resolve_artifact_root(None, repo, file_config)
    except Exception:
        return {
            "log_root": Path("logs"),
            "source": "default",
            "placement": {},
            "warnings": (),
            "repo_key": "",
        }


def _startup_recovery_offer(
    con: Any,
    log_root: Path,
    repo: Any = None,
    *,
    interactive: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """Offer to quarantine a corrupt conversation at startup.

    A corrupt snapshot used to be swallowed: the load raised, the caller
    caught it, and the session continued with NO conversation and no
    explanation. This surfaces the failure and asks, on a real TTY only,
    whether to quarantine the file (never delete it) and start clean.
    Returns the recovery report when one was performed, else None.
    """
    try:
        from cli.session import (
            recover_corrupt_session,
            startup_recovery_candidate,
        )

        candidate = startup_recovery_candidate(log_root, repo)
        if not candidate:
            return None
    except Exception:
        return None
    con.print(
        f"[vex.warn]session {candidate['session_id']} is unreadable[/] "
        f"[vex.muted]({candidate['error']})[/]"
    )
    backup_note = (
        " a last-known-good backup exists" if candidate.get("backup_available") else ""
    )
    con.print(
        "[vex.muted]the file is kept either way; recovery quarantines a copy "
        f"next to it{backup_note}[/]"
    )
    strategy = "report"
    if interactive is None:
        try:
            import sys as _sys

            interactive = bool(_sys.stdin is not None and _sys.stdin.isatty())
        except Exception:
            interactive = False
    if interactive:
        try:
            answer = con.input(
                "[vex.accent]recover?[/] [vex.muted][y = quarantine + start "
                "clean, N = leave the file and continue without it][/] "
            )
        except Exception:
            answer = ""
        if str(answer or "").strip().lower() in ("y", "yes"):
            strategy = "backup" if candidate.get("backup_available") else "fresh"
    else:
        con.print(
            "[vex.muted]non-interactive start: leaving the file untouched - "
            "run `/recover "
            f"{candidate['session_id']}` or `vex --recover` to quarantine it[/]"
        )
    if strategy == "report":
        return None
    try:
        return recover_corrupt_session(
            log_root, candidate["session_id"], repo, strategy=strategy
        )
    except Exception as exc:
        con.print(f"[vex.error]recovery failed:[/] {escape(str(exc))}")
        return None


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


def _is_first_launch(log_root: Path) -> bool:
    """True when no interactive run has EVER been recorded under this
    log root (no session index, no resumable task dirs) — the state
    that earns the wordmark splash (Task C). Cheap: two stat calls."""
    root = Path(log_root)
    if session_store_path(root).is_file():
        return False
    try:
        for d in root.iterdir():
            if (
                d.is_dir()
                and not d.name.startswith((".", "_"))
                and (d / "trace.jsonl").is_file()
            ):
                return False
    except OSError:
        pass
    return True


#: R2-17 (item 5): the first run used to print about two lines, so the
#: journey never reached its value. These are the three facts a first-time
#: user needs BEFORE typing anything — what this is, what to type, and
#: where the evidence lands — and the honesty promise that makes the
#: product different. Kept to three short beats; a first-run screen that
#: is itself a wall fails the same way the old `/help` did.
#:
#: SUPERSEDED by `cli/onboarding.py` (VEX-PF-06) and kept only as the
#: fallback `render_first_run` uses when that module cannot be imported. The
#: live screen is what / is / one worked example / one next action, because
#: "nothing to memorise" and "the evidence lands above" are not the two
#: things a newcomer actually needs; the commands and the diff are.
_FIRST_RUN_BEATS: Tuple[Tuple[str, str], ...] = (
    (
        "1",
        "say what you want in plain language — a question, a change, "
        "a bug report. nothing to memorise.",
    ),
    (
        "2",
        "every run is recorded and checked: a completed run is only "
        "reported as verified when the tests actually pass.",
    ),
    (
        "3",
        "the evidence lands under the logs root above, and "
        "/help <word> searches every command.",
    ),
)


def say_empty_state(
    say: Any, state_id: str, *, width: int = 78, style: str = "vex.muted"
) -> List[str]:
    """Print one declared empty state through `say`, and return its lines.

    VEX-PF-06. A panel with nothing in it is a dead end; a panel with nothing
    in it and no next step is the same dead end in nicer clothes. Every empty
    surface in this module routes through here so the sentence, the runnable
    door and the anti-clutter budget live in ONE place
    (`cli/onboarding.EMPTY_STATES`) rather than in a dozen string literals.

    Markup comes from `cli.onboarding.escape_lines`, so a repository name, a
    connector label or a logs path containing `[` prints instead of deleting
    the message. Falls back to the sentence ALONE rather than raising: an
    empty state that throws is worse than one that is merely short.

    `width` is a hint, not a contract - the caller does not always know the
    terminal - so the default is the width every other help surface uses.
    """
    degraded: List[str] = []
    try:
        from cli import onboarding
    except Exception:
        try:
            say("[vex.muted]nothing to show here[/]")
        except Exception:
            pass
        return []
    try:
        plain = onboarding.empty_state_lines(state_id, width=int(width or 78))
    except Exception:
        plain = []
    for line in plain:
        try:
            say(f"[{style}]{escape(line)}[/]")
        except Exception as exc:
            # A render failure must NEVER delete a message. `say` raising is
            # exactly that failure - and swallowing it is how a surface ends up
            # printing nothing at all, which is indistinguishable from the
            # empty state not existing. So the PLAIN line goes to stdout,
            # unstyled and unparsed, and the cause is recorded in the return
            # value rather than swallowed.
            degraded.append(f"{type(exc).__name__}: {exc}")
            try:
                import sys as _sys

                _sys.stdout.write(f"{line}\n")
            except Exception:
                pass
    return plain


def _first_run_fallback_lines(repo: Any, log_root: Any, model: str) -> List[str]:
    """The pre-VEX-PF-06 first-run beats, as the last-resort renderer.

    Only reached when `cli.onboarding` cannot be imported. It is kept rather
    than deleted because "the orientation module is missing" is a worse first
    run than "the orientation is one sentence shorter than it should be", and
    a first-run surface that raises is a first-run surface some people never
    see at all.
    """
    from rich.markup import escape

    dot = ui.DOT
    lines = [
        f"[vex.accent]welcome[/] [vex.muted]{dot}[/] "
        f"[{ui.TEXT_PRIMARY}]this is a coding agent for "
        f"{escape(Path(repo).name if repo else 'this repository')}[/]"
    ]
    for number, beat in _FIRST_RUN_BEATS:
        lines.append(
            f"  [vex.accent]{escape(number)}[/] [vex.muted]{escape(beat)}[/]"
        )
    facts = []
    if model:
        facts.append(f"model {escape(str(model))}")
    if log_root:
        facts.append(f"logs {escape(str(log_root))}")
    if facts:
        lines.append(f"  [vex.muted]{dot} {dot.join(facts)}[/]")
    lines.append(
        f"  [vex.muted]{dot}[/] [vex.accent]/help[/] "
        f"[vex.muted]searches all {len(help_index())} commands[/]"
    )
    return lines


def render_first_run(
    repo: Any = None,
    log_root: Any = None,
    *,
    model: str = "",
    connected: bool = False,
    width: int = 78,
) -> List[str]:
    """The first-run orientation, as markup lines.

    Delegates to `cli.onboarding.first_run_lines` (VEX-PF-06), which owns the
    three beats the brief names — WHAT Vex is, ONE worked example, ONE next
    action — plus the three controls a newcomer needs on the first screen
    (`/diff`, `/undo`, `/cancel`) and the configured facts. The wordings live
    in one module because the REPL and the TUI print the SAME bytes and two
    hand-maintained copies of "what is this" drift within a round.

    Markup is assembled from `escape()`d fields only, so a repository name or
    a logs path containing `[` prints instead of deleting the screen. Pure and
    total: a caller that cannot resolve the repo or the logs root still gets a
    usable orientation with those facts omitted rather than an error.
    """
    try:
        from cli import onboarding
    except Exception:
        return _first_run_fallback_lines(repo, log_root, model)
    try:
        plain = onboarding.first_run_lines(
            repo, log_root, model=model, connected=bool(connected), width=int(width or 78)
        )
    except Exception:
        return _first_run_fallback_lines(repo, log_root, model)
    dot = ui.DOT
    from rich.markup import escape

    styled: List[str] = []
    for line in plain:
        text = str(line)
        label, _, body = text.partition(" ")
        if label in ("what", "ask", "next") and body:
            styled.append(
                f"[vex.accent]{escape(label)}[/]"
                f"[vex.muted]{escape(' ' + body)}[/]"
            )
            continue
        if label == "keys":
            # The three affordances get the accent because they are the thing
            # the brief puts on this screen; a uniform wall teaches none of it.
            styled.append(
                f"[vex.accent]{escape(label)}[/] [vex.muted]{dot}[/] "
                f"[{ui.TEXT_PRIMARY}]{escape(body.strip())}[/]"
            )
            continue
        styled.append(f"[vex.muted]{escape(text)}[/]")
    return styled


def print_first_run(
    con: Any,
    repo: Any = None,
    log_root: Any = None,
    *,
    model: str = "",
    connected: bool = False,
    width: int = 78,
) -> bool:
    """Print the first-run orientation. Returns True when shown. Never raises."""
    try:
        for line in render_first_run(
            repo, log_root, model=model, connected=connected, width=width
        ):
            con.print(line)
        return True
    except Exception:
        return False



def run_interactive(argv_quote: str = "", log_root: Optional[Path] = None) -> int:
    """The `vex` no-args session. Returns a process exit code (0/130).

    Assumes stdin is an interactive terminal (the CLI dispatches here only
    when sys.stdin.isatty() and no subcommand was given; scripted callers
    use the flag commands).
    """
    con = ui.console()
    repo = _detect_repo()
    # Safe default artifact location: the harness-owned home keyed by repo,
    # OUTSIDE the user's repository. A forced in-repo root is gitignored and
    # warned about here rather than silently duplicating the source tree.
    _artifact = _resolve_session_artifact_root(repo)
    log_root = log_root or _artifact["log_root"]
    for _warning in _artifact["warnings"]:
        con.print(f"[vex.warn]{escape(_warning)}[/]")
    # Plugins round (Task C): installed plugins' tool verbs extend the
    # BATCH read-only allowlist for this session (best-effort, never
    # raises â€” a broken plugin is skipped by the loader).
    try:
        from cli.plugins import apply_tool_extensions

        apply_tool_extensions()
    except Exception:
        pass
    state: Dict[str, Any] = {
        "model": None,
        "provider": None,
        "plan_preview": None,
        "quiet": False,
        "mode": "auto",
        # the session's repo, kept in state so slash-command handlers
        # (custom commands run fixes) see the CURRENT repo without a
        # new parameter threading through every _slash_command caller
        "repo": str(repo),
        # config-file defaults for custom-command-driven fixes (they
        # call _run_one_fix with file_config=None â€” the session's own
        # load applies, same as plain-language fixes)
        "file_config": None,
        "approval_policy": _commands.ApprovalPolicy(),
        # the resolved artifact root, so a session imported/forked from
        # another repository can be re-homed honestly
        "_log_root": str(log_root),
    }
    last: Dict[str, Any] = {}
    from cli.vexconfig import ensure_first_run, maybe_scaffold_repo, merged_settings

    # First-run flow: create the global settings file when missing (the
    # task's "created by Vex's own first-run flow" requirement), with a
    # one-time notice; a read-only home never crashes the session.
    created, gp = ensure_first_run()
    if created:
        con.print(
            f"[vex.ok]first run:[/] [vex.muted]created global settings at {gp} "
            "â€” `vex config list` to see what's set[/]"
        )
    # First-`vex`-in-a-repo: scaffold <repo>/.vex/ (settings.toml +
    # settings.local.toml + commands/ + skills/ examples) when inside a
    # git repo. Never overwrites, never outside a repo, never raises.
    scaffold = maybe_scaffold_repo()
    if scaffold and scaffold.get("created"):
        con.print(
            "[vex.ok]repo setup:[/] [vex.muted]created "
            + ", ".join(f".vex/{c}" for c in scaffold["created"])
            + " â€” `vex config list` shows the chain[/]"
        )
    # Two-tier chain (global + project .vex/settings{,.local}.toml);
    # env vars are applied per-fix by apply_config_defaults so explicit
    # session pins still win.
    file_config = merged_settings()
    # First-run onboarding: no usable model/auth anywhere in the chain
    # -> offer the inline wizard ONCE here (skippable via /skip,
    # VEX_NO_ONBOARD=1; never prompts without a TTY â€” pipe-safe).
    try:
        from cli.onboard import maybe_onboard_repl

        file_config = maybe_onboard_repl(file_config) or {}
    except Exception:
        pass
    state["file_config"] = file_config
    ui.set_theme(config=file_config)
    con = ui.console()
    if file_config.get("log_verbosity") == "quiet":
        state["quiet"] = True
    if file_config.get("plan_preview") is not None:
        state["plan_preview"] = bool(file_config["plan_preview"])

    # Persistent conversation (opencode-style session): one state file
    # per conversation under <log_root>/_conversations/ holding the
    # multi-turn transcript + input history + compacted summary.
    # Memory-first: structural + decision memory is queried
    # automatically on session start (no manual `vex memory` calls).
    try:
        from cli import session as _session_mod

        _startup_recovery_offer(con, log_root, repo)
        conversation = _session_mod.load_latest_session(log_root, repo)
        state["conversation"] = conversation
        _append_history = _session_mod.append_history
        _append_turn = _session_mod.append_turn
        _save_conversation = _session_mod.save_session
        _memory_brief = _session_mod.session_memory_brief
    except Exception:
        conversation = None

    # Two-tier display (branding round Task C): the wordmark splash
    # shows on FIRST launch into an empty session (no prior interactive
    # runs recorded under this log root); every regular session after
    # that gets the one-line compact header. Matching OpenCode's
    # empty-state pattern, not a giant logo every time.
    _print_session_head(
        repo, log_root, state, file_config, first_launch=_is_first_launch(log_root)
    )
    # R2-17: the resume briefing. One honest screen on start — what
    # happened, what it cost, what is UNVERIFIED, what happens next —
    # rendered from the previous run's own journal. Skipped on a genuine
    # first launch (there is nothing to brief); shown whenever a previous
    # run exists, because that is the first thing a daily user sees.
    if not _is_first_launch(log_root):
        print_resume_briefing(con, log_root, repo=repo)
    # Memory-first, session start: surface repo-scoped decisions +
    # structural index state automatically (best-effort, muted lines).
    try:
        for mem_line in _memory_brief(repo, log_root):
            con.print(f"[vex.muted]{mem_line}[/]")
    except Exception:
        pass
    if (
        isinstance(conversation, dict)
        and str(conversation.get("summary") or "").strip()
    ):
        con.print(
            f"[vex.muted]session context: {str(conversation['summary'])[:200]}[/]"
        )
    try:
        from cli.session import build_session_context, format_session_context_status

        context_status = format_session_context_status(
            build_session_context(
                conversation,
                repo=repo,
                task={"issue_text": "session context"},
                token_budget=int((file_config or {}).get("session_context_tokens", 12000)),
            )
        )
        if context_status:
            con.print(f"[vex.muted]context: {escape(str(context_status)[:220])}[/]")
    except Exception:
        pass

    # Steering round, Task A: the reader thread owns stdin for the
    # whole session so lines typed WHILE a harness run is live can
    # STEER it (injected into the run's steering journal; consumed at
    # the loop's safe checkpoints) instead of being invisible until the
    # run finishes. Idle lines flow to this loop unchanged; a console
    # Ctrl+C while a run is live is delivered at the MAIN thread (inside
    # run_task â€” its KI path keeps checkpoints), while idle it stays the
    # leave behavior. The prompt still renders here (the reader never
    # prints it â€” a blocked run may need the console).
    _init_readline_history(log_root)
    reader = _ReplReader(threading.get_ident(), state)
    while True:
        try:
            con.print(
                f"[vex.accent]vex[/][vex.muted] {ui.GLYPHS['prompt']}[/] ", end=""
            )
            line = reader.getline().strip()
        except EOFError:
            con.print("[vex.muted]bye[/]")
            return 0
        except KeyboardInterrupt:
            # Idle Ctrl+C (a run's KI is swallowed by the reader when
            # live, and by run_task when blocked) = the old leave.
            con.print("\n[vex.muted]bye[/]")
            return 0
        if not line:
            continue
        low = line.lower()

        # Persistent conversation: every raw input line joins the
        # session history (Ctrl+R / history search surface).
        if isinstance(conversation, dict):
            try:
                _append_history(conversation, line)
                _save_conversation(log_root, conversation)
            except Exception:
                pass
        _note_readline_history(log_root, line)

        # -- slash commands (Task B) -----------------------------------
        if low.startswith("/"):
            try:
                handled = _slash_command(line, low, last, log_root, state)
            except Exception as exc:
                from cli.errors import explain_exception

                explain_exception(exc, save_traceback=True)
                continue
            if handled == _COMMAND_EXIT:
                # `/quit` is a COMMAND on this surface now (R2-18), not a
                # loop fast path that skipped the preflight and the
                # record. The exit is the same; the record is the
                # difference.
                return 0
            if handled == "continue":
                continue
            if handled is None:
                # /clear swaps state["conversation"] for a fresh file â€”
                # the loop's cached handle must follow it (the old file
                # stays on disk; subsequent turns join the new one).
                if isinstance(state.get("conversation"), dict):
                    conversation = state["conversation"]
                continue
            # unknown slash command: hint (the message itself is printed
            # by _slash_command; this branch just stops processing)
            continue

        # -- session commands (bare words kept from the first pass) ----
        if low in ("exit", "quit", "q", "/quit", "/exit"):
            con.print("[vex.muted]bye[/]")
            return 0
        if low in ("help", "?"):
            con.print(render_help(line[4:].strip()))
            continue
        if low.startswith("repo ") or (
            _looks_like_path(low) and not low.startswith(("fix", "in"))
        ):
            path = line[5:].strip() if low.startswith("repo ") else line
            cand = Path(path).expanduser().resolve()
            if cand.is_dir():
                repo = cand
                state["repo"] = str(repo)
                con.print(f"[vex.ok]repo {ui.GLYPHS['arrow']} {repo}[/]")
            else:
                con.print(f"[vex.error]not a directory: {cand}[/]")
            continue
        if low.startswith("model "):
            state["model"] = line[6:].strip()
            con.print(f"[vex.ok]model pinned {ui.GLYPHS['arrow']} {state['model']}[/]")
            continue

        # @path mentions: attach the referenced repo files' content
        # (expanded from scan_repo_files-grade resolution) before the
        # line is classified â€” a mention is conversation context, and
        # the expanded text is what the harness runs on.
        if "@" in line and isinstance(conversation, dict):
            try:
                from cli.session import expand_at_mentions

                try:
                    from cli.tui import scan_repo_files

                    _files = scan_repo_files(repo)
                except Exception:
                    _files = []
                expanded, inserted = expand_at_mentions(line, repo, _files)
                if inserted:
                    con.print(f"[vex.muted]attached: {', '.join(inserted)}[/]")
                    line = expanded
                from cli.session import expand_symbol_mentions

                expanded, inserted = expand_symbol_mentions(line, repo)
                if inserted:
                    con.print(f"[vex.muted]attached symbols: {', '.join(inserted)}[/]")
                    line = expanded
            except Exception:
                pass

        selected_mode = str(state.get("mode") or "auto").lower()
        if selected_mode not in ("", "auto"):
            try:
                from cli.intent import classify as _mode_intent

                mode_intent = _mode_intent(line)
                if mode_intent.kind == "convo":
                    con.print(
                        f"[vex.muted]{escape(str(mode_intent.reply or 'what would you like to do?'))}[/]"
                    )
                    continue
            except Exception:
                pass
            try:
                state["session_context"] = _build_agent_session_context(
                    line, repo, state, log_root, last
                )
                result_info = _run_one_mode(
                    line,
                    repo,
                    state,
                    log_root,
                    file_config,
                    mode=selected_mode,
                    session_context=state.get("session_context"),
                )
            except KeyboardInterrupt:
                con.print("\n[vex.warn]interrupted[/]")
                continue
            except Exception as exc:
                from cli.errors import explain_exception

                explain_exception(exc, save_traceback=True)
                continue
            if isinstance(conversation, dict):
                try:
                    _append_turn(conversation, "user", line)
                    _append_turn(
                        conversation,
                        "assistant",
                        str((result_info or {}).get("answer") or (result_info or {}).get("status") or ""),
                        task_id=(result_info or {}).get("task_id"),
                    )
                    _save_conversation(log_root, conversation)
                except Exception:
                    pass
            if result_info is not None:
                last = result_info
            continue

        # -- AGT-09: a new prompt COMMITS the staged code revert ------
        # Placed AFTER every bare session command and slash command, so
        # `repo`, `model`, `help` and `/whatever` never silently revert code,
        # and BEFORE the agent dispatch, so the request runs against the tree
        # the user actually meant.
        if _commit_staged_undo_for_prompt(state, log_root):
            continue

        # -- agent dispatch: classify the line (deterministic rules
        # first; ONE cheap model call for the gray zone) into question
        # (read-only answer), agent_task (ONE tool loop for fix/build/
        # refactor/run/debug â€” not 4 modes), or chit_chat (inline reply,
        # nothing launched). `vex fix` (the flag command) still drives
        # harness.core.run_task directly â€” this dispatch is only the
        # interactive `vex` session engine. The cost asymmetry stands: a
        # wrong run burns minutes + model budget, a question costs one
        # line â€” so unsure input asks instead of launching.
        from harness.agent_loop import classify_agent_input

        intent = classify_agent_input(
            line,
            {
                **(file_config or {}),
                "model": state.get("model") or (file_config or {}).get("model"),
                "provider": state.get("provider")
                or (file_config or {}).get("provider"),
            },
        )
        if intent.kind == "chit_chat":
            con.print(f"[vex.muted]{intent.reply or 'what would you like to do?'}[/]")
            if isinstance(conversation, dict):
                try:
                    _append_turn(conversation, "user", line)
                    _append_turn(conversation, "assistant", intent.reply or "")
                    _save_conversation(log_root, conversation)
                except Exception:
                    pass
            continue

        if intent.kind == "question":
            try:
                result_info = _run_one_question(
                    line, repo, state, log_root, file_config
                )
            except KeyboardInterrupt:
                con.print("\n[vex.warn]interrupted[/]")
                continue
            except Exception as exc:
                from cli.errors import explain_exception

                explain_exception(exc, save_traceback=True)
                continue
            if isinstance(conversation, dict):
                try:
                    _append_turn(conversation, "user", line)
                    _append_turn(
                        conversation,
                        "assistant",
                        str((result_info or {}).get("answer") or ""),
                        task_id=(result_info or {}).get("task_id"),
                    )
                    _save_conversation(log_root, conversation)
                except Exception:
                    pass
            if result_info is not None:
                last = result_info
            continue

        # -- an agent task: fix/build/refactor/run/debug share ONE loop --
        # (the sentence IS the task; the loop works on the live repo).
        # _run_one_fix/_run_one_build/_run_one_research stay as legacy
        # programmatic entries (and `vex fix` still uses run_task) but
        # the session no longer dispatches through them.
        try:
            state["session_context"] = _build_agent_session_context(
                line, repo, state, log_root, last
            )
            import inspect as _inspect

            agent_runner = _run_one_agent
            try:
                parameters = _inspect.signature(agent_runner).parameters
                accepts_session_context = "session_context" in parameters or any(
                    parameter.kind == _inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
            except (TypeError, ValueError):
                accepts_session_context = True
            agent_kwargs = (
                {"session_context": state.get("session_context")}
                if accepts_session_context
                else {}
            )
            result_info = agent_runner(
                line,
                repo,
                state,
                log_root,
                file_config,
                **agent_kwargs,
            )
        except KeyboardInterrupt:
            con.print(
                "\n[vex.warn]interrupted â€” partial edits stay in the repo; "
                "[/][vex.muted]/diff undo reverts them[/]"
            )
            continue
        except Exception as exc:  # never dump a traceback on the user
            from cli.errors import explain_exception

            explain_exception(exc, save_traceback=True)
            continue
        if isinstance(conversation, dict):
            try:
                _append_turn(conversation, "user", line)
                _append_turn(
                    conversation,
                    "assistant",
                    str(
                        (result_info or {}).get("answer")
                        or (result_info or {}).get("status")
                        or ""
                    ),
                    task_id=(result_info or {}).get("task_id"),
                )
                _save_conversation(log_root, conversation)
            except Exception:
                pass
        if result_info is None:
            continue
        last = result_info
    return 0


def _pending_approval_request(
    log_root: Path, task_id: str
) -> Optional[Dict[str, Any]]:
    """Return the pending approval request for one task, or None.

    Assumes the task id already resolved through the shared task-id guard;
    an unreadable gate directory simply means "nothing pending" so the
    command preflight stays total.
    """
    try:
        from runtime import approval as approval_mod
    except ImportError:
        return None
    if _safe_task_dir(str(task_id), log_root) is None:
        return None
    gate = Path(log_root) / f"{task_id}.runtime" / "approval"
    try:
        request = approval_mod.pending_request(str(gate))
    except Exception:
        return None
    if not isinstance(request, dict):
        return None
    request_task_id = str(request.get("task_id") or "")
    if request_task_id and request_task_id != str(task_id):
        return None
    return request


def _decide_pending(
    log_root: Path,
    task_id: str,
    approve: bool,
    scope: str = "once",
    policy: Optional[_commands.ApprovalPolicy] = None,
) -> Optional[bool]:
    """Decide one pending request and retain the normalized grant scope.

    Interactive approval gates use runtime's file protocol. The optional
    policy uses ``cli.commands.ApprovalPolicy`` so once/session/path/command
    grants have the same exact-effect semantics on the REPL, TUI, and
    headless surfaces. Returns the verdict, or None when nothing is pending.
    """
    con = ui.console()
    try:
        from runtime import approval as approval_mod
    except ImportError:
        con.print("[vex.error]runtime.approval unavailable[/]")
        return None
    if _safe_task_dir(str(task_id), log_root) is None:
        con.print(f"[vex.error]invalid task id: {escape(str(task_id))}[/]")
        return None
    gate = Path(log_root) / f"{task_id}.runtime" / "approval"
    req = approval_mod.pending_request(str(gate))
    if req is None:
        return None
    request_task_id = str(req.get("task_id") or "")
    if request_task_id and request_task_id != str(task_id):
        con.print("[vex.error]approval request identity mismatch[/]")
        return None
    if not req.get("diff") and req.get("summary"):
        pass  # some requests carry only a summary
    normalized_scope = _commands.normalize_approval_scope(scope)
    if approve and normalized_scope != "once" and policy is not None:
        policy.record(_commands.approval_request_view(req), normalized_scope)
    approval_mod.decide(str(gate), approve=approve)
    suffix = f" [vex.muted]({normalized_scope})[/]" if approve else ""
    con.print(
        f"[vex.{'ok' if approve else 'error'}]"
        f"{'approved' if approve else 'rejected'}[/] â€” the worker "
        f"continues {'with' if approve else 'without'} the fix{suffix}"
    )
    return approve


def last_rationale_text(log_root: Path, task_id: str) -> Optional[str]:
    """The rationale.md body for a run (None when absent/unreadable)."""
    try:
        rat = Path(log_root) / str(task_id or "") / "rationale.md"
        if not rat.is_file():
            return None
        text = rat.read_text(encoding="utf-8", errors="replace").strip()
        return text or None
    except Exception:
        return None


def _render_review(last: Dict[str, Any], log_root: Path) -> None:
    """/review: the last fix's diff + its rationale together.

    Never raises; an absent diff/rationale renders one honest line each.

    BOTH branches of each try go through the shared sanitiser
    (``cli.ui.sanitize_text``) and then ``escape``. The success branch used to
    sanitise and the EXCEPT branch two lines below it printed the raw body —
    and a renderer that fails is precisely when the value is most likely to be
    hostile, because the failure came from the value. A degraded path that
    discloses is worse than the error it was hiding. (VEX-TERM-UX-09 blocker 1.)
    """
    con = ui.console()
    diff = (last or {}).get("diff")
    if diff:
        con.print("[vex.muted]diff:[/]")
        try:
            ui.print_diff(str(diff))
        except Exception:
            con.print(escape(ui.sanitize_text(diff))[:4000])
    else:
        say_empty_state(con.print, "no_diff")
    tid = (last or {}).get("task_id")
    body = last_rationale_text(log_root, tid) if tid else None
    if body:
        con.print()
        con.print("[vex.muted]rationale:[/]")
        try:
            from rich.markdown import Markdown

            con.print(Markdown(ui.sanitize_text(body)))
        except Exception:
            con.print(escape(ui.sanitize_text(body))[:4000])
    else:
        con.print("[vex.muted]no rationale recorded for the last run[/]")


def _readline_history_path(log_root: Path) -> Path:
    """The REPL's persistent input-history file (readline backend)."""
    return Path(log_root) / ".vex-input-history"


def _init_readline_history(log_root: Path) -> None:
    """Load prior input lines into readline (Up-arrow recall).

    Best-effort: stdlib readline exists on POSIX; on plain Windows it
    is absent and history simply stays in the conversation file
    (searchable via the TUI's Ctrl+R). Never raises.
    """
    try:
        import readline  # type: ignore[import-not-found]

        hist = _readline_history_path(log_root)
        readline.set_history_length(200)
        if hist.is_file():
            try:
                readline.read_history_file(str(hist))
            except OSError:
                pass
    except Exception:
        pass


def _note_readline_history(log_root: Path, line: str) -> None:
    """Append one line to the readline history + history file."""
    try:
        text = (line or "").strip()
        if not text:
            return
        try:
            import readline  # type: ignore[import-not-found]

            readline.add_history(text)
            try:
                readline.write_history_file(str(_readline_history_path(log_root)))
            except OSError:
                pass
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shared slash helpers (surface-wiring round) â€” one implementation serves
# BOTH shells via a `say(markup)` hook (REPL: console print; TUI: transcript).
# Every helper is total: bad input degrades to a usage/honest line, never
# a traceback. Auth/config writers are CALLED here (onboard/vexconfig),
# never reimplemented.
# ---------------------------------------------------------------------------


def _slash_say_default(markup: str) -> None:
    """The REPL's say hook (console print idiom). Never raises."""
    try:
        ui.console().print(markup)
    except Exception:
        pass


def _set_handler_result(
    state: Dict[str, Any], status: str, exit_code: int = 0
) -> None:
    """Record one command handler's semantic outcome for every surface."""
    state["_handler_result"] = {"status": str(status), "exit_code": int(exit_code)}


def trace_usage_sum(trace_file: Path) -> Tuple[int, int, float]:
    """(model_calls, tokens, cost_usd) summed over a trace.jsonl's
    model_response usage records. Assumes trace_file is a trace path;
    missing/unreadable/malformed content yields zeros. Never raises."""
    calls = 0
    tokens = 0
    cost = 0.0
    try:
        p = Path(trace_file)
        if not p.is_file():
            return (0, 0, 0.0)
        from cli.runview import event_parts

        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind, data, _timestamp, _identity = event_parts(obj)
            if kind not in ("model_response", "model_completed"):
                continue
            usage = data.get("usage") or data
            try:
                calls += 1
                tokens += int(
                    usage.get("tokens", usage.get("total_tokens", usage.get("completion_tokens", 0))) or 0
                )
                cost += float(
                    usage.get("cost", usage.get("cost_usd", usage.get("usd", 0.0))) or 0.0
                )
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return (calls, tokens, cost)


def session_spend_total(log_root: Path) -> Tuple[int, int, float]:
    """Usage-sum over EVERY task trace under log_root (the session total
    for /cost). Skips index/hidden dirs and worker sidecars. Never raises."""
    calls = 0
    tokens = 0
    cost = 0.0
    try:
        root = Path(log_root)
        if not root.is_dir():
            return (0, 0, 0.0)
        for d in root.iterdir():
            try:
                if not d.is_dir() or d.name.startswith((".", "_")):
                    continue
                if d.name.endswith(".runtime"):
                    continue
                c, t, m = trace_usage_sum(d / "trace.jsonl")
                calls += c
                tokens += t
                cost += m
            except Exception:
                continue
    except Exception:
        pass
    return (calls, tokens, cost)


def trace_cache_summary(trace_file: Path) -> Dict[str, Any]:
    """Prompt-cache receipt summed over a trace.jsonl or model_ledger.jsonl.

    Reads the same per-call rows the router wrote, so the numbers are the
    provider's own reported cache tokens, not an estimate. Both row shapes are
    accepted: a harness ``model_response`` usage record and a runtime
    ``model_routed``/ledger row. A trace with no cache fields yields
    ``{"calls": 0, "available": False}`` â€” the render then says "no cache
    data", which is honest, rather than printing a 0% hit rate that would read
    as a measured failure.

    Never raises.
    """
    summary: Dict[str, Any] = {
        "available": False,
        "calls": 0,
        "hits": 0,
        "decided": 0,
        "cached_tokens": 0,
        "creation_tokens": 0,
        "statuses": {},
        "hit_rate": 0.0,
    }
    decided = {"hit", "partial", "creation", "miss"}
    hits = {"hit", "partial"}
    try:
        p = Path(trace_file)
        if not p.is_file():
            return summary
        from cli.runview import event_parts

        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind, data, _timestamp, _identity = event_parts(obj)
            if kind in ("model_response", "model_completed"):
                usage = data.get("usage") or data
            elif kind in ("model_routed", "model_call"):
                usage = data
            else:
                continue
            if not isinstance(usage, dict):
                continue
            status = str(usage.get("cache_status") or "")
            if not status:
                continue
            summary["available"] = True
            summary["calls"] += 1
            summary["statuses"][status] = summary["statuses"].get(status, 0) + 1
            if status in decided:
                summary["decided"] += 1
            if status in hits:
                summary["hits"] += 1
            try:
                summary["cached_tokens"] += int(
                    usage.get("cached_input_tokens") or 0
                )
                summary["creation_tokens"] += int(
                    usage.get("cache_creation_input_tokens") or 0
                )
            except (TypeError, ValueError):
                continue
    except Exception:
        return summary
    if summary["decided"]:
        summary["hit_rate"] = summary["hits"] / summary["decided"]
    return summary


def session_cache_total(log_root: Path) -> Dict[str, Any]:
    """Cache receipt over EVERY task trace under log_root. Never raises."""
    totals = {
        "available": False,
        "calls": 0,
        "hits": 0,
        "decided": 0,
        "cached_tokens": 0,
        "creation_tokens": 0,
        "hit_rate": 0.0,
    }
    try:
        root = Path(log_root)
        if not root.is_dir():
            return totals
        for d in root.iterdir():
            try:
                if not d.is_dir() or d.name.startswith((".", "_")):
                    continue
                if d.name.endswith(".runtime"):
                    continue
                part = trace_cache_summary(d / "trace.jsonl")
                if not part.get("available"):
                    continue
                totals["available"] = True
                totals["calls"] += int(part["calls"])
                totals["hits"] += int(part["hits"])
                totals["decided"] += int(part["decided"])
                totals["cached_tokens"] += int(part["cached_tokens"])
                totals["creation_tokens"] += int(part["creation_tokens"])
            except Exception:
                continue
    except Exception:
        pass
    if totals["decided"]:
        totals["hit_rate"] = totals["hits"] / totals["decided"]
    return totals


def _format_cache_line(label: str, summary: Dict[str, Any]) -> str:
    """Render one cache line, or an honest 'no data' line."""
    if not summary.get("available"):
        return f"[vex.muted]{label} cache: no cache data in the ledger[/]"
    rate = float(summary.get("hit_rate") or 0.0)
    return (
        f"[vex.muted]{label} cache: [/][vex.accent2]{rate * 100:.0f}% hit[/]"
        f"[vex.muted] of {int(summary.get('decided') or 0)} decided call(s) "
        f"· {int(summary.get('cached_tokens') or 0):,} cached input tok "
        f"· {int(summary.get('creation_tokens') or 0):,} cache-write tok[/]"
    )


def _render_cost(
    last: Dict[str, Any],
    log_root: Path,
    say: Optional[Any] = None,
    task_id: Optional[str] = None,
) -> None:
    """Render last-run and session usage totals AND reconcile the ledger.

    R2-17: the conversation's own `trace.jsonl` usage rows are only part
    of the spend. Every other model call the product makes — the routing
    classifier, a failed attempt, a retry, a call made outside the main
    conversation — is recorded in the router's own per-call ledger and
    used to leave a real charge with no audit trail. This now shows BOTH
    views and the difference between them, so `/cost` reconciles against
    the ledger instead of quietly under-reporting it.

    `task_id` lets a live shell inspect the active trace before the
    session's `last` dictionary has been populated. Missing or malformed
    traces render zeros and never raise.
    """
    say = say or _slash_say_default
    try:
        tid = str(task_id or (last or {}).get("task_id") or "")
        if tid:
            c, t, m = trace_usage_sum(Path(log_root) / tid / "trace.jsonl")
            say(
                f"[vex.accent]{'live run' if task_id else 'last run'}[/] "
                f"[vex.muted]{escape(str(tid))} — {c} model call(s) "
                f"· {t:,} tokens · [/][vex.accent2]{ui.fmt_cost(m)}[/]"
            )
            say(
                _format_cache_line(
                    "run",
                    trace_cache_summary(Path(log_root) / tid / "trace.jsonl"),
                )
            )
            _render_ledger_reconciliation(log_root, tid, say)
        else:
            say_empty_state(say, "no_runs")
        c, t, m = session_spend_total(log_root)
        say(
            f"[vex.accent]session total[/] [vex.muted]{c} model call(s) "
            f"· {t:,} tokens · [/][vex.accent2]{ui.fmt_cost(m)}[/]"
        )
        say(_format_cache_line("session", session_cache_total(log_root)))
    except Exception as exc:
        say(f"[vex.muted](cost unavailable: {type(exc).__name__})[/]")


def _render_ledger_reconciliation(
    log_root: Path, task_id: str, say: Any
) -> None:
    """Print the ledger-vs-conversation reconciliation for one run.

    Reports the calls the conversation's own view could not see, the spend
    they account for, and any UNPRICED call — an unpriced call reads as
    UNKNOWN, never as free, because "we did not measure it" and "it cost
    nothing" are different claims. A run with no ledger says so with a
    reason instead of printing a zero that reads as "we checked".
    """
    from cli.runview import cost_reconciliation, model_call_receipts

    try:
        report = cost_reconciliation(log_root, task_id)
        if not report.get("ledger_available"):
            say(
                f"[vex.muted]   ledger[/] {escape(str(report.get('reason') or 'unavailable'))}[/]"
            )
            return
        unpriced = int(report.get("unpriced_calls") or 0)
        extra_calls = int(report.get("unreceipted_calls") or 0)
        extra_cost = float(report.get("unreceipted_cost_usd") or 0.0)
        say(
            f"[vex.accent]   ledger[/] [vex.muted]{report['ledger_calls']} call(s) "
            f"· {int(report['ledger_tokens']):,} tokens · "
            f"[/][vex.accent2]{ui.fmt_cost(float(report['ledger_cost_usd']))}[/]"
            f" [vex.muted]· {report['trace_calls']} of them in the conversation[/]"
        )
        if extra_calls or extra_cost > 0.0:
            # The whole point: spend the conversation's view could not see.
            say(
                f"[vex.warn]   outside the conversation[/] {extra_calls} call(s) "
                f"· [/][vex.accent2]{ui.fmt_cost(extra_cost)}[/] "
                f"[vex.muted](router ledger: classifier, retries, failed attempts)[/]"
            )
        if unpriced:
            # Never "$0.0000" for a call nobody priced.
            say(
                f"[vex.warn]   unpriced[/] {unpriced} call(s) had no recorded cost "
                f"[vex.muted](unknown, not free)[/]"
            )
        receipts = model_call_receipts(log_root, task_id)
        failures = [r for r in receipts if r.get("outcome") not in ("ok", "")]
        if failures:
            say(
                f"[vex.muted]   {len(failures)} of {len(receipts)} call(s) did not complete[/]"
            )
        say(
            f"[vex.muted]   /cost detail[/] per-call receipts: "
            f"{len(receipts)} recorded[/]"
        )
    except Exception as exc:
        say(f"[vex.muted]   (ledger unavailable: {type(exc).__name__})[/]")


def _render_cost_receipts(log_root: Path, task_id: str, say: Any) -> None:
    """Print the per-model-call receipt list for one run.

    One bounded line per call from the router's own ledger, so a user can
    see WHICH call spent the money, not just a total. Assumes the ledger
    may be absent (an honest "no ledger for this run" line) and never
    raises.
    """
    from cli.runview import model_call_receipts

    try:
        receipts = model_call_receipts(log_root, task_id)
        if not receipts:
            say(
                "[vex.muted]no model-call ledger for this run "
                "(only the conversation's own usage rows exist)[/]"
            )
            return
        say(f"[vex.accent]model call receipts[/] [vex.muted]{escape(str(task_id))}[/]")
        for index, row in enumerate(receipts, start=1):
            model = str(row.get("model") or "?")
            hint = str(row.get("hint") or "")
            tier = f" · {hint}" if hint else ""
            outcome = str(row.get("outcome") or "unknown")
            priced = row.get("priced")
            money = (
                ui.fmt_cost(float(row.get("cost_usd") or 0.0))
                if priced
                else "cost unknown"
            )
            detail = f"{int(row.get('tokens') or 0):,} tokens"
            if row.get("streamed"):
                detail += " · streamed"
            if outcome not in ("ok", "unknown"):
                detail += f" · {outcome}"
                if row.get("reason"):
                    detail += f": {str(row['reason'])[:60]}"
            say(
                f"  [vex.muted]{index:>2}[/] [vex.accent]{escape(model)}[/]"
                f"[vex.muted]{escape(tier)}[/] [vex.muted]{detail} · "
                f"[/][vex.accent2]{money}[/]"
            )
    except Exception as exc:
        say(f"[vex.muted](receipts unavailable: {type(exc).__name__})[/]")


def _render_skills(repo: Any, say: Optional[Any] = None, filt: str = "") -> None:
    """`/skills [filter]`: discovered skills + origins. Assumes repo is
    the session repo (None = global/plugin roots only). Never raises."""
    say = say or _slash_say_default
    try:
        from harness.skills import discover_skills

        try:
            skills = discover_skills(repo_path=str(repo) if repo else None)
        except Exception:
            skills = []
        q = (filt or "").strip().lower()
        if q:
            skills = [
                s
                for s in skills
                if q in str(getattr(s, "name", "")).lower()
                or q in str(getattr(s, "description", "")).lower()
            ]
        if not skills:
            say(
                "[vex.muted]no skills match[/]"
                if q
                else "[vex.muted]no skills discovered "
                "(project .vex/skills/ + global + plugins)[/]"
            )
            return
        head = "[vex.accent]skills[/]"
        if q:
            head += f" [vex.muted]matching {escape(repr(filt.strip()))}[/]"
        say(head)
        for s in skills:
            # A skill NAME and a DESCRIPTION are data written by whoever
            # authored the SKILL.md, not by us. Unescaped, a description
            # containing `[x]` is parsed as a markup tag and the words around
            # it are deleted from the screen - a render failure that erases a
            # message rather than printing it. `escape()` on every field is
            # what makes the line render the way the file reads.
            name = escape(str(getattr(s, "name", "?")))
            origin = escape(str(getattr(s, "origin", "?")))
            desc = escape(
                str(getattr(s, "description", "") or "").replace("\n", " ")
            )
            say(
                f"  [vex.accent]/{name}[/] [vex.muted]({origin})[/]"
                + (f" [vex.muted]{desc[:100]}[/]" if desc else "")
            )
    except Exception as exc:
        say(f"[vex.muted](skills unavailable: {type(exc).__name__})[/]")


# ---------------------------------------------------------------------------
# Subcommand dispatch (VEX-CS-01): three browsers become verbs
# ---------------------------------------------------------------------------
#
# `/plugins`, `/mcp` and `/skills` were three ways to LOOK at a thing. The
# functions that CHANGE a thing were written, tested and reachable only from
# argparse, which is why the interface felt like a REPL: a REPL filters, an
# agent takes verbs.
#
# Every handler here ROUTES to the implementation that already exists and
# renders a receipt. None of them re-implements a command's behaviour, and the
# argparse surface still dispatches to the same functions, so there is one
# implementation per verb and two doors onto it.
#
# Two rules this block exists to hold:
#
#   * NO ARGUMENT opens the interactive menu; a SUBCOMMAND acts and RETURNS.
#     A verb that ends in a dialog is not a verb, and the registry enforces
#     that in `cli.commands`, not here.
#   * A verb that touches the filesystem or a settings file asks for the
#     permission its registry row declares, and the preflight has already
#     refused the in-flight and missing-permission cases before we get here.
#     These handlers therefore never re-check what the registry checked.

#: The receipt every subcommand renders. A dict rather than printed lines so a
#: caller (and a test) can assert on WHAT happened rather than on how it
#: looked. ``lines`` is PLAIN text: the caller escapes it, because a plugin
#: name or a connector label is DATA and a data string carrying ``[`` would
#: be eaten by a markup parser rather than printed.
SubcommandReceipt = Dict[str, Any]


def _receipt(
    verb: str,
    ok: bool,
    lines: List[str],
    *,
    payload: Optional[Mapping[str, Any]] = None,
) -> SubcommandReceipt:
    """Return one plain receipt for a subcommand."""
    return {
        "verb": verb,
        "ok": bool(ok),
        "lines": [str(line) for line in lines],
        "payload": dict(payload or {}),
    }


#: The width a markup round-trip renders at. A receipt is plain text on its
#: way out, so it must be rendered at the width it will be READ at or a long
#: line wraps and the receipt claims a break the reader never saw. 78 is the
#: width the REPL's own renderers default to.
_RECEIPT_WIDTH = 78


def _markup_to_plain(text: str) -> List[str]:
    """Render one markup string to PLAIN lines through a real rich Console.

    Needed because a legacy renderer (``_render_skills``) emits markup and this
    round's receipts are plain text that the caller escapes. Escaping markup
    without rendering it first would print ``[vex.accent]`` to the reader.

    ``no_color=True`` and ``force_terminal=False`` are what make the output
    plain: a Console that thinks it is on a terminal emits the escape bytes
    back, and the receipt's "plain" claim would be a claim about the source
    rather than about the bytes. Width is fixed for the same reason.
    """
    import io as _io

    from rich.console import Console as _Console

    buffer = _io.StringIO()
    console = _Console(
        file=buffer,
        width=_RECEIPT_WIDTH,
        no_color=True,
        force_terminal=False,
        soft_wrap=False,
    )
    try:
        console.print(str(text))
    except Exception:
        # A render failure must never delete the message. Fall back to the
        # source with the tags removed, which is ugly and readable rather
        # than pretty and absent.
        return [_MARKUP_TAG.sub("", str(text))]
    return buffer.getvalue().splitlines() or [""]


#: ``[vex.role]`` and friends, for the fallback path only.
_MARKUP_TAG = re.compile(r"\[/?[a-zA-Z0-9_. #|]*\]")


def _print_receipt(say: Any, receipt: Mapping[str, Any]) -> None:
    """Render one receipt through a caller's ``say``.

    Escapes every line on the way out. The lines are PLAIN; a plugin name or a
    repository path is data, and rich parses ``[`` as a markup tag - a render
    failure there deletes the message instead of printing it.
    """
    for line in receipt.get("lines") or ():
        say(f"[vex.muted]{escape(str(line))}[/]")


def _render_subcommand_receipt(
    con: Any,
    receipt: Mapping[str, Any],
    spec: Any,
    args: str,
) -> None:
    """Print one subcommand receipt, and what the person can do next.

    A verb that returns a receipt and then ends in a dialog is not a verb, so
    nothing here blocks: the lines print and the caller returns. The footer is
    the SAME sentence the preflight uses for a usage error
    (:func:`cli.commands.command_usage` and ``command_recovery_hint``), so a
    refusal and a success are worded by one authority rather than two.
    """
    verb = str(receipt.get("verb") or "")
    ok = bool(receipt.get("ok"))
    tone = "vex.ok" if ok else "vex.error"
    lines = [str(line) for line in receipt.get("lines") or ()]
    if lines:
        head, *tail = lines
        con.print(f"[{tone}]{escape(head)}[/]")
        for line in tail:
            con.print(f"[vex.muted]{escape(line)}[/]")
    if not ok:
        con.print(f"[vex.muted]{escape(_commands.command_usage(spec))}[/]")
        con.print(
            f"[vex.muted]{escape(_commands.command_recovery_hint(spec))}[/]"
        )


def _fail(verb: str, message: str) -> SubcommandReceipt:
    """Return one failed receipt carrying a single plain sentence."""
    return _receipt(verb, False, [message])


# -- /plugin -----------------------------------------------------------------


def plugin_subcommand(
    verb: str, rest: str, *, repo_path: Any = None
) -> SubcommandReceipt:
    """Run one ``/plugin`` verb against the existing plugin implementation.

    Routes to ``cli.plugins`` - the same module ``vex plugin`` dispatches to.
    No behaviour is reimplemented here; the only new thing is that the verb is
    reachable from the session.
    """
    from cli import plugins as plugins_mod

    name = str(verb or "list").strip().lower()
    argument = str(rest or "").strip()
    if name == "list":
        rows = plugins_mod.list_plugins()
        if not rows:
            root = plugins_mod.plugins_root()
            return _receipt(
                "list",
                True,
                [f"no plugins installed under {root}"],
                payload={"count": 0},
            )
        lines = [f"{len(rows)} plugin(s) installed:"]
        for row in rows:
            word = "enabled" if row.get("enabled") else "disabled"
            description = str(row.get("description") or "").replace("\n", " ")
            entry = f"  {row.get('name', '?')} ({word})"
            if description:
                entry += f" - {description}"
            if row.get("error"):
                entry += f" [broken: {row.get('error')}]"
            lines.append(entry)
        return _receipt("list", True, lines, payload={"count": len(rows)})
    if name == "inspect":
        if not argument:
            return _fail(name, f"usage: /plugin {name} <name>")
        entry = plugins_mod.inspect_plugin(argument)
        if not entry:
            return _fail(name, f"no plugin named {argument}")
        lines = [f"{entry.get('name', argument)}"]
        for key in ("version", "description", "origin", "dir"):
            if entry.get(key):
                lines.append(f"{key}: {entry[key]}")
        for key, label in (
            ("skills_on_disk", "skills"),
            ("commands_on_disk", "commands"),
        ):
            values = entry.get(key) or []
            lines.append(f"{label}: {', '.join(str(v) for v in values) or 'none'}")
        verbs = sorted((entry.get("tools") or {}).get("verbs") or [])
        if verbs:
            lines.append(f"tools: {', '.join(verbs)}")
        servers = entry.get("mcp_servers") or {}
        if servers:
            lines.append(
                "mcp: " + ", ".join(f"{label}" for label in sorted(servers))
            )
        return _receipt("inspect", True, lines, payload=entry)
    if name == "install":
        if not argument:
            return _fail(name, f"usage: /plugin install <ref>")
        try:
            installed = plugins_mod.install(argument)
        except Exception as exc:
            return _fail(name, f"install failed: {exc}")
        return _receipt(
            "install",
            True,
            [f"installed plugin {installed}"],
            payload={"name": installed},
        )
    if name in {"enable", "disable", "remove"}:
        if not argument:
            return _fail(name, f"usage: /plugin {name} <name>")
        action = {"enable": plugins_mod.enable, "disable": plugins_mod.disable,
                  "remove": plugins_mod.remove}[name]
        past = {"enable": "enabled", "disable": "disabled", "remove": "removed"}[name]
        try:
            action(argument)
        except Exception as exc:
            return _fail(name, f"{name} failed: {exc}")
        return _receipt(name, True, [f"{past} plugin {argument}"],
                        payload={"name": argument})
    if name == "reload":
        # A reload is a rescan plus a report of what the rescan found. The
        # extension set is re-applied by the SAME function the session start
        # path uses, so a reload cannot produce a different registry than a
        # fresh start would.
        try:
            applied = plugins_mod.apply_tool_extensions()
        except Exception as exc:
            return _fail("reload", f"reload failed: {exc}")
        rows = plugins_mod.list_plugins()
        return _receipt(
            "reload",
            True,
            [
                f"rescanned {len(rows)} plugin(s)",
                f"batch verbs: {', '.join(applied) or 'none'}",
            ],
            payload={"count": len(rows), "verbs": list(applied)},
        )
    if name == "marketplace":
        # The marketplace REGISTRY is out of scope in this tree (recorded in
        # `cli/plugins.py`'s module docstring), so this verb reports that
        # honestly and points at the surface that does exist rather than
        # pretending to add a source.
        return _receipt(
            "marketplace",
            True,
            [
                "no marketplace registry is configured in this build",
                f"install directly instead: /plugin install <path-or-git-url>",
            ],
            payload={"sources": []},
        )
    return _fail(name, f"usage: /plugin {name}")


# -- /mcp --------------------------------------------------------------------


def mcp_subcommand(
    verb: str, rest: str, *, cfg: Optional[Mapping[str, Any]] = None,
    repo_path: Any = None,
) -> SubcommandReceipt:
    """Run one ``/mcp`` verb against the existing connector implementation.

    Routes to ``cli.connectors`` - the same module ``vex mcp`` dispatches to,
    and the same one the connector gate already runs. A verb here does not get
    a looser path than the argparse surface gets.
    """
    from cli import connectors

    name = str(verb or "list").strip().lower()
    argument = str(rest or "").strip()
    if name == "list":
        # `/mcp` and `/mcp <label>` have always been answered by
        # `_render_mcp`, which knows the connector precedence order, the
        # empty-state sentence and the tool listing. Delegating is what keeps
        # the historical lines BYTE-IDENTICAL, and it is the point of this
        # round: a new door onto an existing implementation, not a second
        # implementation.
        if argument and len(argument.split()) > 1:
            return _fail("list", "usage: /mcp list <label>")
        collected: List[str] = []
        _render_mcp(
            dict(cfg or {}),
            say=lambda text: collected.extend(_markup_to_plain(text)),
            label=argument,
            repo_path=repo_path,
        )
        # Deliberately NOT a second `mcp_server_table(...)` call: that is a
        # `discover_mcp_servers` walk plus a plugin scan, and
        # `test_cli_terminal_parity.py` counts the calls the shared backend
        # receives. Two calls would be two reads of the same registry, and the
        # count is the point of that test.
        return _receipt(
            "list", True, collected or ["no MCP servers configured"],
            payload={"label": argument, "count": len(collected)},
        )
    if name == "add":
        parts = argument.split(None, 1)
        if len(parts) < 2:
            return _fail("add", "usage: /mcp add <label> <command...>")
        try:
            connectors.add_server(parts[0], parts[1], repo_path=repo_path)
        except Exception as exc:
            return _fail("add", f"add failed: {exc}")
        return _receipt("add", True, [f"added connector {parts[0]}"],
                        payload={"label": parts[0]})
    if name in {"remove", "pin", "enable", "disable", "reconnect"}:
        if not argument:
            return _fail(name, f"usage: /mcp {name} <label>")
    if name == "remove":
        try:
            connectors.remove_server(argument, repo_path=repo_path)
        except Exception as exc:
            return _fail("remove", f"remove failed: {exc}")
        return _receipt("remove", True, [f"removed connector {argument}"],
                        payload={"label": argument})
    if name == "health":
        try:
            results = connectors.check_health(repo_path=repo_path)
        except Exception as exc:
            return _fail("health", f"health probe failed: {exc}")
        if not results:
            return _receipt("health", True, ["no MCP servers configured"],
                            payload={"ok": False, "count": 0})
        lines = []
        healthy = 0
        for row in results:
            good = bool(row.get("ok"))
            healthy += 1 if good else 0
            detail = row.get("error") or row.get("tools") or ""
            lines.append(
                f"  {row.get('label', '?')}: {'ok' if good else 'FAILED'} {detail}"
            )
        return _receipt(
            "health",
            healthy == len(results),
            [f"{healthy}/{len(results)} connector(s) healthy", *lines],
            payload={"ok": healthy == len(results), "count": len(results)},
        )
    if name == "call":
        parts = argument.split(None, 1)
        if len(parts) < 2:
            return _fail("call", "usage: /mcp call <label> <tool> [args-json]")
        tool_args: Optional[Dict[str, Any]] = None
        if len(parts) > 2:
            import json as _json

            try:
                tool_args = _json.loads(parts[2])
            except Exception as exc:
                return _fail("call", f"args must be JSON: {exc}")
        try:
            result = connectors.call_tool(
                parts[0], parts[1], args=tool_args, repo_path=repo_path
            )
        except Exception as exc:
            return _fail("call", f"call failed: {exc}")
        return _receipt(
            "call",
            bool(result.get("ok")),
            [str(result.get("text") or result.get("error") or "no result")],
            payload=result,
        )
    if name == "pin":
        parts = argument.split()
        if len(parts) < 3:
            return _fail("pin", "usage: /mcp pin <label> <tool> <digest>")
        return _fail(
            "pin",
            "tool pins are declared in the connector permission file; "
            "edit the [connector_permissions.<label>] pins entry",
        )
    if name == "reconnect":
        return _fail(
            "reconnect",
            "connectors are spawned per call, so there is no persistent "
            "connection to reconnect; /mcp health <label> probes one",
        )
    if name in {"enable", "disable"}:
        return _fail(
            name,
            f"a connector is enabled by being declared; use /mcp add or "
            f"/mcp remove rather than /mcp {name}",
        )
    return _fail(name, f"usage: /mcp {name}")


# -- /skills -----------------------------------------------------------------


def skills_subcommand(
    verb: str, rest: str, *, repo_path: Any = None, say: Any = None
) -> SubcommandReceipt:
    """Run one ``/skills`` verb against the existing skill implementation.

    ``list`` DELEGATES to :func:`_render_skills` - the exact function the
    historical bare ``/skills`` called - so the roster a person reads after
    this round is the same one, rendered the same way.
    """
    from cli import plugins as plugins_mod

    name = str(verb or "list").strip().lower()
    argument = str(rest or "").strip()
    if name == "list":
        # `_render_skills` is the HISTORICAL renderer and it emits MARKUP, so
        # its output cannot go straight into a receipt whose lines are plain
        # text and then be escaped a second time - the reader would see
        # `[vex.accent]` where a skill name belongs. The receipt therefore
        # RENDERS the markup once, through a real rich Console with colour
        # disabled, and keeps the resulting PLAIN text. That is the same
        # round-trip the TUI's transcript does, and it is what makes the
        # receipt's "plain text" claim true rather than aspirational.
        #
        # `strip_ansi` is NOT enough: it removes escape BYTES and leaves
        # `[vex.accent]` intact, so a line built that way would render its own
        # markup tags as literal text once the caller escapes it.
        collected: List[str] = []

        def _plain(text: str) -> None:
            collected.extend(_markup_to_plain(text))

        _render_skills(repo_path, say=_plain, filt=argument)
        return _receipt(
            "list", True, collected or ["no skills discovered"],
            payload={"filter": argument},
        )
    if not argument:
        return _fail(name, f"usage: /skills {name} <name>")
    if name == "inspect":
        for item in plugins_mod.list_skill_installs(repo_path):
            if str(item.get("name") or "") == argument:
                lines = [f"{item.get('name', argument)} ({item.get('origin', '?')})"]
                if item.get("description"):
                    lines.append(str(item["description"]))
                if item.get("source"):
                    lines.append(f"source: {item['source']}")
                return _receipt("inspect", True, lines, payload=item)
        return _fail("inspect", f"no skill named {argument}")
    if name == "enable":
        try:
            plugins_mod.enable_skill(argument, repo_path=repo_path)
        except Exception as exc:
            return _fail("enable", f"enable failed: {exc}")
        return _receipt("enable", True, [f"enabled skill {argument}"],
                        payload={"name": argument})
    if name == "disable":
        try:
            plugins_mod.disable_skill(argument, repo_path=repo_path)
        except Exception as exc:
            return _fail("disable", f"disable failed: {exc}")
        return _receipt("disable", True, [f"disabled skill {argument}"],
                        payload={"name": argument})
    if name == "create":
        root = Path(repo_path or ".") / ".vex" / "skills" / argument
        if root.exists():
            return _fail("create", f"{root} already exists")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", argument):
            return _fail(
                "create",
                "a skill name must be lowercase letters, digits, dot, dash "
                "or underscore and start with a letter or digit",
            )
        try:
            (root / "skills").mkdir(parents=True, exist_ok=True)
            (root / "SKILL.md").write_text(
                f"---\nname: {argument}\ndescription: what this skill is for\n"
                "---\n\nDescribe the task this skill covers.\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return _fail("create", f"create failed: {exc}")
        return _receipt("create", True, [f"created skill {root / 'SKILL.md'}"],
                        payload={"path": str(root / "SKILL.md")})
    return _fail(name, f"usage: /skills {name}")


# -- /worktree ---------------------------------------------------------------


def worktree_subcommand(
    verb: str, rest: str, *, repo_path: Any = None, log_root: Any = None
) -> SubcommandReceipt:
    """Run one ``/worktree`` verb against the existing worktree implementation.

    This is the DOOR item 7 asked for. ``worktree_list``, ``worktree_new``,
    ``worktree_remove`` and ``worktree_path`` were written, tested and
    reachable only from argparse; this routes to those same functions, so the
    REPL gains the verbs without a second implementation of any of them.
    """
    from cli import commands as commands_mod

    name = str(verb or "list").strip().lower()
    argument = str(rest or "").strip()
    repo = str(repo_path or ".")
    if name == "list":
        try:
            records = commands_mod.worktree_list(repo, log_root=log_root)
        except commands_mod.WorktreeCommandError as exc:
            return _fail("list", str(exc))
        if not records:
            return _receipt("list", True, ["no managed worktrees"],
                            payload={"count": 0})
        lines = [f"{len(records)} worktree(s):"]
        for record in records:
            lines.append(f"  {record.get('node_id', '?')} {record.get('path', '')}")
        return _receipt("list", True, lines, payload={"count": len(records)})
    if not argument:
        return _fail(name, f"usage: /worktree {name} <name>")
    if name == "create":
        base = ""
        pieces = argument.split()
        if "--base" in pieces:
            index = pieces.index("--base")
            if index + 1 < len(pieces):
                base = pieces[index + 1]
            pieces = [p for i, p in enumerate(pieces) if i != index]
        target = pieces[0] if pieces else ""
        try:
            record = commands_mod.worktree_new(
                repo, target, base=base, log_root=log_root
            )
        except commands_mod.WorktreeCommandError as exc:
            return _fail("create", str(exc))
        return _receipt(
            "create",
            True,
            [f"created {record.get('node_id', target)} at {record.get('path', '')}"],
            payload=record,
        )
    if name == "remove":
        forced = "--force" in argument
        target = argument.split()[0]
        try:
            record = commands_mod.worktree_remove(
                repo, target, force=forced, log_root=log_root
            )
        except commands_mod.WorktreeCommandError as exc:
            return _fail("remove", str(exc))
        return _receipt("remove", True, [f"removed {record.get('path', target)}"],
                        payload=record)
    if name == "checkout":
        try:
            path = commands_mod.worktree_path(repo, argument, log_root=log_root)
        except commands_mod.WorktreeCommandError as exc:
            return _fail("checkout", str(exc))
        return _receipt("checkout", True, [path], payload={"path": path})
    return _fail(name, f"usage: /worktree {name}")


#: Which handler answers which command. The dispatcher's single table, so a
#: new verb-bearing command is a row here rather than a branch in three
#: surfaces. `/hooks`, `/migrate` and `/support-bundle` are deliberately
#: absent: their behaviour belongs to Terminals 09 and 10, and the refusal
#: that says so is in :func:`unimplemented_subcommand` rather than a missing
#: dispatch that would look like a crash.
SUBCOMMANDS_BY_COMMAND: Dict[str, Any] = {
    "/plugins": plugin_subcommand,
    "/mcp": mcp_subcommand,
    "/skills": skills_subcommand,
    "/worktree": worktree_subcommand,
}

#: Commands whose registry row is DECLARED and whose behaviour is owned by
#: another terminal. Read from `cli.commands` rather than restated here, so
#: the row, the palette's disabled reason and the interactive refusal are
#: the same fact. `/hooks`, `/migrate` and `/support-bundle` are the three
#: item 7 of the brief asks for.
HANDED_OFF_COMMANDS: Mapping[str, str] = dict(_commands.HANDED_OFF_COMMANDS)


def unimplemented_subcommand(command: str, verb: str) -> SubcommandReceipt:
    """Return the honest refusal for a command whose handler is handed off."""
    owner = HANDED_OFF_COMMANDS.get(command, "another terminal")
    return _fail(
        verb,
        f"{command} is declared in the command registry but its handler is "
        f"owned by {owner}; run the CLI form instead",
    )


def run_subcommand(
    spec: Any,
    args: str,
    *,
    state: Optional[Mapping[str, Any]] = None,
    log_root: Any = None,
    say: Any = None,
) -> Optional[SubcommandReceipt]:
    """Dispatch one resolved verb to its handler, or ``None`` if not ours.

    ``None`` means "this command is not dispatched here", which is how the
    REPL falls through to the next branch. It is NOT an error path: a command
    with no verb registry, or one whose handler another terminal owns, is
    answered by its own branch.
    """
    from cli import commands as commands_mod

    name = getattr(spec, "name", "") or ""
    if name in HANDED_OFF_COMMANDS:
        return unimplemented_subcommand(name, default_subcommand_name(name))
    handler = SUBCOMMANDS_BY_COMMAND.get(name)
    if handler is None:
        return None
    resolution = commands_mod.resolve_subcommand(spec, args)
    if resolution is None or resolution.spec is None:
        return None
    session = dict(state or {})
    repo = session.get("repo")
    try:
        if name == "/plugins":
            return handler(resolution.verb, resolution.rest, repo_path=repo)
        if name == "/mcp":
            cfg = None
            try:
                from cli.vexconfig import merged_settings as _merged

                cfg = _merged()
            except Exception:
                cfg = None
            return handler(
                resolution.verb,
                resolution.rest,
                cfg=cfg,
                repo_path=repo,
            )
        if name == "/skills":
            return handler(
                resolution.verb, resolution.rest, repo_path=repo, say=say
            )
        return handler(
            resolution.verb, resolution.rest, repo_path=repo, log_root=log_root
        )
    except Exception as exc:  # a handler must never take a session down
        return _fail(resolution.verb, f"{name} {resolution.verb} failed: {exc}")


def default_subcommand_name(command: str) -> str:
    """Return the no-argument verb's name for a command, or ``""``."""
    from cli import commands as commands_mod

    verb = commands_mod.default_subcommand(command)
    return verb.name if verb is not None else ""


def mcp_server_table(
    cfg: Optional[Dict[str, Any]] = None,
    repo_path: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """Return the canonical connector registry for a repository.

    Discovery always starts with ``cli.connectors.discover_mcp_servers`` so
    project/local precedence and masking rules cannot drift between shells.
    Disabled plugin entries are appended as diagnostic-only rows."""
    from cli import connectors

    rows: List[Dict[str, str]] = []
    try:
        discovered = connectors.discover_mcp_servers(
            str(repo_path) if repo_path else None
        )
    except Exception:
        discovered = {}
    for label, info in sorted(discovered.items()):
        rows.append(
            {
                "label": str(label),
                "command": str(info.get("command") or ""),
                "source": str(info.get("source") or "unknown"),
            }
        )
    try:
        from cli import plugins as plugins_mod

        for entry in plugins_mod.list_plugins():
            if entry.get("error") or entry.get("enabled") is not False:
                continue
            for label, command in (entry.get("mcp_servers") or {}).items():
                rows.append(
                    {
                        "label": str(label),
                        "command": str(command),
                        "source": f"disabled-plugin:{entry.get('name', '?')}",
                    }
                )
    except Exception:
        pass
    if not rows:
        legacy = (cfg or {}).get("agent_mcp_servers") or {}
        if isinstance(legacy, dict):
            rows.extend(
                {
                    "label": str(label),
                    "command": str(command),
                    "source": "config",
                }
                for label, command in legacy.items()
            )
    return rows


def _render_mcp(
    cfg: Optional[Dict[str, Any]] = None,
    say: Optional[Any] = None,
    label: str = "",
    repo_path: Optional[Path] = None,
) -> None:
    """List unified connectors or one connector's tools without leaking secrets."""
    say = say or _slash_say_default
    try:
        from cli import connectors

        servers = mcp_server_table(cfg, repo_path=repo_path)
        lab = str(label or "").strip()
        if not lab:
            if not servers:
                say_empty_state(say, "no_connectors")
                return
            say("[vex.accent]mcp connectors[/] [vex.muted](/mcp <label> lists tools)[/]")
            for server in servers:
                command = connectors.mask_command(server.get("command", ""))
                say(
                    f"  [vex.accent]{escape(server['label'])}[/] "
                    f"[vex.muted]({escape(server['source'])})[/] "
                    f"[vex.muted]{escape(command[:100])}[/]"
                )
            return
        discovered = connectors.discover_mcp_servers(
            str(repo_path) if repo_path else None
        )
        command = discovered.get(lab, {}).get("command")
        if not command:
            say(f"[vex.error]unknown MCP server: {escape(lab)}[/]")
            if servers:
                say(
                    "[vex.muted]configured: "
                    + ", ".join(escape(str(s["label"])) for s in servers)
                    + "[/]"
                )
            return
        from memory.mcp_client import list_mcp_tools

        try:
            out = list_mcp_tools(command)
        except Exception as exc:
            out = {"ok": False, "error": ui.strip_ansi(f"{type(exc).__name__}: {exc}")}
        tools = (out or {}).get("tools") or []
        if not (out or {}).get("ok"):
            say(
                f"[vex.error]{escape(lab)} unavailable[/] "
                f"[vex.muted]{escape(ui.strip_ansi(str((out or {}).get('error', '?'))))}[/]"
            )
            return
        if not tools:
            say(f"[vex.muted]{escape(lab)}: no tools exposed[/]")
            return
        say(f"[vex.accent]{escape(lab)}[/] [vex.muted]({len(tools)} tools)[/]")
        for tool in tools[:30]:
            try:
                name = str(tool.get("name", "?"))
                desc = str(tool.get("description") or "")[:100]
            except Exception:
                continue
            say(
                f"  [vex.accent]{escape(name)}[/]"
                + (f" [vex.muted]{escape(desc)}[/]" if desc else "")
            )
    except Exception as exc:
        say(f"[vex.muted](mcp unavailable: {type(exc).__name__})[/]")


def _do_init(repo: Any, say: Optional[Any] = None) -> None:
    """`/init`: scaffold .vex/ in the session repo (settings + examples).
    Calls the vexconfig writers (never reimplements them). Assumes repo
    is the session repo. Never raises."""
    say = say or _slash_say_default
    try:
        from cli import vexconfig

        root = Path(str(repo)) if repo else Path.cwd()
        try:
            info = vexconfig.ensure_project_layout(root)
        except OSError as exc:
            say(f"[vex.error]could not scaffold .vex/: {exc}[/]")
            return
        try:
            vexconfig.ensure_gitignore(root)
        except Exception:
            pass
        created = (info or {}).get("created") or []
        if created:
            say(
                "[vex.ok]repo setup:[/] [vex.muted]created "
                + ", ".join(f".vex/{c}" for c in created)
                + "[/]"
            )
        else:
            say("[vex.muted].vex/ already set up â€” nothing to create[/]")
    except Exception as exc:
        say(f"[vex.muted](init unavailable: {type(exc).__name__})[/]")


def _reload_file_config(
    state: Optional[Dict[str, Any]],
    file_config: Optional[Dict[str, Any]] = None,
    start: Optional[Any] = None,
) -> Dict[str, Any]:
    try:
        from cli.vexconfig import merged_settings

        refreshed = dict(merged_settings(start) or {})

    except Exception:
        refreshed = {}
    targets: List[Dict[str, Any]] = []
    if isinstance(file_config, dict):
        targets.append(file_config)
    state_config = (state or {}).get("file_config")
    if isinstance(state_config, dict):
        targets.append(state_config)
    for target in targets:
        try:
            target.clear()
            target.update(refreshed)
        except Exception:
            pass
    if state is not None and not isinstance(state_config, dict):
        state["file_config"] = dict(refreshed)
    return refreshed


def _do_logout(
    say: Optional[Any] = None,
    state: Optional[Dict[str, Any]] = None,
    file_config: Optional[Dict[str, Any]] = None,
    repo: Optional[Any] = None,
) -> bool:
    """`/logout`: strip the stored api_key and report the real outcome."""
    say = say or _slash_say_default
    success = False
    try:
        from cli.onboard import cmd_logout

        if repo is None:
            code = cmd_logout(None)
        else:
            from types import SimpleNamespace

            code = cmd_logout(SimpleNamespace(start=repo))
        success = int(code) == 0
        if not success:
            say(
                "[vex.warn]logout incomplete â€” persisted key may remain; "
                "check the lines above[/]"
            )
    except Exception as exc:
        say(f"[vex.muted](logout failed: {type(exc).__name__})[/]")
    finally:
        _reload_file_config(state, file_config, start=repo)
    return success


def _render_review_surface(
    con: Any,
    state: Dict[str, Any],
    log_root: Path,
    argument: str,
    *,
    in_flight: bool = False,
) -> bool:
    """Ask `cli.review` to answer this `/diff` line, and print its receipt.

    Returns True when review CLAIMED the line, so the caller knows to stop;
    False means "not mine" and the caller continues into the historical
    engine. Every failure mode DEGRADES to False rather than raising: a
    review backend that cannot be reached must not take a working `/diff`
    down with it, and the honest answer to "I could not read the review" is
    the historical diff.

    The lines come from `cli.review`, which routes every one of them through
    `cli.ui.sanitize_text` at its own `_bound` boundary. They are escaped
    again on the way out because a rich `Console` IS a markup parser and a
    repository path may contain `[` - a line that reached it raw would have
    its message eaten rather than printed.
    """
    task_id = active_task_id(Path(log_root), state.get("last") or {})
    if not task_id:
        return False
    task_dir = _safe_task_dir(task_id, log_root)
    if task_dir is None:
        return False
    try:
        from cli import review as _review
    except Exception:
        return False

    text = str(argument or "").strip()
    known = tuple(_review.diff_review_verbs())
    verb = ""
    rest = text
    if text:
        first, _, tail = text.partition(" ")
        if first.lower() in known:
            verb, rest = first.lower(), tail.strip()
    if not verb:
        # No verb: the SHOW surface, printed rather than modaled because a
        # rich Console has no screen stack. The per-file roster is collapsed
        # by default, so this is a page and not a wall.
        try:
            document = _review.build_review(
                task_dir,
                state.get("repo") or Path.cwd(),
                config=dict(state.get("config") or {}),
                width=int(_con_width(con) or 80),
            )
        except Exception:
            return False
        expanded = [document.path()] if rest else []
        rendered = _review.render_review(
            document, width=document.width, expanded=expanded
        )
        lines = _review.review_lines(rendered, expanded=expanded)
        if not lines:
            say_empty_state(
                con.print,
                "no_diff",
            )
            return True
        for line in lines:
            con.print(escape(ui.sanitize_text(str(line))))
        return True

    result = _review.review_command(
        task_dir,
        state.get("repo") or Path.cwd(),
        rest,
        verb=verb,
        in_flight=bool(in_flight),
        config=dict(state.get("config") or {}),
        width=int(_con_width(con) or 80),
    )
    if not result.get("handled"):
        return False
    for line in list(result.get("lines") or [])[:80]:
        con.print(escape(ui.sanitize_text(str(line))))
    _set_handler_result(state, "ok" if result.get("ok") else "failed", 0 if result.get("ok") else 1)
    return True


def _con_width(con: Any) -> int:
    """The console's own width, or 80. Never raises.

    Read from the console rather than passed in so a width the user actually
    has is the width the receipt is bounded to - a receipt clipped to 80 on
    a 200-column terminal is a receipt that hides its own hunk headers.
    """
    try:
        return int(getattr(con, "width", 0) or 0)
    except Exception:
        return 0


def _print_session_pulse(
    con: Any, state: Dict[str, Any], log_root: Path
) -> bool:
    """Print the session pulse under `/status`. Returns whether it printed.

    **Prints only what it can establish.** `context.fraction` is `None`
    when the context window is not resolvable and the percentage line is
    OMITTED rather than rendered as `0%` - "0% of the window used" is a
    measurement nobody took, and this repo's core rule is that `0` must
    never mean "we do not know". `cost.usd` is `None` when the run was
    unpriced, and the line is omitted for the same reason: `$0.000000`
    reads as a measured fact that the work was free.

    A pulse that cannot be read prints NOTHING. An empty section is the
    honest absent state; a card shown because we could not look is the
    clutter the anti-clutter rule exists to remove.
    """
    conversation = (state or {}).get("conversation")
    if not isinstance(conversation, dict) or not conversation:
        return False
    try:
        from cli import session as _session

        pulse = _session.session_pulse(
            conversation,
            config=dict((state or {}).get("file_config") or {}),
            log_root=log_root,
            task_id=active_task_id(Path(log_root), (state or {}).get("last") or {}) or None,
        )
    except Exception:
        return False
    if not isinstance(pulse, dict) or not pulse.get("readable"):
        return False
    rows: List[str] = []
    conversation_facts = pulse.get("conversation") or {}
    turns = int(conversation_facts.get("turns_active") or 0)
    if turns:
        rows.append(
            f"session  {turns} turn(s) active, "
            f"{int(conversation_facts.get('compactions') or 0)} compaction(s)"
        )
    fraction = (pulse.get("context") or {}).get("fraction")
    if fraction is not None:
        rows.append(f"context  {round(float(fraction) * 100)}% of window used")
    cost = pulse.get("cost") or {}
    if cost.get("priced") and cost.get("usd") is not None:
        rows.append(f"spend    {ui.fmt_cost(float(cost['usd']))}")
    pressure = str(pulse.get("pressure") or "").strip()
    if pressure in ("pressuring", "exhausted"):
        rows.append(f"context  {pressure}")
    if not rows:
        return False
    for row in rows:
        con.print(f"[vex.muted]{escape(row)}[/]")
    return True


def _do_clear(
    log_root: Path, repo: Any, state: Dict[str, Any], say: Optional[Any] = None
) -> Optional[str]:
    """`/clear`: start a fresh conversation; the old file is kept on
    disk under _conversations/ (never deleted). Assumes state carries
    the session's conversation dict. Returns the new session id (or
    None). Never raises. NOTE: callers that cached the old conversation
    object must re-read state["conversation"] after this returns (the
    REPL loop does)."""
    say = say or _slash_say_default
    try:
        from cli.session import load_or_create, save_session

        old = str((state.get("conversation") or {}).get("session_id") or "?")
        conv = load_or_create(log_root, repo)
        save_session(log_root, conv)
        state["conversation"] = conv
        new = str(conv.get("session_id") or "?")
        say(
            f"[vex.ok]cleared[/] [vex.muted]â€” fresh conversation {new} "
            f"(previous {old} kept)[/]"
        )
        return new
    except Exception as exc:
        say(f"[vex.muted](clear failed: {type(exc).__name__})[/]")
        return None


def _native_undo(
    task_id: str, task_dir: Path, repo: Path, arg: str
) -> Optional[Dict[str, Any]]:
    """Restore strict-kernel edits from its pristine snapshot."""
    pristine = task_dir / "pristine"
    if not pristine.is_dir():
        return None
    try:
        from harness.editor import changed_files

        candidates = [str(path) for path in changed_files(str(pristine), str(repo))]
    except Exception:
        return None
    requested = str(arg or "").strip().strip("`\"'").replace("\\", "/")
    if requested and requested.lower() != "all":
        normalized = requested.strip("/")
        candidates = [
            path
            for path in candidates
            if path == normalized or path.endswith("/" + normalized)
        ]
    if not candidates:
        return {"outcome": "nothing", "files": [], "diff": None}
    restored: List[str] = []
    deleted: List[str] = []
    for relative in candidates:
        if ".." in Path(relative).parts or Path(relative).is_absolute():
            continue
        source = pristine / relative
        target = repo / relative
        try:
            if source.is_file() and not source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                restored.append(relative)
            elif target.exists() or target.is_symlink():
                target.unlink()
                deleted.append(relative)
        except OSError:
            continue
    if not restored and not deleted:
        return {"outcome": "nothing", "files": [], "diff": None}
    try:
        from harness.agent_loop import agent_diff

        diff = agent_diff(task_id, task_dir.parent, str(repo))
    except Exception:
        diff = None
    try:
        trace = task_dir / "trace.jsonl"
        with trace.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "undo",
                        "ts": time.time(),
                        "data": {"restored": restored, "deleted": deleted},
                    }
                )
                + "\n"
            )
    except OSError:
        pass
    return {
        "outcome": "done",
        "files": restored + deleted,
        "restored": restored,
        "deleted": deleted,
        "diff": diff,
    }


def undo_result(
    last: Dict[str, Any], log_root: Path, repo: Any, arg: str = ""
) -> Dict[str, Any]:
    """Shared `/diff undo` + `/undo` core. Assumes arg is the text after
    "undo" ("" = last edit, "all" = everything, else one file). Returns
    {"outcome": "not_agent"|"nothing"|"done"|"error", "files": [...],
    "diff": str|None, "error": str}. The CALLER renders in its own idiom
    (REPL: ui.print_diff; TUI: diff_render_lines). Never raises."""
    try:
        tid = (last or {}).get("task_id")
        if not tid or not str(tid).startswith("agent-"):
            return {"outcome": "not_agent", "files": [], "diff": None}
        from harness.agent_loop import agent_diff, undo_edits

        a = (arg or "").strip()
        if a.lower() in ("", "all"):
            scope: Any = "all" if a.lower() == "all" else 1
            targets = None
        else:
            targets = [a.strip("`\"'")]
            scope = 1
        task_dir = _safe_task_dir(str(tid), Path(log_root))
        repo_path = Path(repo or Path.cwd())
        redo_paths = _redo_candidates(task_dir, repo_path, a) if task_dir is not None else []
        has_undo_material = bool(
            task_dir is not None
            and task_dir.is_dir()
            and (
                (task_dir / "orig").is_dir()
                or (task_dir / "pristine").is_dir()
                or (task_dir / "redo.json").is_file()
            )
        )
        if has_undo_material and (task_dir / "orig").is_dir():
            try:
                from cli.fileview import undo_preflight

                preflight = undo_preflight(task_dir, repo_path, redo_paths)
            except Exception as exc:
                preflight = {"ok": False, "status": "conflict", "error": f"{type(exc).__name__}: {exc}"}
            if not preflight.get("ok"):
                return {
                    "outcome": "conflict",
                    "files": [],
                    "diff": None,
                    "error": preflight.get("error")
                    or (preflight.get("conflicts") or [{"reason": "workspace changed"}])[0].get("reason", "workspace changed"),
                }
        if has_undo_material and not (task_dir / "orig").is_dir():
            try:
                from cli.fileview import undo_preflight

                preflight = undo_preflight(
                    task_dir,
                    repo_path,
                    redo_paths,
                    post_root=task_dir / "pristine",
                )
            except Exception as exc:
                preflight = {"ok": False, "status": "conflict", "error": f"{type(exc).__name__}: {exc}"}
            if not preflight.get("ok"):
                return {
                    "outcome": "conflict",
                    "files": [],
                    "diff": None,
                    "error": preflight.get("error")
                    or (preflight.get("conflicts") or [{"reason": "workspace changed"}])[0].get("reason", "workspace changed"),
                }
            _capture_redo(task_dir, repo_path, redo_paths, task_dir / "pristine")
            native = _native_undo(str(tid), task_dir, repo_path, a)
            if native is not None:
                return native
        if has_undo_material:
            _capture_redo(task_dir, repo_path, redo_paths)
        res = undo_edits(
            str(tid),
            Path(log_root),
            str(repo or Path.cwd()),
            steps=scope,
            targets=targets,
        )
        files = list(res.get("restored", [])) + list(res.get("deleted", []))
        if not files:
            if task_dir is not None:
                try:
                    (task_dir / "redo.json").unlink()
                except OSError:
                    pass
            return {"outcome": "nothing", "files": [], "diff": None}
        try:
            diff = agent_diff(str(tid), Path(log_root), str(repo or Path.cwd()))
        except Exception:
            diff = None
        return {"outcome": "done", "files": files, "diff": diff}
    except Exception as exc:
        return {
            "outcome": "error",
            "files": [],
            "diff": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _redo_candidates(task_dir: Path, repo: Path, arg: str) -> List[str]:
    """Return safe repository-relative paths represented by an undo record."""
    orig = task_dir / "orig"
    try:
        all_paths = [
            path.relative_to(orig).as_posix()
            for path in orig.rglob("*")
            if path.is_file() and not path.name.endswith(".absent")
        ]
    except OSError:
        all_paths = []
    if not all_paths:
        try:
            from harness.editor import changed_files

            all_paths = changed_files(str(task_dir / "pristine"), str(repo))
        except Exception:
            all_paths = []
    requested = str(arg or "").strip().strip("`\"'").replace("\\", "/")
    if not requested or requested.lower() == "all":
        return sorted(set(all_paths))
    normalized = requested.strip("/")
    matches = [
        path
        for path in all_paths
        if path == normalized or path.endswith("/" + normalized)
    ]
    return sorted(set(matches))


def _capture_redo(
    task_dir: Path, repo: Path, paths: List[str], post_root: Optional[Path] = None
) -> None:
    """Persist pre-undo bytes and hashes through the shared safe receipt writer."""
    try:
        from cli.fileview import capture_undo_receipt

        capture_undo_receipt(task_dir, repo, paths, post_root=post_root)
    except Exception:
        return


def redo_result(
    last: Dict[str, Any], log_root: Path, repo: Any, arg: str = ""
) -> Dict[str, Any]:
    """Redo the most recent undo for an agent session, with containment checks."""
    try:
        task_id = str((last or {}).get("task_id") or "")
        task_dir = _safe_task_dir(task_id, log_root)
        if task_dir is None or not task_id.startswith("agent-"):
            return {"outcome": "not_agent", "files": [], "diff": None}
        try:
            from cli.fileview import redo_apply

            safe_result = redo_apply(task_dir, repo or Path.cwd())
            if safe_result.get("outcome") != "nothing":
                return safe_result
        except Exception:
            pass
        receipt = task_dir / "redo.json"
        if not receipt.is_file():
            return {"outcome": "nothing", "files": [], "diff": None}
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        records = payload.get("files") if isinstance(payload, Mapping) else None
        if not isinstance(records, list):
            return {"outcome": "nothing", "files": [], "diff": None}
        root = Path(repo or Path.cwd()).resolve()
        changed: List[str] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            relative = str(record.get("path") or "").replace("\\", "/")
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if record.get("existed"):
                try:
                    content = __import__("base64").b64decode(str(record.get("content") or ""))
                except (ValueError, TypeError):
                    continue
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_bytes(content)
            else:
                try:
                    if candidate.is_file() or candidate.is_symlink():
                        candidate.unlink()
                except OSError:
                    continue
            changed.append(relative)
        if not changed:
            return {"outcome": "nothing", "files": [], "diff": None}
        from harness.agent_loop import agent_diff

        diff = agent_diff(task_id, Path(log_root), str(root))
        try:
            receipt.unlink()
        except OSError:
            pass
        return {"outcome": "done", "files": changed, "diff": diff}
    except Exception as exc:
        return {"outcome": "error", "files": [], "diff": None, "error": f"{type(exc).__name__}: {exc}"}


def _handle_staged_undo(
    state: Dict[str, Any],
    log_root: Path,
    rest: str,
    *,
    say: Optional[Any] = None,
) -> bool:
    """Run the SHARED staged-undo dispatcher and render it in the REPL idiom.

    Returns True when it owned the line; False hands ``/undo <file>`` to the
    historical per-file revert. The decision logic lives in
    ``cli.fileview.undo_command`` so the REPL and the TUI cannot disagree
    about what ``/undo`` means - one core, two idioms.
    """
    say = say or _slash_say_default
    try:
        from cli import fileview as _fv

        result = _fv.undo_command(
            state.get("repo") or Path.cwd(),
            log_root,
            rest,
            session_id=str(state.get("session_id") or ""),
            in_flight=_live_run() is not None,
        )
    except Exception as exc:
        say(f"[vex.error]undo failed:[/] [vex.muted]{escape(f'{type(exc).__name__}: {exc}')}[/]")
        return True
    if not result.get("handled"):
        return False
    kind = str(result.get("kind") or "")
    if result.get("ok") and kind in {"stage", "widen", "scope"}:
        head = "[vex.ok]staged revert[/]"
    elif result.get("ok"):
        head = "[vex.ok]undo[/]"
    else:
        head = "[vex.warn]undo[/]"
    try:
        lines = [str(item) for item in (result.get("lines") or [])]
    except Exception:
        lines = []
    for index, line in enumerate(lines):
        text = str(line or "")
        if not text:
            continue
        prefix = head if index == 0 else "[vex.muted]"
        say(f"{prefix} [vex.muted]{escape(text)}[/]")
    if not result.get("ok"):
        _set_handler_result(state, "failed", 1)
    return True


def _commit_staged_undo_for_prompt(
    state: Dict[str, Any], log_root: Path, *, say: Optional[Any] = None
) -> bool:
    """Commit a staged CODE-ONLY revert before a new prompt runs.

    The second half of the staged model: ``/undo`` stages, and the next prompt
    is what commits it. Only the ``files`` scope auto-commits - a
    ``conversation`` or ``both`` range rewrites history and has to be typed.
    """
    say = say or _slash_say_default
    if _live_run() is not None:
        return False
    try:
        from cli import fileview as _fv

        result = _fv.commit_staged_undo_for_prompt(
            state.get("repo") or Path.cwd(),
            log_root,
            session_id=str(state.get("session_id") or ""),
            in_flight=False,
        )
    except Exception:
        return False
    if not result.get("committed"):
        return False
    for line in result.get("lines") or []:
        say(f"[vex.muted]{escape(str(line))}[/]")
    if not bool((result.get("payload") or {}).get("ok")):
        say("[vex.muted]the staged revert is still pending[/]")
    return True


def _render_undo_result(res: Dict[str, Any], last: Dict[str, Any]) -> None:
    """Render an undo_result dict in the REPL idiom (ui.print_diff for
    the refreshed diff). Assumes last is the session's last-run dict
    (updated in place on done). Never raises."""
    con = ui.console()
    try:
        outcome = (res or {}).get("outcome")
        if outcome == "not_agent":
            con.print(
                "[vex.muted]undo is for agent sessions (this run never "
                "touched the live repo)[/]"
            )
            return
        if outcome == "nothing":
            con.print("[vex.muted]nothing to undo[/]")
            return
        if outcome == "conflict":
            con.print(
                f"[vex.warn]undo conflict[/] [vex.muted]{escape(str((res or {}).get('error') or 'workspace changed after the recorded operation'))}[/]"
            )
            return
        if outcome == "done":
            files = (res or {}).get("files") or []
            con.print(
                f"[vex.ok]undone {len(files)} file(s)[/] "
                f"[vex.muted]{', '.join(files[:5])}[/]"
            )
            diff = (res or {}).get("diff")
            if diff:
                last["diff"] = diff
                ui.print_diff(diff)
            else:
                last["diff"] = None
            return
        con.print(
            f"[vex.error]undo failed:[/] [vex.muted]{(res or {}).get('error', '?')}[/]"
        )
    except Exception:
        pass


def history_matches(line_text: str, query: str) -> bool:
    """True when a raw input-history line satisfies `query` under the
    /sessions filter grammar (session_matches): the line is scored as a
    session record's issue text, so bare tokens AND-match and
    key:value tokens narrow honestly (a status: filter matches no
    history line â€” history carries no status â€” rather than matching
    all). Never raises."""
    try:
        return session_matches(
            {"task_id": "", "issue": line_text, "repo": "", "status": ""},
            query,
        )
    except Exception:
        try:
            q = (query or "").lower()
            return not q or q in (line_text or "").lower()
        except Exception:
            return True


def _mode_command(
    state: Dict[str, Any], value: str, say: Optional[Any] = None
) -> Optional[str]:
    """Set or display the active product mode and return its canonical name."""
    from cli.commands import MODE_NAMES, mode_spec

    say = say or _slash_say_default
    selected = str(value or "").strip()
    if not selected:
        current = str(state.get("mode") or "auto")
        say(
            f"[vex.accent]mode[/] [vex.accent2]{escape(current)}[/] "
            f"[vex.muted]choose: {', '.join(MODE_NAMES)}[/]"
        )
        return current
    resolved = mode_spec(selected)
    if resolved is None:
        say(f"[vex.error]unknown mode:[/] {escape(selected)} [vex.muted](choose: {', '.join(MODE_NAMES)})[/]")
        return None
    state["mode"] = resolved.name
    state["agent_mode"] = resolved.name
    state["mode_config"] = resolved.to_dict()
    say(
        f"[vex.ok]mode {resolved.label}[/] [vex.muted]â€” {resolved.summary}[/]"
    )
    return resolved.name


def _mode_config_lines(state: Dict[str, Any]) -> List[str]:
    """Return a bounded, user-facing description of the active mode profile."""
    from cli.commands import mode_spec

    selected = str(state.get("mode") or "auto")
    profile = mode_spec(selected)
    if profile is None:
        return ["mode: auto (choose with /mode)"]
    return [
        f"mode: {profile.name}",
        f"strategy: {profile.strategy}",
        f"tools: {', '.join(profile.visible_tools)}",
        f"approval: {profile.approval}",
        f"read-only: {'yes' if profile.read_only else 'no'}",
    ]


def repository_files(repo: Any, query: str = "", limit: int = 4000) -> List[str]:
    """Return repository-relative files for the file browser and @-mentions."""
    try:
        from cli.tui import scan_repo_files

        files = scan_repo_files(repo, cap=max(1, int(limit)))
    except Exception:
        files = []
    needle = str(query or "").strip().lower()
    if needle:
        files = [path for path in files if needle in path.lower()]
    return files[: max(1, int(limit))]


def file_projection(
    log_root: Path,
    repo: Any,
    task_id: Optional[str] = None,
    *,
    snapshot: Optional[Mapping[str, Any]] = None,
    include_git: bool = True,
) -> Dict[str, Any]:
    """Return the shared journal/workspace file projection for a task."""
    try:
        from cli import fileview

        task_dir = _safe_task_dir(str(task_id), Path(log_root)) if task_id else None
        return fileview.build_file_projection(
            task_dir,
            repo,
            snapshot=snapshot,
            include_git=include_git,
            max_files=120,
            max_diff_lines=80,
        )
    except Exception:
        return {}


def context_snapshot(
    repo: Any,
    issue_text: str = "",
    *,
    task_id: Optional[str] = None,
    changed_files: Optional[Sequence[Any]] = None,
    selected_files: Optional[Sequence[Any]] = None,
    token_budget: int = 4000,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return cited context metadata for the REPL and TUI source panel."""
    try:
        from cli import fileview

        return fileview.context_snapshot(
            repo,
            issue_text,
            task_id=task_id,
            changed_files=changed_files,
            selected_files=selected_files,
            token_budget=token_budget,
            config=config,
        )
    except Exception as exc:
        return {
            "repo": str(repo or ""),
            "issue": issue_text,
            "sections": [],
            "sources": [],
            "skills": [],
            "memory": [],
            "citations": [],
            "warnings": [f"{type(exc).__name__}: {exc}"],
        }


def context_lines(repo: Any, issue_text: str = "", config: Optional[Mapping[str, Any]] = None) -> List[str]:
    """Render a bounded, source-aware context summary for the plain REPL."""
    data = context_snapshot(repo, issue_text, config=config, token_budget=2500)
    rows = [
        f"[vex.accent]context[/] [vex.muted]{escape(str(data.get('repo') or 'unavailable'))}[/]",
        f"[vex.muted]estimated[/] {int(data.get('estimated_tokens') or 0)} tokens",
    ]
    for source in data.get("sources", [])[:12]:
        if isinstance(source, Mapping):
            label = str(source.get("source") or "source")
            path = str(source.get("path") or "")
            line = source.get("line")
            detail = f"{label}:{path}" if path else label
            if line:
                detail += f":{line}"
        else:
            detail = str(source)
        rows.append(f"  [vex.accent2]{escape(detail[:120])}[/]")
    for warning in data.get("warnings", [])[:3]:
        rows.append(f"  [vex.warn]{escape(str(warning)[:140])}[/]")
    return rows


def _checkpoint_lines(
    log_root: Path, task_id: Optional[str] = None, repo: Any = None
) -> List[Dict[str, Any]]:
    """Read journal and durable checkpoint receipts for one run or session."""
    try:
        from cli import fileview

        return fileview.checkpoint_records(
            repo if repo is not None else _detect_repo(),
            log_root,
            task_id=task_id,
        )
    except Exception:
        from cli.runview import read_checkpoints

        target = str(task_id or "")
        task_dir = _safe_task_dir(target, Path(log_root)) if target else None
        return read_checkpoints(task_dir) if task_dir is not None else []


def file_change_attribution(record: Mapping[str, Any]) -> str:
    """Render the five file-change attributions as one worded fragment.

    The product's contract is that a file change identifies who changed it,
    why, whether it is verified, whether it can be undone, and which
    checkpoint contains it. The REPL's `/diff <file>` printed the path and a
    summary and stopped, so three of the five answers were simply absent from
    the surface a person reads first.

    ``verified`` is read from the RECORD's fail-closed flag, never
    re-derived from a status word here. A renderer that recomputes a verdict
    is a renderer that can disagree with the projection — which is the defect
    class this file already had once.
    """
    checkpoints = record.get("checkpoint_ids") or []
    where = str(checkpoints[0]) if checkpoints else "no checkpoint"
    if len(checkpoints) > 1:
        where = f"{checkpoints[0]} +{len(checkpoints) - 1} more"
    return (
        f"by {record.get('actor', 'unknown')}"
        f" | {record.get('reason', 'reason not recorded')}"
        f" | verified: {record.get('verification_state', 'not_run')}"
        f" | undoable: {'yes' if record.get('undoable') else 'no'}"
        f" | checkpoint: {where}"
    )


def _diagnostic_lines(
    log_root: Path,
    repo: Any = None,
    task_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return journal diagnostics and, when configured, live LSP diagnostics.

    The live path used to be

    ``manager = LspManager.from_config(config, cwd=repo)``

    behind ``except Exception: pass``. `LspManager.from_config`'s second
    parameter is named `repo_path`, so the call raised `TypeError` on the
    first attempt in every session, the exception was swallowed, and live
    language-server diagnostics NEVER reached `/diagnostics` — only journal
    rows did, with nothing on screen to say the live check had been skipped.
    The same bug is recorded as already fixed in
    `harness/agent_kernel/strategy.py`; this CLI copy was missed.

    The live values now come from `cli.fileview.lsp_diagnostics`, which is
    the same call with the right keyword, and the REASON it produced
    nothing is a separate receipt (`lsp_state_report`) rather than a
    swallowed exception. "Not configured", "could not start", and "your code
    is clean" are three different sentences.
    """
    from cli.runview import read_diagnostics

    task_dir = _safe_task_dir(task_id, Path(log_root)) if task_id else None
    values = read_diagnostics(task_dir) if task_dir is not None else []
    if repo:
        from cli import fileview as _fv

        values.extend(_fv.lsp_diagnostics(repo))
    try:
        from cli.fileview import normalize_diagnostics

        return normalize_diagnostics(values, repo=repo)
    except Exception:
        return list(values)


def export_session(
    log_root: Path,
    state: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
    destination: Optional[Path] = None,
    *,
    share: bool = False,
) -> Path:
    """Export a privacy-filtered session/artifact without mutating its source."""
    import os
    import tempfile

    from shared.privacy import apply_privacy_mode

    root = Path(log_root)
    target = str(task_id or (state or {}).get("task_id") or "")
    task_dir = _safe_task_dir(target, root) if target else None
    facts = {}
    if task_dir is not None:
        try:
            from cli.runview import read_run_facts

            facts = read_run_facts(task_dir)
        except Exception:
            facts = {}
    payload = {
        "schema_version": 1,
        "exported_at": time.time(),
        "repo": str((state or {}).get("repo") or ""),
        "task": facts,
        "conversation": (state or {}).get("conversation") or {},
        "turns": (state or {}).get("history") or [],
    }
    transformed = apply_privacy_mode(payload, "shareable" if share else "redacted")
    if destination is None:
        destination = root / "_exports" / (
            f"session-{target or 'current'}-share.json"
            if share
            else f"session-{target or 'current'}.json"
        )
    destination = Path(destination).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise OSError("export destination must not be a symlink")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(transformed, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            Path(temporary).unlink()
        except OSError:
            pass
    return destination


def _command_state_snapshot(
    log_root: Path,
    last: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    """Return the journal projection and normalized state for one REPL command."""
    live = _live_run()
    target = active_task_id(Path(log_root), last or {}, live)
    snapshot: Dict[str, Any] = {}
    if target:
        task_dir = _safe_task_dir(target, Path(log_root))
        if task_dir is not None:
            try:
                from cli.runview import read_live_projection

                snapshot = read_live_projection(task_dir)
            except Exception:
                snapshot = {}
    pending = bool(
        target and _pending_approval_request(Path(log_root), target) is not None
    )
    state = _commands.normalize_terminal_state(
        snapshot,
        in_flight=live is not None,
        waiting_for_approval=pending,
        has_task=target is not None,
    )
    return snapshot, state


def _slash_command(
    line: str, low: str, last: Dict[str, Any], log_root: Path, state: Dict[str, Any]
):
    """Resolve, execute, and record one slash command on the REPL surface."""
    raw = str(line or "").strip()
    snapshot, state_before = _command_state_snapshot(Path(log_root), last)
    live = _live_run()
    target = active_task_id(Path(log_root), last, live)
    pending = bool(
        target and _pending_approval_request(Path(log_root), target) is not None
    )
    context = _commands.surface_command_context(
        "repl",
        in_flight=live is not None,
        snapshot=snapshot,
        task_id=str(target or ""),
        pending_approval=pending,
    )
    # THE ALIAS SEAM, MOUNTED (REPL dispatcher). `resolve_line` hands every
    # line it does not rewrite to the registry byte-identically, so this is
    # additive: 7 aliases, zero changes to an existing command.
    from cli import command_aliases as _aliases

    resolution = _aliases.resolve_line(raw, context)
    # VEX-CS-01: a row whose handler no shell owns (`interactive_dispatch ==
    # "flag-only"`) is refused HERE, in the preflight, with the flag that does
    # the work. It is deliberately not a branch in `_slash_command_impl`:
    # `test_cli_terminal_parity.py` reads both dispatchers with `ast` and
    # requires the SAME set of command keys, so a REPL-only branch would be a
    # command one shell has and the other does not - the exact defect that pin
    # was written for.
    if resolution.spec is not None and resolution.status == "ok":
        handed_off = _commands.interactive_dispatch_refusal(resolution.spec)
        if handed_off:
            resolution = replace(
                resolution,
                status="disabled",
                message=handed_off,
                exit_code=EXIT_CODES_USAGE,
            )
    # A project/global custom command is a real command that is not in
    # `COMMAND_SPECS`, so an unregistered name is a refusal only when no
    # template backs it (R2-18: the same rule on all three surfaces).
    if resolution.status != "ok" and not (
        resolution.spec is None
        and resolution.status in {"unknown", "not_command"}
        and _commands.is_custom_command_line(raw, state.get("repo"))
    ):
        con = ui.console()
        if resolution.spec is None or resolution.status in {
            "unknown",
            "invalid",
            "not_command",
        }:
            line_text = (
                _commands.unknown_command_line(resolution)
                if resolution.spec is None
                else resolution.message
            )
            con.print(f"[vex.error]{escape(line_text)}[/]")
            if resolution.spec is None:
                con.print(f"[vex.muted]{escape(_commands.command_recovery_hint(None))}[/]")
            elif resolution.status == "invalid":
                # A usage error is half a refusal without the way forward. The
                # same sentence the `disabled` branch prints, from the same
                # authority, so a typo and a state refusal read alike.
                con.print(
                    f"[vex.muted]"
                    f"{escape(_commands.command_recovery_hint(resolution.spec))}"
                    f"[/]"
                )
        else:
            con.print(
                f"[vex.warn]{escape(resolution.spec.name)} unavailable: "
                f"{escape(resolution.message)}[/]"
            )
            con.print(
                f"[vex.muted]{escape(_commands.command_usage(resolution.spec))}[/]"
            )
            con.print(
                f"[vex.muted]{escape(_commands.command_recovery_hint(resolution.spec))}[/]"
            )
        # R2-18: the record is built by the SHARED reducer, so the REPL
        # cannot publish an envelope, a status, or an exit code the other
        # two surfaces would not. An unknown command is a refusal here for
        # the same reason it is one in the headless adapter.
        state["last_command"] = _commands.command_record(
            command=resolution.command,
            args=resolution.args,
            surface="repl",
            spec=resolution.spec,
            status=resolution.status,
            exit_code=resolution.exit_code,
            state_before=state_before,
            state_after=state_before,
            message=resolution.message,
            recovery=resolution.recovery,
            task_id=str(target or ""),
            verification_state=str(snapshot.get("verification_state") or "not_run"),
            run_status=str(snapshot.get("status") or ""),
            evidence=snapshot.get("verification_evidence"),
        ).to_dict()
        # R2-18: the refusal used to be reported by the DISPATCHER, which
        # returned the status word; the preflight returns the same word for
        # the same case so `handled == "unknown"` still means "there is no
        # such command" to every caller.
        return "unknown" if resolution.status == "unknown" else None
    canonical = raw
    if resolution.spec is not None:
        canonical = f"{resolution.spec.name} {resolution.args}".rstrip()
    state.pop("_handler_result", None)
    try:
        handled = _slash_command_impl(
            canonical, canonical.lower(), last, Path(log_root), state
        )
    except BaseException as exc:
        # R2-18: the exception policy is `commands.command_failure`, the
        # same one the headless adapter uses. It used to be a hard-coded
        # `error`/1 here and in the TUI, so a Ctrl+C read as a task
        # failure in a shell and as an interruption (130) in a script.
        state["last_command"] = _commands.command_record(
            command=resolution.command,
            args=resolution.args,
            surface="repl",
            spec=resolution.spec,
            state_before=state_before,
            state_after=state_before,
            recovery=resolution.recovery,
            task_id=str(target or ""),
            verification_state=str(snapshot.get("verification_state") or "not_run"),
            run_status=str(snapshot.get("status") or ""),
            evidence=snapshot.get("verification_evidence"),
            exc=exc,
        ).to_dict()
        raise
    after_snapshot, state_after = _command_state_snapshot(Path(log_root), last)
    handler_result = state.pop("_handler_result", None)
    status = str(
        (handler_result or {}).get("status")
        or ("unknown" if handled == "unknown" else "ok")
    )
    exit_code = int(
        (handler_result or {}).get("exit_code", 2 if status == "unknown" else 0)
    )
    outcome = _commands.command_record(
        command=resolution.command,
        args=resolution.args,
        surface="repl",
        spec=resolution.spec,
        status=status,
        exit_code=exit_code,
        state_before=state_before,
        state_after=state_after,
        task_id=str(target or ""),
        verification_state=str(
            after_snapshot.get("verification_state")
            or snapshot.get("verification_state")
            or "not_run"
        ),
        run_status=str(
            after_snapshot.get("status") or snapshot.get("status") or ""
        ),
        evidence=after_snapshot.get("verification_evidence")
        or snapshot.get("verification_evidence"),
    )
    state["last_command"] = outcome.to_dict()
    return handled


def _slash_command_impl(
    line: str, low: str, last: Dict[str, Any], log_root: Path, state: Dict[str, Any]
):
    """Execute one preflight-approved slash command or custom template."""
    con = ui.console()
    parts = line.split()
    cmd = low.split()[0] if low.split() else ""

    if cmd in ("/help",):
        # R2-17: searchable. `/help <word>` ranks; bare `/help` groups.
        con.print(render_help(" ".join(parts[1:]).strip()))
        return None

    if cmd in ("/mode",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        _mode_command(state, rest, say=con.print)
        return None

    if cmd in ("/status",):
        # THE SESSION PULSE, MOUNTED (REPL twin). `cli.session
        # .session_pulse` answers "how much of my conversation is still in
        # context, how much have I spent, and is anything wrong" and
        # nothing read it for twelve rounds. It is a pure projection - its
        # own suite pins that calling it changes nothing on the session dict
        # - and it is rendered BELOW the run status, never instead of it:
        # a run's outcome and a session's health are different questions.
        #
        # It prints ONLY what it can establish. `context.fraction` is None
        # when the window is not resolvable and the line is omitted: a
        # percentage of an unknown window is a fabricated number.
        _printed_pulse = _print_session_pulse(con, state, log_root)
        target = active_task_id(Path(log_root), last)
        if not target:
            say_empty_state(con.print, "no_runs")
            return None
        from cli.runview import read_live_projection, status_is_completed, status_lines

        task_dir = _safe_task_dir(target, log_root)
        if task_dir is None:
            con.print(f"[vex.error]invalid task id: {escape(str(target))}[/]")
            return None
        snapshot = read_live_projection(task_dir)
        if not (task_dir / "state.json").is_file() or str(target).startswith("agent-"):
            for row in status_lines(
                snapshot,
                mode=str(snapshot.get("mode") or "agent_task"),
                live=not status_is_completed(snapshot.get("status"))
                and str(snapshot.get("status")) not in ("failed", "timeout", "cancelled", "blocked"),
            ):
                con.print(row)
            return None
        import argparse as _ap

        from cli.main import cmd_status  # late import: avoid a cycle

        ns = _ap.Namespace(
            task_id=target, log_root=str(last.get("log_root") or log_root)
        )
        cmd_status(ns)
        return None

    if cmd in ("/files",):
        query = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if query.startswith("@"):
            query = query[1:].strip()
        active = active_task_id(Path(log_root), last)
        snapshot = None
        task_dir = _safe_task_dir(str(active), Path(log_root)) if active else None
        if task_dir is not None:
            try:
                from cli.runview import read_live_projection

                snapshot = read_live_projection(task_dir)
            except Exception:
                snapshot = None
        try:
            from cli import fileview

            rows = fileview.file_picker_rows(
                state.get("repo") or Path.cwd(),
                query,
                changed_files=(snapshot or {}).get("changed_files") or [],
                include_symbols=True,
                limit=100,
            )
        except Exception:
            rows = []
        if not rows:
            con.print("[vex.muted]no repository files or symbols match[/]")
            return None
        con.print(
            f"[vex.accent]files[/] [vex.muted]({len(rows)} shown · "
            "directories and ranked symbols; use @path to attach one)[/]"
        )
        for row in rows[:100]:
            if row.get("kind") == "directory":
                con.print(f"  [vex.muted]{escape(str(row.get('label') or row.get('path') or ''))}[/]")
            elif row.get("qualified"):
                con.print(
                    f"  [vex.accent2]@{escape(str(row.get('qualified')))}[/] "
                    f"[vex.muted]{escape(str(row.get('path') or ''))}:{int(row.get('line') or 0)}[/]"
                )
            else:
                con.print(f"  [vex.accent2]{escape(str(row.get('path') or row.get('label') or ''))}[/]")
        return None

    if cmd in ("/checkpoints",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        active = active_task_id(Path(log_root), last)
        records = _checkpoint_lines(Path(log_root), active, state.get("repo") or Path.cwd())
        if rest.lower().startswith("compare "):
            checkpoint_id = rest.split(None, 1)[1].strip()
            result = None
            try:
                from cli import fileview

                result = fileview.compare_checkpoint(
                    state.get("repo") or Path.cwd(), log_root, checkpoint_id
                )
            except Exception as exc:
                result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            if result.get("diff"):
                con.print(f"[vex.accent]checkpoint {escape(checkpoint_id)}[/]")
                ui.print_diff(str(result["diff"]))
            else:
                con.print(
                    f"[vex.muted]checkpoint {escape(checkpoint_id)}: "
                    f"{escape(str(result.get('status') or 'unavailable'))}[/]"
                )
            return None
        if rest.lower().startswith("restore "):
            restore_args = rest.split()
            checkpoint_id = restore_args[1] if len(restore_args) > 1 else ""
            include_conversation = any(
                value.lower() in {"conversation", "all", "files+conversation"}
                for value in restore_args[2:]
            )
            try:
                from cli import fileview

                result = fileview.restore_checkpoint(
                    state.get("repo") or Path.cwd(),
                    log_root,
                    checkpoint_id,
                    include_conversation=include_conversation,
                )
            except Exception as exc:
                result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            if result.get("ok"):
                con.print(
                    f"[vex.ok]restored {escape(checkpoint_id)}[/] "
                    f"[vex.muted]({len(result.get('restored_files') or [])} file(s))[/]"
                )
            else:
                _set_handler_result(state, "failed", 1)
                con.print(
                    f"[vex.warn]restore refused[/] [vex.muted]{escape(str(result.get('status') or 'conflict'))}[/]"
                )
                for conflict in result.get("conflicts", [])[:8]:
                    con.print(f"  [vex.muted]{escape(str(conflict))}[/]")
            return None
        if not records:
            con.print("[vex.muted]no checkpoints recorded[/]")
            return None
        con.print(f"[vex.accent]checkpoints[/] [vex.muted]({len(records)})[/]")
        for index, record in enumerate(records, 1):
            identifier = record.get("checkpoint_id") or record.get("resume_token") or index
            sequence = record.get("last_event_sequence", "?")
            files = record.get("captured_paths") or record.get("agent_owned_changes") or []
            detail = f"{identifier} · seq {sequence} · {len(files) if isinstance(files, list) else 0} file(s)"
            con.print(f"  [vex.accent2]{index}[/] [vex.muted]{escape(detail)}[/]")
        con.print("[vex.muted]compare: /checkpoints compare <id> · restore: /checkpoints restore <id> [conversation][/]")
        return None

    if cmd in ("/open",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        try:
            from cli import fileview

            path, lineno = fileview.split_open_target(rest)
            result = fileview.launch_editor_detached(path, lineno)
            if result.returncode == 0:
                con.print(f"[vex.ok]opened {escape(str(path))}[/]")
            else:
                con.print(f"[vex.error]editor exited {result.returncode}[/]")
                if result.stderr:
                    con.print(f"[vex.muted]{escape(result.stderr[:400])}[/]")
        except Exception as exc:
            con.print(f"[vex.error]cannot open {escape(rest)!r}:[/] {escape(str(exc))}")
        return None

    if cmd in ("/repo",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if not rest:
            con.print("[vex.muted]usage: /repo <path>[/]")
            return None
        try:
            from cli import fileview

            changed = fileview.reload_repo_settings(Path(rest), state)
            con.print("[vex.ok]repo switched[/]")
            if changed["changed"]:
                con.print("[vex.accent]effective settings changed:[/]")
                for row in changed["effective_diff"]:
                    con.print(
                        f"  [vex.muted]{escape(str(row['key']))}[/] "
                        f"old=[vex.muted]{escape(str(row['before']))}[/] "
                        f"new=[vex.accent]{escape(str(row['after']))}[/]"
                    )
            con.print(f"[vex.muted]log root: {escape(str(changed['log_root']))}[/]")
        except Exception as exc:
            con.print(f"[vex.error]cannot switch repo:[/] {escape(str(exc))}")
        return None

    if cmd in ("/doctor",):
        tokens = line.split()
        json_mode = "--json" in tokens[1:]
        try:
            from cli import doctor

            log_root = state.get("_log_root") or str(log_root)
            record = doctor.run_doctor(
                repo_path=str(state.get("repo") or Path.cwd()),
                log_root=str(log_root),
            )
            if json_mode:
                con.print(ui.strip_ansi(doctor.render_doctor_json(record)))
            else:
                con.print(doctor.render_doctor_human(record))
        except Exception as exc:
            con.print(f"[vex.error]doctor failed:[/] {escape(str(exc))}")
        return None

    if cmd in ("/diagnostics",):
        values = _diagnostic_lines(
            Path(log_root), state.get("repo"), active_task_id(Path(log_root), last)
        )
        from cli import fileview as _fv

        try:
            receipt = _fv.lsp_state_report(state.get("repo"))
        except Exception as exc:
            receipt = {
                "available": False,
                "state": "unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        from cli.a11y import diagnostic_row, lsp_state_sentence

        if not values:
            # Three different facts, three different sentences. The previous
            # "no diagnostics available" made an ABSENT live check
            # indistinguishable from a CLEAN workspace.
            con.print(f"[vex.muted]{escape(lsp_state_sentence(receipt))}[/]")
            return None
        con.print(f"[vex.accent]diagnostics[/] [vex.muted]({len(values)})[/]")
        for item in values[:40]:
            if isinstance(item, Mapping):
                con.print(f"  [vex.error]{escape(diagnostic_row(item))}[/]")
            else:
                con.print(f"  [vex.muted]{escape(str(item))}[/]")
        if len(values) > 40:
            con.print(
                f"[vex.muted]… {len(values) - 40} more; "
                "the TUI panel lists all of them[/]"
            )
        con.print(f"[vex.muted]{escape(lsp_state_sentence(receipt))}[/]")
        return None

    if cmd in ("/relevant",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        from cli import fileview as _fv

        rows = _fv.relevant_file_rows(
            _fv.relevant_projection(
                log_root,
                state.get("repo") or Path.cwd(),
                active_task_id(Path(log_root), last),
                include_git=True,
            ),
            rest,
        )
        if not rows:
            con.print(
                "[vex.muted]no relevant files — nothing is changed, staged, "
                "or cited for this run[/]"
            )
            return None
        con.print(
            f"[vex.accent]relevant[/] [vex.muted]({len(rows)} ranked; "
            "each row states why)[/]"
        )
        for row in rows[:60]:
            con.print(
                f"  [vex.accent2]{int(row.get('rank') or 0):>2}[/] "
                f"{escape(str(row.get('path') or '?'))} "
                f"[vex.muted]{escape(str(row.get('reason') or ''))}[/]"
            )
        if len(rows) > 60:
            con.print(f"[vex.muted]… {len(rows) - 60} more[/]")
        con.print("[vex.muted]attach one with @path[/]")
        return None

    if cmd in ("/context",):
        active = active_task_id(Path(log_root), last)
        issue = str(last.get("issue") or last.get("answer") or "")
        task_dir = _safe_task_dir(str(active), Path(log_root)) if active else None
        if task_dir is not None:
            try:
                from cli.runview import read_live_projection

                issue = str(read_live_projection(task_dir).get("issue") or issue)
            except Exception:
                pass
        for row in context_lines(
            state.get("repo") or Path.cwd(), issue, config=state.get("file_config") or {}
        ):
            con.print(row)
        return None

    if cmd in ("/export", "/share"):
        if _live_run() is not None:
            con.print("[vex.warn]wait for the run to finish before exporting[/]")
            return None
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        target = active_task_id(Path(log_root), last)
        try:
            destination = export_session(
                Path(log_root),
                state,
                target,
                Path(rest) if rest else None,
                share=cmd == "/share",
            )
            con.print(f"[vex.ok]{'shared' if cmd == '/share' else 'exported'}[/] [vex.muted]{escape(str(destination))}[/]")
        except Exception as exc:
            con.print(f"[vex.error]export failed:[/] {escape(str(exc))}")
        return None

    if cmd in ("/diff",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        low_rest = rest.lower()
        if (
            low_rest == "undo"
            or low_rest.startswith("undo ")
            or low_rest == "undo all"
            or low_rest == "all"
        ):
            if _live_run() is not None:
                con.print("[vex.warn]undo is unavailable while a run is active[/]")
                return None
            arg = (
                rest[len("undo") :].strip()
                if low_rest.startswith("undo")
                else rest.strip()
            )
            _render_undo_result(
                undo_result(last, log_root, state.get("repo"), arg), last
            )
            return None
        tid = active_task_id(Path(log_root), last)
        if rest and rest.lower() not in {"all", "undo"}:
            # Local import on purpose: `fileview` is bound inside other
            # handlers of this same function (each in its own `try`), which
            # makes it a LOCAL of the whole dispatch. A `/diff` that reached
            # for it without importing raised
            # `UnboundLocalError: local variable 'fileview' referenced
            # before assignment` — found by the real-terminal run, not by
            # reading the code.
            from cli import fileview

            projection = file_projection(
                log_root,
                state.get("repo") or Path.cwd(),
                tid,
                include_git=True,
            )
            target = fileview.parse_diff_target(rest)
            requested = str(target.get("path") or "")
            hunk_number = target.get("hunk")
            line_number = target.get("line")
            if not requested:
                con.print(
                    f"[vex.muted]no diff recorded for {escape(rest)}[/] "
                    "[vex.muted](use /diff <file>, <file>#<hunk>, or <file>:<line>)[/]"
                )
                return None
            try:
                if hunk_number is not None and line_number is None:
                    selected = fileview.open_diff_file(projection, requested, hunk_number)
                else:
                    selected = fileview.open_diff_line(projection, requested, line_number)
                    if selected is not None and hunk_number is not None:
                        # `file#hunk:line` names both; narrowing the hunks
                        # keeps the address pointing at the one it named
                        selected["hunks"] = [
                            hunk
                            for hunk in (selected.get("hunks") or [])
                            if isinstance(hunk, Mapping)
                            and int(hunk.get("index", 0)) == hunk_number
                        ]
            except Exception:
                selected = None
            if selected:
                lines: List[str] = []
                for hunk in selected.get("hunks") or []:
                    if not isinstance(hunk, Mapping):
                        continue
                    if hunk_number is not None and int(hunk.get("index", 0)) != hunk_number:
                        continue
                    lines.append(str(hunk.get("header") or ""))
                    lines.extend(str(line) for line in hunk.get("lines") or [])
                if lines:
                    con.print(
                        f"[vex.accent]{escape(str(selected.get('path')))}[/] "
                        f"[vex.muted]{escape(str(selected.get('summary') or selected.get('status') or ''))}[/]"
                    )
                    con.print(f"[vex.muted]{escape(file_change_attribution(selected))}[/]")
                    if line_number:
                        kind = str(selected.get("selected_line_kind") or "unknown")
                        con.print(
                            f"[vex.accent2]line {line_number} · {kind}[/]"
                        )
                    ui.print_diff("\n".join(lines))
                    return None
            con.print(f"[vex.muted]no diff recorded for {escape(rest)}[/]")
            return None
        if last.get("diff"):
            if not rest:
                try:
                    projection = file_projection(
                        log_root,
                        state.get("repo") or Path.cwd(),
                        tid,
                        include_git=True,
                    )
                    summary = (projection.get("diff") or {}).get("summary") or {}
                    if summary.get("large"):
                        con.print(
                            f"[vex.muted]diff summary:[/] {escape(str(summary.get('headline') or 'large diff'))} "
                            "[dim]· open a file with /diff <file>[/]"
                        )
                except Exception:
                    pass
            # THE MOUNT, REPL twin. `cli.review` is asked first and can
            # DECLINE: `review_command` returns `handled=False` for every
            # verb it does not own, which is what keeps the historical
            # per-file and bare-`all` paths above reachable and
            # byte-identically. One dispatcher, two shells - a REPL
            # reviewer and a TUI reviewer cannot decide differently.
            _handled = _render_review_surface(
                con, state, log_root, rest, in_flight=_live_run() is not None
            )
            if _handled:
                return None
            ui.print_diff(last["diff"])
            return None
        # Agent runs that made no recorded diff yet: recompute live
        # (pristine reference vs the repo) so /diff works post-session.
        if tid and str(tid).startswith("agent-"):
            projection = file_projection(
                log_root,
                state.get("repo") or Path.cwd(),
                tid,
                include_git=True,
            )
            diff = (projection.get("diff") or {}).get("text") or ""
            if not diff:
                from harness.agent_loop import agent_diff as _adiff

                diff = _adiff(tid, Path(log_root), str(state.get("repo") or Path.cwd())) or ""
            if diff:
                last["diff"] = diff
                ui.print_diff(diff)
                return None
        say_empty_state(con.print, "no_diff")
        return None

    if cmd in ("/sessions",):
        _print_sessions(
            con,
            log_root,
            line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else "",
            root_scoped=bool((state or {}).get("_headless_scoped")),
        )
        return None

    if cmd in ("/fork",):
        _fork_command(con, log_root, state, line)
        return None

    if cmd in ("/import",):
        _import_command(con, log_root, state, line)
        return None

    if cmd in ("/recover",):
        _recover_command(con, log_root, state, line)
        return None

    if cmd in ("/feed",):
        tid = last.get("task_id")
        if not tid:
            say_empty_state(con.print, "no_runs")
            return None
        _print_feed(
            con,
            log_root,
            tid,
            line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else "",
        )
        return None

    if cmd in ("/resume",):
        parts = line.split()
        if len(parts) < 2:
            recent = most_recent_resumable(log_root)
            if recent is None:
                con.print("[vex.muted]usage: /resume <task_id|session_id> (see /sessions)[/]")
                return None
            recent_id = recent.get("task_id")
            recent_root = Path(str(recent.get("log_root") or log_root))
            if _safe_task_dir(recent_id, recent_root) is None:
                con.print(
                    f"[vex.error]invalid task id: {recent_id!r} "
                    "(expected a single contained path segment)[/]"
                )
                _set_handler_result(state, "failed", 1)
                return None
            con.print(
                f"[vex.accent]resuming[/] [vex.muted]{recent_id} â€” "
                f"{str(recent.get('issue') or '')[:80]}[/]"
            )
            try:
                _fold_resumed(
                    _resume_task(recent_id, recent_root, state),
                    last,
                    recent_root,
                    state,
                )
            except KeyboardInterrupt:
                con.print(
                    "\n[vex.warn]interrupted â€” resumable via "
                    "[vex.accent]vex --continue[/]"
                )
            except Exception as exc:
                from cli.errors import explain_exception

                con.print(f"[vex.error]resume of {recent['task_id']!r} failed:[/]")
                explain_exception(exc)
                _set_handler_result(state, "failed", 1)
            return None
        task_id = parts[1]
        if _safe_task_dir(task_id, log_root) is None:
            # Not a run in this log root: it may be a conversation short id
            # (what /fork prints) from any indexed repository.
            if _resume_conversation_command(con, state, task_id):
                return None
            con.print(
                f"[vex.error]invalid task id: {task_id!r} "
                "(expected a single contained path segment)[/]"
            )
            _set_handler_result(state, "failed", 1)
            return None
        try:
            _fold_resumed(
                _resume_task(task_id, log_root, state), last, log_root, state
            )
        except KeyboardInterrupt:
            con.print(
                "\n[vex.warn]interrupted â€” resumable via "
                "[vex.accent]vex --continue[/]"
            )
        except Exception as exc:
            # Task D: plain-language explanation (bad task id, unreadable
            # state, sandbox down...) â€” plus the id that was asked for.
            from cli.errors import explain_exception

            con.print(f"[vex.error]resume of {parts[1]!r} failed:[/]")
            explain_exception(exc)
            _set_handler_result(state, "failed", 1)
        return None

    if cmd in ("/approve", "/reject"):
        values = line.split(None, 1)[1].strip().split() if len(line.split(None, 1)) > 1 else []
        scope_words = {"once", "session", "path", "command", "y", "s", "p", "c"}
        if values and values[0].lower() in scope_words:
            scope = values[0]
            tid = active_task_id(Path(log_root), last)
        else:
            tid = values[0] if values else active_task_id(Path(log_root), last)
            scope = values[1] if len(values) > 1 else "once"
        if not tid:
            con.print(
                "[vex.muted]no task to decide on â€” run with "
                "--approval or approve from a benchmark[/]"
            )
            return None
        policy = state.get("approval_policy")
        if not isinstance(policy, _commands.ApprovalPolicy):
            policy = _commands.session_approval_policy()
            state["approval_policy"] = policy
        decided = _decide_pending(
            log_root,
            tid,
            approve=(cmd == "/approve"),
            scope=scope,
            policy=policy,
        )
        if decided is None:
            con.print(f"[vex.muted]no pending approval request for {tid}[/]")
            _set_handler_result(state, "failed", 1)
        return None

    if cmd in ("/quit", "/exit"):
        # R2-18: `/quit` used to be a LOOP fast path that printed "bye"
        # and returned 0 before the dispatcher, the preflight, and the
        # command record existed — so a `/quit` produced no record on this
        # surface while the TUI recorded one, and the registry row the
        # user reads in `/help` described a command this shell did not
        # own. It is a command here now; the exit is unchanged.
        if _live_run() is not None:
            con.print("[vex.warn]/cancel the active run before /quit[/]")
            _set_handler_result(state, "failed", EXIT_CODES_USAGE)
            return None
        con.print("[vex.muted]bye[/]")
        return _COMMAND_EXIT

    if cmd in ("/watch",):
        # R2-18: `/watch` was a TUI-only branch with no registry row, so it
        # executed in one shell, was "unknown command" in the REPL, and was
        # invisible to the palette, `/help`, and the headless table — a
        # command the user could learn from one surface and not use in
        # another. It is a registry row now, and the REPL says the same
        # thing the TUI says: the follower's job belongs to
        # `vex watch <task-id>`, in its own terminal, because this
        # session's own reader owns stdin.
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        target = rest or str(active_task_id(Path(log_root), last) or "")
        if not target:
            con.print(
                "[vex.muted]usage: /watch <task-id>[/] [vex.muted]"
                "(or `vex watch <task-id>` from another terminal)[/]"
            )
            _set_handler_result(state, "failed", EXIT_CODES_USAGE)
            return None
        con.print(
            f"[vex.accent]watching[/] [vex.muted]{escape(target)} — "
            f"in another terminal: [vex.accent]vex watch {escape(target)}[/][/]"
        )
        return None

    if cmd in ("/steer",):
        # R2-18: `/steer` existed ONLY on the reader thread
        # (`_ReplReader`), so a `/steer` that reached the main loop's
        # dispatcher — which is where a script, a piped stdin, or a
        # redirect lands, and where the OTHER in-flight commands live —
        # came back "unknown command: /steer" and exit 2. One command,
        # two behaviours, depending on which thread the user's keystroke
        # happened to arrive on. Both call this same function.
        instruction = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if not instruction:
            con.print(
                "[vex.muted]usage: /steer <instruction> (plain text while a "
                "run is live does the same)[/]"
            )
            _set_handler_result(state, "failed", EXIT_CODES_USAGE)
            return None
        live = _live_run()
        if not live:
            _snapshot, current_state = _command_state_snapshot(Path(log_root), last)
            if current_state not in {"running", "waiting_for_approval", "resumed"}:
                con.print(
                    "[vex.muted]nothing running to steer — /steer applies at "
                    "the next safe boundary of a live run[/]"
                )
                _set_handler_result(state, "failed", EXIT_CODES_USAGE)
                return None
            live = {
                "task_id": str(active_task_id(Path(log_root), last) or ""),
                "log_root": str(log_root),
            }
        if not live.get("task_id"):
            con.print("[vex.muted]nothing running to steer[/]")
            _set_handler_result(state, "failed", EXIT_CODES_USAGE)
            return None
        from harness import steering as _steering

        ack = steer_live_run(
            instruction, live["task_id"], Path(live["log_root"]), say=con.print
        )
        if str(ack or "") not in _steering.INTENTS:
            # `steer_live_run` returns an intent when the run ACCEPTED the
            # instruction, and one of `None` / "starting" / "refused"
            # when it did not. The ack already says which and why; the
            # record has to agree, or a user reading the state (or a
            # script reading the exit code) sees a refusal the record
            # calls a success.
            _set_handler_result(state, "failed", EXIT_CODES_USAGE)
        return None

    if cmd in ("/cancel",):
        if _live_run() is None:
            _snapshot, current_state = _command_state_snapshot(Path(log_root), last)
            if current_state not in {"running", "waiting_for_approval", "resumed"}:
                con.print("[vex.muted]nothing running[/]")
                _set_handler_result(state, "failed", 1)
                return None
        con.print(
            "[vex.warn]cancel requested â€” sending Ctrl+C semantics "
            "to the running task (checkpoints kept)[/]"
        )
        _interrupt_main()
        return None

    # VEX-CEILING-10 background runs. /detach leaves the run ALIVE: it
    # writes the background control record and stops the REPL's own live
    # gate, but it NEVER interrupts the worker. /attach is the inverse and
    # works against a run whose process is gone, because it replays the
    # run's own event journal.
    if cmd in ("/detach",):
        from cli import background as _bg

        live = _live_run()
        tid = (live or {}).get("task_id") or active_task_id(Path(log_root), last)
        if not tid:
            con.print("[vex.muted]nothing running to detach[/]")
            _set_handler_result(state, "failed", 1)
            return None
        mode = str((live or {}).get("mode") or state.get("mode") or "fix")
        path = _bg.detach(Path(log_root), str(tid), mode=mode, note="repl /detach")
        if path is None:
            con.print(
                f"[vex.error]could not detach {tid} (control record not writable) "
                "â€” the run continues here[/]"
            )
            _set_handler_result(state, "failed", 1)
            return None
        # Stop watching without stopping the run. The worker keeps going in
        # this process; the live gate is cleared so the REPL accepts new
        # input instead of steering a run the user has stepped away from.
        _clear_live_run()
        con.print(
            f"[vex.accent]detached[/] [vex.muted]{tid} is still running â€” "
            f"follow it with [vex.accent]vex watch {tid}[/], "
            f"rebind with [vex.accent]/attach[/][/]"
        )
        return None

    if cmd in ("/attach",):
        from cli import background as _bg

        parts = line.split(None, 1)
        tid = parts[1].strip() if len(parts) > 1 else ""
        tid = tid or str(active_task_id(Path(log_root), last) or "")
        if not tid:
            listed = _bg.list_detached(Path(log_root), limit=1)
            tid = listed[0].task_id if listed else ""
        if not tid:
            con.print(
                "[vex.muted]no detached run to attach â€” "
                "pass one with [vex.accent]/attach <task-id>[/][/]"
            )
            _set_handler_result(state, "failed", 1)
            return None
        receipt = _bg.attach(Path(log_root), str(tid))
        if not receipt.get("attached"):
            con.print(
                f"[vex.error]cannot attach {tid}: "
                f"{receipt.get('reason') or 'unknown reason'}[/]"
            )
            _set_handler_result(state, "failed", 1)
            return None
        con.print(
            f"[vex.accent]attached[/] [vex.muted]{tid} â€” replayed "
            f"{receipt.get('events', 0)} events Â· phase "
            f"{receipt.get('phase') or 'unknown'}"
            + (
                f" Â· status {receipt.get('status')}"
                if receipt.get("status")
                else ""
            )
            + "[/]"
        )
        detail = str(receipt.get("live_text") or "").strip()
        if detail:
            con.print(f"[vex.muted]{detail[-600:]}[/]")
        return None

    if cmd in ("/quiet",):
        state["quiet"] = not state.get("quiet", False)
        con.print(f"[vex.ok]verbosity: {'quiet' if state['quiet'] else 'normal'}[/]")
        return None

    if cmd in ("/trace",):
        _trace_feed_command(parts[1] if len(parts) > 1 else None, last, log_root)
        return None

    if cmd in ("/model",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            # /model <name>: pin for subsequent runs (like `model <name>`).
            state["model"] = rest
            con.print(f"[vex.ok]model pinned {ui.GLYPHS['arrow']} {state['model']}[/]")
            return None
        # /model: show the effective model + where it came from.
        from cli.onboard import format_model_display

        con.print(
            f"[vex.muted]{format_model_display(state, state.get('file_config'))}[/]"
        )
        return None

    if cmd in ("/effort", "/thinking"):
        from cli.commands import apply_effort, effort_receipt, render_effort

        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        receipt = effort_receipt(state, state.get("file_config"), rest)
        # `apply_effort` writes BOTH the session key and VEX_EFFORT, so the
        # router resolves the new rung on the next run even when that run's
        # config is assembled by a path this REPL does not own.
        applied = apply_effort(state, receipt)
        for rendered in render_effort(receipt):
            role = "vex.warn" if rendered.startswith("effort unchanged") else "vex.muted"
            con.print(f"[{role}]{escape(rendered)}[/]")
        if applied:
            con.print(
                f"[vex.ok]effort {receipt.get('level')}"
                f"{ui.GLYPHS['arrow']} applies from the next model call"
                "[/] [vex.muted](mid-run: the current turn keeps its level)[/]"
            )
        return None

    if cmd in ("/plan",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            # /plan <text>: agent-shaped text gets the lightweight agent
            # preview (steps/files, approve -> run, edit -> steer); anything
            # else keeps the fix-loop step preview (approve before edits).
            try:
                from harness.agent_loop import classify_agent_input as _classify

                _kind = _classify(rest, config={}).kind
            except Exception:
                _kind = "fix"
            if _kind == "agent_task":
                repo_p = Path(state["repo"]) if state.get("repo") else _detect_repo()
                try:
                    guidance = _agent_plan_preview(rest, repo_p, state)
                except Exception:
                    guidance = rest
                if guidance is None:
                    return None
                try:
                    if str(state.get("mode") or "auto").lower() not in ("", "auto"):
                        result_info = _run_one_mode(
                            rest,
                            repo_p,
                            state,
                            log_root,
                            file_config=state.get("file_config"),
                            mode=str(state.get("mode")),
                            plan_guidance=guidance or "",
                        )
                    else:
                        result_info = _run_one_agent(
                            rest,
                            repo_p,
                            state,
                            log_root,
                            file_config=state.get("file_config"),
                            plan_guidance=guidance,
                        )
                except KeyboardInterrupt:
                    con.print(
                        "\n[vex.warn]interrupted â€” live repo kept as-is "
                        "([vex.accent]/diff undo[/][vex.warn] reverts)[/]"
                    )
                    return None
                except Exception as exc:
                    from cli.errors import explain_exception

                    explain_exception(exc, save_traceback=True)
                    return None
                if result_info is not None:
                    last.update(result_info)
                return None
            state["plan_preview"] = True
            con.print(
                "[vex.accent]plan mode[/] [vex.muted]â€” previewing steps "
                "before edits (approve/reject)[/]"
            )
            try:
                result_info = _run_one_fix(
                    rest,
                    Path(state["repo"]) if state.get("repo") else _detect_repo(),
                    state,
                    log_root,
                    file_config=state.get("file_config"),
                )
            except KeyboardInterrupt:
                con.print(
                    "\n[vex.warn]interrupted â€” containers cleaned, "
                    "checkpoints kept ([vex.accent]vex --continue[/]"
                    "[vex.warn] resumes)[/]"
                )
                return None
            except Exception as exc:  # never dump a traceback on the user
                from cli.errors import explain_exception

                explain_exception(exc, save_traceback=True)
                return None
            if result_info is not None:
                last.update(result_info)
            return None
        state["plan_preview"] = not state.get("plan_preview")
        con.print(
            f"[vex.ok]plan preview: {'on' if state['plan_preview'] else 'off'}[/] "
            "[vex.muted](next fix previews its steps before edits)[/]"
        )
        return None

    if cmd in ("/review",):
        # A project/plugin "review.md" custom command keeps working:
        # "/review <args>" with a resolvable template runs that
        # template (the documented plugin example). A bare "/review"
        # (or no template on disk) renders the builtin view: the last
        # fix's diff + its rationale together.
        from cli import commands as commands_mod

        rest = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
        if rest.strip():
            template = commands_mod.load_command("review", repo_path=state.get("repo"))
            if template is not None:
                filled = commands_mod.fill_template(template, rest)
                con.print()
                ui.rule("[vex.accent]/review â€” running as a fix request[/]")
                con.print(f"[vex.muted]instruction:[/]\n{filled}")
                con.print()
                try:
                    result_info = _run_one_fix(
                        filled,
                        Path(state["repo"]) if state.get("repo") else _detect_repo(),
                        state,
                        log_root,
                        file_config=state.get("file_config"),
                    )
                except KeyboardInterrupt:
                    con.print(
                        "\n[vex.warn]interrupted â€” containers cleaned, "
                        "checkpoints kept ([vex.accent]vex --continue[/]"
                        "[vex.warn] resumes)[/]"
                    )
                    return None
                except Exception as exc:
                    from cli.errors import explain_exception

                    explain_exception(exc, save_traceback=True)
                    return None
                if result_info is not None:
                    last.update(result_info)
                return None
        _render_review(last, log_root)
        return None

    if cmd in ("/compact",):
        conv = state.get("conversation")
        if not isinstance(conv, dict):
            con.print("[vex.muted]no conversation to compact yet[/]")
            return None
        try:
            from cli.session import compact_session, save_session

            summary = compact_session(conv, log_root)
            save_session(log_root, conv)
        except Exception:
            summary = ""
        if summary:
            con.print(
                "[vex.ok]compacted[/] [vex.muted]â€” older turns summarized "
                "(recall-backed), recent turns kept[/]"
            )
            con.print(f"[vex.muted]{summary[:400]}[/]")
        else:
            con.print("[vex.muted]nothing to compact yet[/]")
        return None

    if cmd in ("/copy-diff", "/copy"):
        diff = last.get("diff")
        if not diff:
            say_empty_state(con.print, "no_diff")
            return None
        try:
            from cli.session import copy_text_to_clipboard

            ok = copy_text_to_clipboard(str(diff))
        except Exception:
            ok = False
        if ok:
            con.print("[vex.ok]diff copied to the clipboard[/]")
        else:
            con.print(
                "[vex.warn]clipboard unavailable[/] [vex.muted]â€” "
                "showing the diff instead (pipe `vex fix` output or "
                "re-run /diff)[/]"
            )
            ui.print_diff(str(diff))
        return None

    if cmd in ("/history",):
        conv = state.get("conversation")
        hist = []
        try:
            if isinstance(conv, dict):
                hist = [str(h) for h in (conv.get("history") or [])]
        except Exception:
            hist = []
        if not hist:
            con.print("[vex.muted]no input history yet[/]")
            return None
        query = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        # the /sessions filter grammar, reused: each history line is
        # scored as a session record's issue text (bare tokens AND-match;
        # key:value tokens narrow honestly).
        shown = [h for h in hist if history_matches(h, query)][-20:]
        if not shown:
            con.print(f"[vex.muted]no history matches {query!r}[/]")
            return None
        con.print("[vex.accent]input history[/] [vex.muted](most recent last)[/]")
        for h in shown:
            con.print(f"  [vex.muted]{h[:120]}[/]")
        return None

    if cmd in ("/init",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            con.print(
                "[vex.muted]usage: /init (scaffolds .vex/ in the session repo)[/]"
            )
            return None
        _do_init(state.get("repo"))
        return None

    if cmd in ("/login",):
        rest = (
            line.split(None, 1)[1].strip().lower()
            if len(line.split(None, 1)) > 1
            else ""
        )
        if rest and rest not in ("global", "project"):
            con.print("[vex.muted]usage: /login [global|project][/]")
            return None
        try:
            import argparse as _ap

            from cli.onboard import cmd_login

            rc = cmd_login(_ap.Namespace(tier=rest or "global"))
        except Exception as exc:
            con.print(f"[vex.muted](login failed: {type(exc).__name__})[/]")
            return None
        if rc == 0:
            _reload_file_config(state, start=state.get("repo"))
            con.print("[vex.ok]model configured[/]")
        elif rc == 1:
            con.print("[vex.muted]login skipped â€” `vex login` any time[/]")
        return None

    if cmd in ("/logout",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            con.print("[vex.muted]usage: /logout (removes the stored api_key)[/]")
            return None
        _do_logout(state=state, repo=state.get("repo"))
        return None

    if cmd in ("/mcp",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        # VEX-CS-01: the verb dispatch runs FIRST, so `/mcp add` and
        # `/mcp health` are verbs rather than a server label that happens to
        # be spelled `add`. A free-text first word (`/mcp mem`) resolves to
        # the `list` verb in the registry, so it reaches the SAME renderer as
        # before - this is a routing change, not a rendering change.
        spec = _commands.command_spec("/mcp")
        receipt = run_subcommand(
            spec,
            rest,
            state=state,
            log_root=log_root,
            say=con.print,
        )
        if receipt is not None:
            _render_subcommand_receipt(con, receipt, spec, rest)
            if not receipt.get("ok"):
                _set_handler_result(state, "failed", EXIT_CODES_USAGE)
            return None
        if len(rest.split()) > 1:
            con.print("[vex.muted]usage: /mcp [list|add|remove|health|call|pin|"
                      "reconnect|enable|disable][/]")
            return None
        try:
            from cli.vexconfig import merged_settings

            cfg = {
                **(merged_settings() or {}),
                **{
                    k: v
                    for k, v in {
                        "model": state.get("model"),
                        "provider": state.get("provider"),
                    }.items()
                    if v is not None
                },
            }
        except Exception:
            cfg = None
        _render_mcp(cfg, label=rest, repo_path=state.get("repo"))
        return None

    if cmd in ("/skills",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        spec = _commands.command_spec("/skills")
        receipt = run_subcommand(
            spec,
            rest,
            state=state,
            log_root=log_root,
            say=con.print,
        )
        if receipt is not None:
            _render_subcommand_receipt(con, receipt, spec, rest)
            if not receipt.get("ok"):
                _set_handler_result(state, "failed", EXIT_CODES_USAGE)
            return None
        _render_skills(state.get("repo"), filt=rest)
        return None

    if cmd in ("/cost",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            con.print("[vex.muted]usage: /cost (last run + session total)[/]")
            return None
        _render_cost(
            last,
            log_root,
            task_id=active_task_id(Path(log_root), last) if _live_run() else None,
        )
        return None

    if cmd in ("/undo",):
        if _live_run() is not None:
            con.print("[vex.warn]undo is unavailable while a run is active[/]")
            return None
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if _handle_staged_undo(state, log_root, rest, say=_slash_say_default):
            return None
        undo = undo_result(last, log_root, state.get("repo"), rest)
        _render_undo_result(undo, last)
        if str(undo.get("outcome") or "") != "done":
            _set_handler_result(state, "failed", 1)
        return None

    if cmd in ("/redo",):
        if _live_run() is not None:
            con.print("[vex.warn]redo is unavailable while a run is active[/]")
            return None
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        result = redo_result(last, log_root, state.get("repo"), rest)
        if result.get("outcome") != "done":
            _set_handler_result(state, "failed", 1)
        if result.get("outcome") == "done":
            con.print(f"[vex.ok]redid {len(result.get('files') or [])} file(s)[/]")
            if result.get("diff"):
                last["diff"] = result["diff"]
                ui.print_diff(result["diff"])
            else:
                last["diff"] = None
        elif result.get("outcome") == "not_agent":
            con.print("[vex.muted]redo is for agent sessions[/]")
        elif result.get("outcome") == "nothing":
            con.print("[vex.muted]nothing to redo[/]")
        else:
            con.print(f"[vex.error]redo failed:[/] {escape(str(result.get('error') or 'unknown error'))}")
        return None

    if cmd in ("/clear",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            con.print("[vex.muted]usage: /clear (starts a fresh conversation)[/]")
            return None
        _do_clear(log_root, state.get("repo"), state)
        return None

    if cmd in ("/build",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if not rest:
            con.print("[vex.muted]usage: /build <what to build>[/]")
            return None
        repo_path = Path(state["repo"]) if state.get("repo") else _detect_repo()
        con.print(f"[vex.accent]build[/] [vex.muted]{escape(rest)}[/]")
        result_info = _run_one_build(
            rest, repo_path, state, log_root, file_config=state.get("file_config")
        )
        if result_info is not None:
            last.update(result_info)
        return None

    if cmd in ("/ask",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if not rest:
            con.print("[vex.muted]usage: /ask <question>[/]")
            return None
        repo_path = Path(state["repo"]) if state.get("repo") else _detect_repo()
        result_info = _run_one_question(
            rest, repo_path, state, log_root, file_config=state.get("file_config")
        )
        if result_info is not None:
            last.update(result_info)
        return None

    if cmd in ("/plugins",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        spec = _commands.command_spec("/plugins")
        receipt = run_subcommand(
            spec,
            rest,
            state=state,
            log_root=log_root,
            say=con.print,
        )
        if receipt is not None:
            _render_subcommand_receipt(con, receipt, spec, rest)
            if not receipt.get("ok"):
                _set_handler_result(state, "failed", EXIT_CODES_USAGE)
            return None
        from cli import plugins as plugins_mod

        try:
            rows = plugins_mod.list_plugins()
        except Exception as exc:
            con.print(f"[vex.error]plugin list failed:[/] {escape(str(exc))}")
            return None
        if not rows:
            con.print("[vex.muted]no plugins installed[/]")
            return None
        for row in rows:
            name = str(row.get("name") or "?")
            state_word = "enabled" if row.get("enabled") else "disabled"
            # A plugin with no description left a trailing orphan [/], which
            # raised MarkupError ("closing tag '[/]' has nothing to close") and
            # took the whole /plugins listing down. Emit the closing tag only
            # when there is something to wrap.
            description = str(row.get("description") or "")
            line = f"[vex.accent]{escape(name)}[/] [vex.muted]·[/] {state_word}"
            if description:
                line += f" [vex.muted]·[/] {escape(description)}[/]"
            con.print(line)
        con.print(
            "[vex.muted]enable/disable: vex plugin enable|disable <name>[/]"
        )
        return None

    # VEX-CS-01 item 7 needs NO branch here. The four rows (`/worktree`,
    # `/hooks`, `/migrate`, `/support-bundle`) are reachable through their own
    # CLI flags, and each declares `interactive_dispatch="flag-only"` in
    # `cli.commands`, so `_slash_command`'s preflight refuses an interactive
    # line with that flag BEFORE this dispatcher is reached - and
    # `cli/command_exec.py` refuses it the same way headlessly. A branch would
    # be dead code, and `test_cli_terminal_parity.py` reads both dispatchers
    # with `ast` and requires the SAME set of command keys; a REPL-only branch
    # is a command one shell has and the other does not, which is the exact
    # defect that pin was written for. The handoff that makes these real
    # verbs is in `cli/AGENTS.md`, "Handoff to 01".

    if cmd in ("/theme",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        from cli import theme as theme_mod

        names = theme_mod.theme_names()
        active_name = names[0]
        if not rest:
            con.print(
                f"[vex.muted]theme:[/] [vex.accent]"
                f"{escape(active_name)}[/] [vex.muted]Â· available: "
                f"{', '.join(names)}[/]"
            )
            con.print(
                "[vex.muted]usage: /theme "
                f"[{'|'.join(names)}] â€” persisted with vex config set theme "
                "<name>[/]"
            )
            return None
        if rest.lower() in {"reset", "unset"}:
            try:
                from cli.vexconfig import set_tier_key

                path, _created = set_tier_key("local", "theme", active_name)
            except Exception as exc:
                con.print(f"[vex.error]theme reset failed:[/] {escape(str(exc))}")
                return None
            ui.set_theme(active_name)
            state["theme"] = active_name
            con.print(
                f"[vex.ok]theme reset to default[/] [vex.muted]"
                f"({escape(active_name)}) Â· cleared local theme key at "
                f"{escape(str(path))}[/]"
            )
            return None
        if rest not in names:
            con.print(
                f"[vex.error]unknown theme:[/] {escape(rest)} "
                f"[vex.muted]Â· available: {', '.join(names)}[/]"
            )
            return None
        try:
            from cli.vexconfig import set_tier_key

            path, _created = set_tier_key("local", "theme", rest)
        except Exception as exc:
            con.print(f"[vex.error]theme save failed:[/] {escape(str(exc))}")
            return None
        ui.set_theme(rest)
        state["theme"] = rest
        con.print(
            f"[vex.ok]theme set[/] [vex.muted]({escape(rest)}) Â· saved to "
            f"{escape(str(path))}[/]"
        )
        return None

    if cmd in ("/settings",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        from cli import vexconfig as config_mod

        if not rest:
            merged = config_mod.merged_settings()
            for key in sorted(merged):
                if key in {"api_key"}:
                    continue
                con.print(
                    f"[vex.muted]{escape(key)}[/] = [vex.accent]"
                    f"{escape(str(merged[key]))}[/]"
                )
            con.print(
                "[vex.muted]usage: /settings <key> â€” read one value; edit with "
                "vex config set <key> <value>[/]"
            )
            return None
        pieces = rest.split(None, 1)
        key = pieces[0]
        merged = config_mod.merged_settings()
        if len(pieces) == 1:
            if key.lower() in {"reset", "unset"}:
                con.print("[vex.muted]usage: /settings reset|unset <key>[/]")
                return None
            value = merged.get(key, "")
            if key == "api_key" and value:
                value = config_mod.mask_secret(str(value))
            if key not in merged:
                con.print(
                    f"[vex.error]unknown setting:[/] {escape(key)} "
                    f"[vex.muted]â€” /settings lists every effective value[/]"
                )
                return None
            con.print(
                f"[vex.muted]{escape(key)}[/] = [vex.accent]{escape(str(value))}[/]"
            )
            return None
        if len(pieces) >= 2 and key.lower() in {"reset", "unset"}:
            target = pieces[1]
            try:
                outcome, path = config_mod.unset_tier_key("local", target)
            except Exception as exc:
                con.print(f"[vex.error]settings unset failed:[/] {escape(str(exc))}")
                return None
            if outcome == "removed":
                con.print(
                    f"[vex.ok]{escape(target)}[/] [vex.muted]unset â€” back at default[/]"
                )
            else:
                con.print(
                    f"[vex.muted]{escape(target)}[/] [vex.muted]was already at default[/]"
                )
            return None
        try:
            path, _created = config_mod.set_tier_key("local", key, pieces[1].strip())
        except Exception as exc:
            con.print(f"[vex.error]settings write failed:[/] {escape(str(exc))}")
            return None
        if key == "model":
            state["model"] = pieces[1].strip()
        elif key == "provider":
            state["provider"] = pieces[1].strip()
        con.print(
            f"[vex.ok]{escape(key)}[/] [vex.muted]saved to {escape(str(path))}[/]"
        )
        return None

    # -- custom commands (Plugins round, Task B) ------------------------
    # /<name> [args...] where <name> is NOT a built-in: resolve the
    # template (project > global > plugin), fill $ARGUMENTS, and run it
    # as a fix request. The filled template is echoed first so the user
    # sees exactly what will run.
    from cli import commands as commands_mod

    name = cmd.lstrip("/")
    template = commands_mod.load_command(name, repo_path=state.get("repo"))
    if template is not None:
        arguments = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
        filled = commands_mod.fill_template(template, arguments)
        con.print()
        ui.rule(f"[vex.accent]/{name} â€” running as an agent task[/]")
        if arguments:
            con.print(f"[vex.muted]arguments: {arguments}[/]")
        con.print(f"[vex.muted]instruction:[/]\n{filled}")
        con.print()
        try:
            if str(state.get("mode") or "auto").lower() not in ("", "auto"):
                result_info = _run_one_mode(
                    filled,
                    Path(state["repo"]) if state.get("repo") else _detect_repo(),
                    state,
                    log_root,
                    file_config=state.get("file_config"),
                    mode=str(state.get("mode")),
                )
            else:
                result_info = _run_one_agent(
                    filled,
                    Path(state["repo"]) if state.get("repo") else _detect_repo(),
                    state,
                    log_root,
                    file_config=state.get("file_config"),
                )
        except KeyboardInterrupt:
            con.print(
                "\n[vex.warn]interrupted â€” containers cleaned, "
                "checkpoints kept ([vex.accent]vex --continue[/]"
                "[vex.warn] resumes)[/]"
            )
            return None
        except Exception as exc:  # never dump a traceback on the user
            from cli.errors import explain_exception

            explain_exception(exc, save_traceback=True)
            return None
        if result_info is not None:
            last.update(result_info)
        return None

    available = commands_mod.command_names(state.get("repo"))
    hint = ""
    if available:
        hint = " â€” custom commands available: " + ", ".join(f"/{n}" for n in available)
    con.print(
        f"[vex.error]unknown command: {line.split()[0]}[/] "
        f"[vex.muted]â€” try /help{hint}[/]"
    )
    return "unknown"


def _mode_config(
    state: Dict[str, Any],
    file_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Session config for the non-fix modes (question/research/build).

    Same precedence as _run_one_fix: explicit session pins > settings
    chain; base_url normalized onto runtime's api_base.
    """
    from cli.vexconfig import apply_config_defaults, normalize_runtime_keys

    return normalize_runtime_keys(
        apply_config_defaults(
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
    )


def _print_answer_head(kind: str, task_id: str, repo_name: str = "") -> None:
    """The run line for read-only modes (mirrors the fix run line)."""
    con = ui.console()
    tail = f" {ui.DOT} [vex.muted]{ui.GLYPHS['wait']} {task_id}[/]" if task_id else ""
    repo_part = f" in [vex.accent]{repo_name}[/] " if repo_name else ""
    con.print(
        f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]{kind}[/]{repo_part}"
        f"[vex.muted]{ui.DOT}[/]{tail}"
    )


def _run_one_question(
    question: str,
    repo: Path,
    state: Dict[str, Any],
    log_root: Path,
    file_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Run one Q&A (read-only) from the interactive session.

    Returns {"task_id", "log_root", "answer", "status"} for the session
    index, or None when the run couldn't start.
    """
    from cli.main import _clear_router_context, _set_router_context
    from harness.qa_mode import run_question

    cfg = _mode_config(state, file_config)
    # Pre-generate the task id so the embedded-UI hook (the TUI's live
    # sidebar/run-line) can attach BEFORE the blocking model call â€”
    # the same _fire_task_start contract _execute_task/_run_one_build
    # honor. (The REPL leaves the hook unset; firing is a no-op there.)
    import uuid

    task_id = f"qa-{uuid.uuid4().hex[:8]}"
    _fire_task_start(task_id)
    _set_router_context(_RouterCtx(cfg))  # type: ignore[arg-type]
    t0 = time.time()
    try:
        out = run_question(
            question=question,
            repo_path=str(repo),
            config=cfg,
            log_root=log_root,
            task_id=task_id,
        )
    finally:
        _clear_router_context()
    elapsed = time.time() - t0

    _print_answer_head("answering", out["task_id"], Path(repo).name)
    con = ui.console()
    if out["status"] == "success" and out["answer"]:
        con.print()
        con.print(ui.strip_ansi(out["answer"]))
    else:
        con.print("[vex.error]the question could not be answered[/]")
    con.print(
        f"[vex.muted]{len(out.get('model_calls', []))} model calls[/] "
        f"[vex.muted]{ui.DOT}[/] [vex.muted]{elapsed:.0f}s[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.accent2]{ui.fmt_cost(out.get('cost_usd', 0.0))}[/]"
    )
    con.print(f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]{out['trace_path']}[/]")
    record_session(log_root, out["task_id"], question, str(repo), out["status"])
    notify_done(out["status"])
    return {
        "task_id": out["task_id"],
        "log_root": log_root,
        "diff": None,
        "status": out["status"],
        "answer": out["answer"],
        "mode": "question",
        "elapsed_s": round(elapsed, 1),
        "cost_usd": float(out.get("cost_usd") or 0.0),
        "model_calls": list(out.get("model_calls") or []),
        "tokens": sum(
            int((call or {}).get("tokens") or 0)
            for call in (out.get("model_calls") or [])
            if isinstance(call, dict)
        ),
    }


class _RouterCtx:
    """Minimal duck-typed stand-in for Task for _set_router_context (it
    only reads .config â€” building a full Task just for router context
    would imply a repo/issue we don't mean to run)."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config


def _run_one_research(
    question: str,
    state: Dict[str, Any],
    log_root: Path,
    file_config: Optional[Dict[str, Any]] = None,
    repo: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Run one research task (read-only FETCH/DOCS-assisted)."""
    from cli.main import _clear_router_context, _set_router_context
    from harness.research_mode import run_research

    con = ui.console()
    cfg = _mode_config(state, file_config)
    # Pre-generate + fire like _run_one_question (embedded-UI hook).
    import uuid

    task_id = f"research-{uuid.uuid4().hex[:8]}"
    _fire_task_start(task_id)
    _set_router_context(_RouterCtx(cfg))  # type: ignore[arg-type]
    t0 = time.time()
    try:
        out = run_research(
            question=question,
            config=cfg,
            log_root=log_root,
            repo_path=str(repo) if repo else None,
            task_id=task_id,
        )
    finally:
        _clear_router_context()
    elapsed = time.time() - t0

    _print_answer_head("researching", out["task_id"])
    if out["status"] == "success" and out["answer"]:
        con.print()
        con.print(ui.strip_ansi(out["answer"]))
    else:
        con.print("[vex.error]the research question could not be answered[/]")
    fetch_note = (
        f"[vex.muted]{len(out.get('fetches', []))} fetches[/] [vex.muted]{ui.DOT}[/] "
        if out.get("fetches")
        else ""
    )
    con.print(
        f"{fetch_note}[vex.muted]{len(out.get('model_calls', []))} model calls[/] "
        f"[vex.muted]{ui.DOT}[/] [vex.muted]{elapsed:.0f}s[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.accent2]{ui.fmt_cost(out.get('cost_usd', 0.0))}[/]"
    )
    con.print(f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]{out['trace_path']}[/]")
    record_session(log_root, out["task_id"], question, str(repo or ""), out["status"])
    notify_done(out["status"])
    return {
        "task_id": out["task_id"],
        "log_root": log_root,
        "diff": None,
        "status": out["status"],
        "answer": out["answer"],
        "mode": "research",
        "elapsed_s": round(elapsed, 1),
        "cost_usd": float(out.get("cost_usd") or 0.0),
        "model_calls": list(out.get("model_calls") or []),
        "tokens": sum(
            int((call or {}).get("tokens") or 0)
            for call in (out.get("model_calls") or [])
            if isinstance(call, dict)
        ),
    }


def _run_one_build(
    request: str,
    repo: Path,
    state: Dict[str, Any],
    log_root: Path,
    file_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Run one build/feature task end-to-end.

    Stage 1 (authored acceptance tests) + stage 2 (the UNCHANGED fix
    loop, live-monitored like a fix run). The loop itself goes through
    the same LiveMonitor + router-context + result rendering as a fix
    (_execute_task drives run_task; here we drive run_build, which wraps
    run_task â€” so we replicate _execute_task's monitor/result core
    around the build call).
    """
    import uuid

    from cli.main import _clear_router_context, _set_router_context
    from harness.build_mode import run_build

    con = ui.console()
    cfg = _mode_config(state, file_config)
    task_id = f"build-{uuid.uuid4().hex[:8]}"
    repo_name = Path(repo).name

    con.print(
        f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]building in[/] "
        f"[vex.accent]{repo_name}[/] [vex.muted]{ui.DOT} "
        f"{ui.GLYPHS['wait']} {task_id}[/]"
    )
    _set_router_context(_RouterCtx(cfg))
    mon = LiveMonitor(task_id, log_root).start(quiet=bool((state or {}).get("quiet")))
    _fire_task_start(task_id)
    # Steering round, Task A: builds steer too (run_build wraps the
    # SAME run_task loop, whose SteeringBuffer polls
    # logs/{task_id}/steering.jsonl â€” the same journal this injects).
    _set_live_run(task_id, log_root)
    t0 = time.time()
    try:
        out = run_build(
            request_text=request,
            repo_path=str(repo),
            config=cfg,
            log_root=log_root,
            task_id=task_id,
        )
    except KeyboardInterrupt:
        record_session(log_root, task_id, request, str(repo), "interrupted")
        raise
    except Exception as exc:
        mon.stop()
        from cli.errors import explain_exception

        explain_exception(exc, save_traceback=True)
        return None
    finally:
        _clear_live_run()
        _clear_router_context()
        mon.stop()
    elapsed = time.time() - t0

    result = out.get("result")
    status = out.get("status", "error")
    if status == "already_exists":
        con.print(f"[vex.warn]nothing to build:[/] [vex.muted]{out.get('note', '')}[/]")
        record_session(log_root, task_id, request, str(repo), "already_exists")
        return {
            "task_id": task_id,
            "log_root": log_root,
            "diff": None,
            "status": "already_exists",
        }
    if result is None:
        con.print(f"[vex.error]build failed to start: {out.get('note', '?')}[/]")
        record_session(log_root, task_id, request, str(repo), status)
        return {
            "task_id": task_id,
            "log_root": log_root,
            "diff": None,
            "status": status,
        }

    display_status, style, mark = _terminal_result_display(
        result.status, result.verification
    )
    con.print()
    con.print(
        f"[{style}]{mark} {display_status}[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{result.attempts} attempt(s)[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{len(result.model_calls)} model calls[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{elapsed:.0f}s[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.accent2]{ui.fmt_cost(result.cost_usd)}[/]"
    )
    if result.verification is not None:
        v = result.verification
        t_style = "vex.ok" if v.target_test_passed else "vex.error"
        r_style = "vex.ok" if v.regression_passed else "vex.error"
        con.print(
            f"  [{t_style}]{'PASS' if v.target_test_passed else 'FAIL'} target"
            "[/]"
            f"[vex.muted] {ui.DOT} [/]"
            f"[{r_style}]{'PASS' if v.regression_passed else 'FAIL'} regression"
            "[/]"
            f"[vex.muted] {ui.DOT} flaky: {v.flaky}[/]"
        )
    con.print(
        f"[vex.muted]acceptance tests[/] [{ui.TEXT_PRIMARY}]{', '.join(out.get('acceptance_tests', []))}[/]"
    )
    if result.diff:
        con.print("[vex.muted]diff:[/]")
        ui.print_diff(result.diff)
    rat = Path(log_root) / task_id / "rationale.md"
    if rat.is_file():
        from rich.markdown import Markdown

        con.print()
        con.print(Markdown(ui.strip_ansi(rat.read_text(encoding="utf-8"))))
    con.print(
        f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]{Path(log_root).resolve() / task_id / 'trace.jsonl'}[/]"
    )
    # R2-17 (item 6): the recovery card, in the surface where the build
    # failed — what failed, why, and what to do next.
    print_run_recovery(
        con,
        log_root,
        task_id,
        result.status,
        note=str(getattr(result, "note", "") or ""),
    )
    record_session(log_root, task_id, request, str(repo), result.status)
    notify_done(result.status)
    return {
        "task_id": task_id,
        "log_root": log_root,
        "diff": result.diff,
        "status": result.status,
        "mode": "build",
        "attempts": result.attempts,
        "cost_usd": result.cost_usd,
        "model_calls": list(result.model_calls),
        "elapsed_s": round(elapsed, 1),
        "verification": result.verification,
    }


# ---------------------------------------------------------------------------
# R2-15 (TRUST) -- the daily path's boundary: resolved, shown, revocable
# ---------------------------------------------------------------------------
#
# The finding this answers is a trust asymmetry. `vex fix` runs sandboxed
# behind a verifier gate; the daily interactive path defaulted to
# `approval=auto` on LIVE HOST BASH with no sandbox. The path a user trusts
# LEAST -- "just let the agent do something in my repo" -- had the weakest
# boundary, and "unverified completion" was in the daily vocabulary while
# "unsandboxed execution" was not.
#
# Four things happen here, all before a model call:
#   1. `resolve_session_trust` asks `shared.approval.resolve_daily_trust` what
#      containment this run is ACTUALLY in, and PINS the config to it, so the
#      kernel and the receipt cannot disagree.
#   2. The receipt is printed (loudest line first) and written to
#      `logs/{task_id}/trust.json` -- a separate file, never the authoritative
#      trace, for the same reason `harness.trace.write_receipt` uses one.
#   3. The grant ledger is loaded, so an approval already given is not asked
#      again ("trust calibration").
#   4. `_trusted_approver` wraps whatever approver the surface supplies, so
#      calibration is a property of the daily PATH rather than of one prompt.
#
# The policy itself lives in `shared/approval.py` (one implementation, bottom
# layer). Nothing here decides what is allowed; it only reports the boundary and
# remembers what the operator already said yes to.

_TRUST_LOCK = threading.Lock()
_TRUST: Dict[str, Any] = {
    "ledger": None,
    "repo_key": "",
    "session_id": "",
    "path": "",
    "receipt": None,
}


def _trust_repo_key(repo: Any) -> str:
    """Return the stable key for a repository, or "" when it cannot be resolved.

    Assumes ``repo`` is a path-ish value. A repository with no stable key still
    gets a sandbox boundary and a prompt; it just cannot share a grant journal
    across sessions, which is the safe direction.
    """
    try:
        from memory.paths import repo_key as _repo_key

        return str(_repo_key(repo) or "")
    except Exception:
        return ""


def _trust_paths(args: Any) -> Tuple[str, ...]:
    """Return the path-like arguments of one tool call as a tuple of strings."""
    data = args if isinstance(args, Mapping) else {}
    out: List[str] = []
    for key in ("path", "file", "target", "source_path", "destination_path"):
        value = data.get(key)
        if value:
            text = str(value).replace("\\", "/").lstrip("./")
            if text:
                out.append(text)
    if isinstance(data.get("paths"), (list, tuple)):
        out.extend(
            str(item).replace("\\", "/").lstrip("./")
            for item in data["paths"]
            if str(item)
        )
    return tuple(dict.fromkeys(out))


def _trust_ledger() -> Optional[Any]:
    """Return the session's grant ledger, or None when trust is unresolved."""
    with _TRUST_LOCK:
        ledger = _TRUST.get("ledger")
    return ledger


def _trust_lookup(**effect: Any) -> Optional[Any]:
    """Return a remembered grant covering this effect, or None to prompt.

    Assumes the caller passes LIVE effect dimensions. Returning a grant here
    means "the operator already approved this class of effect in this session";
    the policy engine still decides whether to run it.
    """
    ledger = _trust_ledger()
    if ledger is None:
        return None
    try:
        return ledger.covering(**effect)
    except Exception:
        return None


def _trust_remember(
    *, tool: str = "", command: str = "", paths: Sequence[str] = (), scope: str = ""
) -> Optional[Any]:
    """Remember one approval in the session ledger; return the grant or None."""
    ledger = _trust_ledger()
    if ledger is None or not scope:
        return None
    try:
        return ledger.record(
            scope=scope,
            tool=tool,
            # A command grant is only meaningful for a command; an empty one is
            # refused by the ledger, so we never pass a blank prefix for a
            # command-scope grant and let it become a blanket allow.
            command_prefix=command if scope.endswith("command_prefix") else "",
            paths=tuple(paths),
        )
    except Exception:
        return None


def _trust_note(message: str) -> None:
    """Print one muted trust note without ever raising into a run."""
    try:
        ui.console().print(f"[vex.muted]{message}[/]")
    except Exception:
        pass


def session_trust_forget(tool: str = "") -> int:
    """Revoke remembered grants for this session; return how many were removed.

    Assumes ``tool`` is a canonical tool name; an empty value revokes
    everything. This is the user-facing half of "keep it revocable" -- the
    ledger is a convenience, never an authorization, so taking it back costs
    one extra prompt and nothing else.
    """
    ledger = _trust_ledger()
    if ledger is None:
        return 0
    try:
        if tool:
            return int(ledger.revoke_tool(tool) or 0)
        removed = len(ledger.grants)
        ledger.forget()
        return int(removed)
    except Exception:
        return 0


def resolve_session_trust(
    config: Dict[str, Any],
    *,
    repo: Any = "",
    log_root: Any = "",
    session_id: str = "",
    task_id: str = "",
) -> Any:
    """Resolve the daily boundary, PIN it into ``config``, and register the ledger.

    ``config`` is MUTATED on purpose: the receipt is only true if the run is
    actually configured the way the receipt says, and the kernel reads
    ``agent_process_sandboxed`` from this same mapping. Returns the
    :class:`shared.approval.DailyTrust` receipt, which the caller renders (see
    :func:`render_trust_banner`) and records (see :func:`record_trust_receipt`).

    Assumes ``config`` is the run's resolved config (settings chain merged over
    ``harness.config.DEFAULTS``); the ledger honours an explicit
    ``approval_calibration: false`` and an explicit
    ``approval_calibration_persist: true``.
    """
    import os as _os

    from shared.approval import (
        TrustLedger,
        load_trust_ledger,
        resolve_daily_trust,
        save_trust_ledger,
        trust_ledger_path,
        verify_trust_applied,
    )

    key = _trust_repo_key(repo)
    trust = resolve_daily_trust(
        config, repo_path=str(repo or ""), repo_key=key, session_id=session_id
    )
    if isinstance(config, dict):
        config.update(trust.config_patch())

    calibration = bool(trust.calibration)
    ledger: Any
    path = trust_ledger_path(log_root, key) if (log_root and key) else ""
    with _TRUST_LOCK:
        existing = _TRUST.get("ledger")
        # A ledger is REUSED across the runs of one session (that is what "stop
        # re-asking" means), and dropped when the session or the repository
        # changes -- a grant is scoped to the conversation that earned it.
        reuse = (
            calibration
            and existing is not None
            and str(_TRUST.get("repo_key") or "") == key
            and str(_TRUST.get("session_id") or "") == str(session_id or "")
        )
        if reuse:
            ledger = existing
        elif (
            calibration
            and trust.calibration_persist
            and path
            and _os.path.isfile(path)
        ):
            ledger = load_trust_ledger(path)
            ledger.repo_key = key
            ledger.session_id = str(session_id or "")
            ledger.enabled = True
        else:
            ledger = TrustLedger(repo_key=key, session_id=str(session_id or ""))
        try:
            ledger.max_grants = int(
                (config or {}).get("approval_calibration_max_grants", 200) or 200
            )
        except (TypeError, ValueError):
            pass
        _TRUST["ledger"] = ledger
        _TRUST["repo_key"] = key
        _TRUST["session_id"] = str(session_id or "")
        _TRUST["path"] = path if trust.calibration_persist else ""
        _TRUST["receipt"] = trust
    if trust.calibration_persist and path:
        save_trust_ledger(path, ledger)

    mismatch = verify_trust_applied(trust, config)
    if mismatch:
        # The receipt is about to be shown to a human; a config that disagrees
        # with it is reported rather than smoothed over.
        from dataclasses import replace as _replace

        trust = _replace(
            trust,
            notes=(*trust.notes, f"config disagrees with the receipt: {mismatch}"),
            sandboxed=bool(trust.sandboxed) and kernel_sandbox_flag(config) is not False,
        )
        with _TRUST_LOCK:
            _TRUST["receipt"] = trust
    if task_id and log_root:
        record_trust_receipt(log_root, task_id, trust, ledger)
    return trust


def kernel_sandbox_flag(config: Any) -> Optional[bool]:
    """Return the boolean the kernel will read for shell containment, or None.

    Exposed so a caller can prove a receipt against the key the kernel actually
    reads rather than against the key an operator thought they set.
    """
    from shared.approval import KERNEL_SANDBOX_KEY

    value = (config or {}).get(KERNEL_SANDBOX_KEY, None)
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"true", "yes", "on", "1"}:
        return True
    if text in {"false", "no", "off", "0"}:
        return False
    return None


def render_trust_banner(trust: Any, ledger: Any = None, *, quiet: bool = False) -> None:
    """Print the boundary in force at the moment a run starts.

    Assumes ``trust`` came from :func:`resolve_session_trust`. An unsandboxed
    run leads with a warning-shaped line and names the opt-out; a sandboxed run
    says so in one line. ``quiet`` suppresses the informational lines but NEVER
    the unsandboxed warning -- quiet is a verbosity preference, not a way to hide
    a weaker boundary.
    """
    if trust is None:
        return
    lines = list(trust.banner_lines())
    if trust.sandboxed and quiet:
        # A quiet session still gets the containment line; the detail lines are
        # what quiet suppresses.
        lines = lines[:1]
    try:
        con = ui.console()
        for line in lines:
            style = "vex.warn" if line.startswith("boundary: UNSANDBOXED") else "vex.muted"
            con.print(f"[{style}]{line}[/]")
        if ledger is not None and not quiet:
            con.print(f"[vex.muted]{ledger.summary()}[/]")
    except Exception:
        pass


def record_trust_receipt(
    log_root: Any, task_id: str, trust: Any, ledger: Any = None
) -> Optional[str]:
    """Write the boundary receipt to ``logs/{task_id}/trust.json``; return the path.

    A SEPARATE file on purpose: the authoritative ``trace.jsonl`` is the kernel's
    append-only record with contiguous sequence allocation, and a second writer
    guessing at its sequence would be able to corrupt replay. The same reason
    ``harness.trace.write_receipt`` writes ``receipt.json``. A write failure is
    reported, never raised: losing the receipt must not end the run.
    """
    if trust is None or not task_id:
        return None
    try:
        payload = trust.to_dict()
        payload["calibration_summary"] = ledger.summary() if ledger is not None else ""
        payload["grants"] = [grant.as_dict() for grant in getattr(ledger, "grants", [])]
        target = Path(log_root) / str(task_id) / "trust.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return str(target)
    except Exception:
        return None


def _trusted_approver(raw: Optional[Any]) -> Optional[Any]:
    """Wrap an approver so calibration applies to EVERY surface, not one prompt.

    Assumes ``raw`` is any callable returning a bool, a ``(bool, scope)`` tuple,
    or a mapping with ``approved``/``scope``. A remembered grant short-circuits
    the prompt; a refusal is returned unchanged. The wrapper never raises: an
    approval callback that throws must not decide the run.
    """
    if raw is None:
        return None

    def _wrapped(tool: Any = None, args: Any = None, *rest: Any) -> Any:
        try:
            if callable(raw) and not isinstance(raw, str):
                # Typed kernel callback: (call, decision).
                if hasattr(tool, "tool") and hasattr(tool, "arguments"):
                    name = str(getattr(tool, "tool", "") or "").strip().lower()
                    arguments = dict(getattr(tool, "arguments", {}) or {})
                else:
                    name = str(tool or "").strip().lower()
                    arguments = args if isinstance(args, Mapping) else {}
                remembered = _trust_lookup(
                    tool=name,
                    command=str(arguments.get("command") or ""),
                    paths=_trust_paths(arguments),
                )
                if remembered is not None:
                    _trust_note(
                        f"already approved this session ({remembered.scope})"
                    )
                    return True, remembered.scope
            answer = raw(tool, args, *rest) if callable(raw) else raw
            # R2-15: remember what the surface's approver granted, so calibration
            # covers EVERY approver rather than only the console prompt. A
            # `once` answer installs nothing, which is exactly right: it was
            # consumed at the decision site.
            if answer is not None:
                scope = ""
                if isinstance(answer, tuple) and len(answer) == 2:
                    approved, scope = bool(answer[0]), str(answer[1] or "")
                elif isinstance(answer, Mapping):
                    approved = bool(answer.get("approved"))
                    scope = str(answer.get("scope") or "")
                if approved and scope and scope != "once":
                    name = str(
                        getattr(tool, "tool", "") if hasattr(tool, "tool") else tool or ""
                    ).strip().lower()
                    arguments = (
                        dict(getattr(tool, "arguments", {}) or {})
                        if hasattr(tool, "arguments")
                        else (args if isinstance(args, Mapping) else {})
                    )
                    _trust_remember(
                        tool=name,
                        command=str(arguments.get("command") or ""),
                        paths=_trust_paths(arguments),
                        scope=scope,
                    )
            return answer
        except Exception:
            return (False, "once") if rest else False

    return _wrapped


def _agent_approve_prompt(
    tool: Any,
    args: Any = None,
    preview: str = "",
    decision: Any = None,
) -> Any:
    """Prompt for a typed or legacy tool approval with explicit scopes.

    R2-15: calibration. A fact the operator already approved in this session is
    not asked again -- the ledger answers first -- and a grant they DO give is
    remembered so the next identical effect is silent. Both directions are
    revocable through ``shared.approval.TrustLedger`` (``/trust forget`` is the
    user-facing door, see the module's Cross-terminal requests).
    """
    try:
        if hasattr(tool, "tool") and hasattr(tool, "arguments"):
            call = tool
            decision = args
            tool = str(getattr(call, "tool", "") or "tool")
            args = dict(getattr(call, "arguments", {}) or {})
            preview = str(getattr(decision, "exact_effect", "") or "")
        tool_name = str(tool).strip().lower()
        command = str((args or {}).get("command") or "")
        paths = _trust_paths(args or {})
        remembered = _trust_lookup(tool=tool_name, command=command, paths=paths)
        if remembered is not None:
            _trust_note(
                f"already approved this session ({remembered.scope})"
                f"{' · ' + remembered.command_prefix if remembered.command_prefix else ''}"
            )
            return (
                (True, remembered.scope) if decision is not None else True
            )
        con = ui.console()
        con.print(f"[vex.warn]approval needed â€” {str(tool).upper()}[/]")
        if preview:
            for line in str(preview).splitlines()[:12]:
                con.print(f"[vex.muted]  {line[:120]}[/]")
        answer = input(
            f"allow this {tool}? [y=once s=session p=path c=command n=reject] "
        ).strip().lower()
        scopes = {
            "y": "once",
            "yes": "once",
            "s": "session_path",
            "session": "session_path",
            "p": "session_path",
            "path": "session_path",
            "c": "session_command_prefix",
            "command": "session_command_prefix",
        }
        approved = answer in scopes
        scope = scopes.get(answer, "once")
        # The grant itself is recorded by `_trusted_approver`, which wraps this
        # prompt, so a TUI approver and this one calibrate identically. Recording
        # here as well would install the same fact twice.
        if decision is not None:
            return approved, scope
        return approved
    except Exception:
        return (False, "once") if decision is not None else False


def _agent_plan_preview(
    request: str, repo: Path, state: Dict[str, Any]
) -> Optional[str]:
    """Lightweight agent plan preview: show steps/files, approve/edit/cancel.

    Returns the approved guidance text (the request plus any steering
    edit), or None when the user cancels. Uses input() (the TUI maps it
    to a modal). Never raises.
    """
    try:
        from harness.agent_loop import render_agent_plan
    except Exception:
        return request
    try:
        plan = render_agent_plan(
            request,
            str(repo),
            {"model": state.get("model"), "provider": state.get("provider")},
        )
    except Exception:
        return request
    con = ui.console()
    con.print()
    ui.rule("[vex.accent]agent plan preview[/]")
    for i, step in enumerate(plan.get("steps", []), start=1):
        con.print(f"  [vex.accent]{i}.[/] {step}")
    files = plan.get("files", []) or []
    if files:
        con.print(f"[vex.muted]files: {', '.join(files[:6])}[/]")
    _fire_prompt_body(
        "run this plan? [Y/n/e] ", [str(s) for s in plan.get("steps", [])][:12]
    )
    try:
        answer = input("run this plan? [Y] run / [e]dit / [n] cancel ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("", "y", "yes", "run"):
        con.print("[vex.ok]approved â€” starting the agent[/]")
        return request
    if answer in ("e", "edit"):
        try:
            edit = input("steer the plan (one line, empty cancels): ").strip()
        except (EOFError, KeyboardInterrupt):
            edit = ""
        if not edit:
            con.print("[vex.warn]cancelled â€” nothing ran[/]")
            return None
        return request + "\n\nUser-steered plan: " + edit
    con.print("[vex.warn]cancelled â€” nothing ran[/]")
    return None


def _build_agent_session_context(
    request: str,
    repo: Path,
    state: Dict[str, Any],
    log_root: Path,
    last: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the bounded conversation context for one agent request."""
    try:
        from cli.session import build_session_context

        conversation = state.get("conversation") if isinstance(state, dict) else None
        file_config = state.get("file_config") if isinstance(state, dict) else {}
        budget = int((file_config or {}).get("session_context_tokens", 12000))
        return build_session_context(
            conversation,
            repo=repo,
            task={"issue_text": request},
            prior_diff=str((last or {}).get("diff") or ""),
            token_budget=max(1, budget),
        )
    except Exception as exc:
        return {
            "text": "",
            "sources": [],
            "token_budget": 0,
            "estimated_tokens": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _mode_strategy(mode: str) -> str:
    """Map a product mode to a registered kernel strategy."""
    from cli.commands import normalize_mode

    selected = normalize_mode(mode, default="build")
    return {
        "plan": "planning",
        "build": "daily",
        "explore": "research",
        "review": "question",
        "debug": "daily",
        "ask": "question",
    }.get(selected, "daily")


def _mode_permission_rules(mode: str) -> List[Dict[str, str]]:
    """Return policy rules for a mode without weakening configured rules."""
    from cli.commands import mode_spec

    selected = mode_spec(mode)
    if selected is None:
        return []
    rules: List[Dict[str, str]] = []
    for side_effect in selected.denied_side_effects:
        rules.append({"action": "deny", "side_effect_class": side_effect})
    if selected.approval == "ask":
        for side_effect in ("workspace_write", "process", "network", "external"):
            rules.append({"action": "ask", "side_effect_class": side_effect})
    return rules


def _kernel_approval_callback(callback: Optional[Any]) -> Optional[Any]:
    """Adapt legacy approval callbacks to the kernel's typed callback."""
    if callback is None:
        return None

    def _callback(call: Any, decision: Any) -> Any:
        import inspect

        try:
            inspect.signature(callback).bind(call, decision)
        except (TypeError, ValueError):
            return callback(
                getattr(call, "tool", ""),
                dict(getattr(call, "arguments", {}) or {}),
                str(getattr(decision, "exact_effect", "") or ""),
            )
        return callback(call, decision)

    return _callback


def _run_one_mode(
    request: str,
    repo: Path,
    state: Dict[str, Any],
    log_root: Path,
    file_config: Optional[Dict[str, Any]] = None,
    *,
    mode: Optional[str] = None,
    task_id: Optional[str] = None,
    plan_guidance: Optional[str] = None,
    resume_history: Optional[str] = None,
    session_context: Optional[Any] = None,
    approve_fn: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Run one explicitly selected product mode through the authoritative kernel."""
    from cli.commands import (
        mode_config,
        mode_spec,
        normalize_mode,
        resolve_agent_approval,
    )
    from cli.vexconfig import apply_config_defaults, normalize_runtime_keys
    from harness.agent_kernel import (
        AgentKernel,
        CompletionPolicy,
        PolicyEngine,
        RunEventJournal,
        RunSpec,
    )
    from harness.agent_kernel.strategy import build_default_handlers
    from harness.agent_kernel.tools import ToolRegistry

    selected = normalize_mode(mode or str((state or {}).get("mode") or "build"), "build")
    profile = mode_spec(selected)
    if profile is None:
        return None
    tid = str(task_id or f"agent-{__import__('uuid').uuid4().hex[:8]}")
    strategy = _mode_strategy(selected)
    conversation = (state or {}).get("conversation") or {}
    session_id = str(conversation.get("session_id") or f"session-{tid}")
    config = normalize_runtime_keys(
        apply_config_defaults(
            {
                "model": (state or {}).get("model"),
                "provider": (state or {}).get("provider"),
            },
            file_config,
        )
    )
    config.update(mode_config(selected))
    config["agent_kernel_enabled"] = True
    config["agent_strategy"] = strategy
    config["permission_default"] = "allow"
    config["agent_approval"], _approval_reason = resolve_agent_approval(
        profile, config
    )
    # R2-15: resolve and PIN the boundary before the run, and show it. The pin
    # is what makes the receipt true -- the kernel reads the same mapping.
    trust = resolve_session_trust(
        config, repo=repo, log_root=log_root, session_id=session_id, task_id=tid
    )
    render_trust_banner(trust, _trust_ledger(), quiet=bool((state or {}).get("quiet")))
    existing_rules = config.get("permission_rules") or config.get("agent_permission_rules") or []
    if isinstance(existing_rules, dict):
        existing_rules = [existing_rules]
    config["permission_rules"] = list(existing_rules) + _mode_permission_rules(selected)
    if config["agent_approval"] == "require" and approve_fn is None:
        # Keyed on the RESOLVED value, not `profile.approval`. Those were the
        # same answer while `approval="ask"` happened to be the only writing
        # mode; after the gate became capability-derived they are not, and
        # gating on the declared word would leave a newly-gated mode with a
        # required gate and NO approver - which parks the run until its
        # timeout rather than showing a diff.
        import sys

        approve_fn = (
            _agent_approve_prompt
            if getattr(sys.stdin, "isatty", lambda: False)()
            else (lambda *_args: False)
        )
    completion = CompletionPolicy(config=config)
    registry = ToolRegistry()
    visible = {
        spec.name
        for spec in registry._specs.values()
        if _mode_tool_visible_for_profile(selected, spec.name)
    }
    registry.restrict(sorted(visible))
    build_default_handlers(
        registry,
        repo_path=str(repo),
        config=config,
        completion=completion,
    )
    policy = PolicyEngine(
        config["permission_rules"],
        session_id=session_id,
        default_action="allow",
        protected_paths=config.get("protected_paths", ()),
    )
    context_text = (
        str(session_context.get("text") or session_context.get("context") or "")
        if isinstance(session_context, Mapping)
        else str(session_context or "")
    )
    spec = RunSpec(
        session_id=session_id,
        run_id=tid,
        request=request,
        repository_identity=str(repo),
        strategy=strategy,
        verification_policy={
            key: config[key]
            for key in ("target_test", "test_command", "baseline_reruns", "verify_timeout_s")
            if key in config
        },
        metadata={
            "mode": selected,
            "agent_mode": selected,
            "session_context": context_text,
            "session_context_receipt": {
                "delivered": bool(context_text),
                "chars": len(context_text),
            },
            "plan_guidance": plan_guidance or "",
            "resume_history": resume_history or "",
        },
    )
    approval_receipts: List[Dict[str, Any]] = []
    raw_approval = _trusted_approver(
        _kernel_approval_callback(approve_fn) if approve_fn is not None else None
    )

    def _approval_callback(call: Any, decision: Any) -> Any:
        value = raw_approval(call, decision) if raw_approval is not None else None
        if isinstance(value, tuple) and len(value) == 2:
            approved, scope = bool(value[0]), str(value[1])
        elif isinstance(value, Mapping):
            approved, scope = bool(value.get("approved")), str(value.get("scope") or "once")
        else:
            approved, scope = bool(value), "once"
        if scope != "once":
            approval_receipts.append(
                {
                    "approved": approved,
                    "scope": scope,
                    "call_id": str(getattr(call, "call_id", "") or getattr(call, "id", "")),
                    "tool": str(getattr(call, "tool", "")),
                    "exact_effect": str(getattr(decision, "exact_effect", "") or ""),
                }
            )
        return value

    mon = LiveMonitor(tid, log_root).start(quiet=bool((state or {}).get("quiet")))
    _fire_task_start(tid)
    _set_live_run(tid, log_root)
    t0 = time.time()
    try:
        result = AgentKernel(
            repo_path=str(repo),
            log_root=Path(log_root),
            config=config,
            policy_engine=policy,
            tool_registry=registry,
            completion_policy=completion,
            approval_callback=_approval_callback if raw_approval is not None else None,
            strategy_options={
                "plan_guidance": plan_guidance or "",
                "resume_history": resume_history or "",
                "session_context": session_context,
            },
        ).run(spec, strategy=strategy, resume=bool(resume_history))
    except KeyboardInterrupt:
        record_session(log_root, tid, request, str(repo), "cancelled")
        raise
    except Exception as exc:
        mon.stop()
        _clear_live_run()
        from cli.errors import explain_exception

        explain_exception(exc, save_traceback=True)
        return None
    finally:
        _clear_live_run()
        mon.stop()
    if approval_receipts:
        try:
            journal = RunEventJournal(
                Path(log_root) / tid / "trace.jsonl",
                session_id=session_id,
                run_id=tid,
                turn_id="turn-1",
            )
            for receipt in approval_receipts:
                journal.append("approval_decided", receipt)
        except Exception:
            pass
    elapsed = round(time.time() - t0, 1)
    payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    status = str(payload.get("status") or "failed")
    answer = ui.strip_ansi(str(payload.get("answer") or ""))
    verification_evidence = payload.get("verification_evidence") or payload.get("verification") or []
    if isinstance(verification_evidence, Mapping):
        verification_evidence = [verification_evidence]
    from cli.runview import (
        effective_terminal_status,
        status_is_completed,
        status_is_verified,
        status_label,
    )

    display_status = effective_terminal_status(status, verification_evidence)
    display_label = status_label(display_status)
    verified = status_is_verified(display_status)
    completed = status_is_completed(display_status)
    result_style = "vex.ok" if verified else "vex.warn" if completed else "vex.error"
    result_mark = (
        ui.GLYPHS["ok"]
        if verified
        else ui.GLYPHS["wait"]
        if completed
        else ui.GLYPHS["fail"]
    )
    con = ui.console()
    con.print(
        f"[{result_style}]"
        f"{result_mark} {display_label}[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{len(payload.get('model_calls') or [])} model calls[/] "
        f"[vex.muted]{ui.DOT}[/] [vex.muted]{elapsed:.0f}s[/] "
        f"[vex.muted]{ui.DOT}[/] [vex.accent2]{ui.fmt_cost(payload.get('cost', 0.0))}[/]"
    )
    if answer:
        con.print()
        con.print(answer)
    if payload.get("diff"):
        con.print("[vex.muted]diff:[/]")
        ui.print_diff(str(payload["diff"]))
    con.print(
        f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]"
        f"{escape(str(payload.get('trace_path') or Path(log_root) / tid / 'trace.jsonl'))}[/]"
    )
    record_session(log_root, tid, request, str(repo), status)
    notify_done(status)
    return {
        "task_id": tid,
        "log_root": log_root,
        "mode": selected,
        "status": status,
        "kernel_status": status,
        "answer": answer,
        "diff": payload.get("diff") or "",
        "files_touched": list(payload.get("changed_files") or []),
        "cost_usd": float(payload.get("cost") or 0.0),
        "model_calls": list(payload.get("model_calls") or []),
        "elapsed_s": elapsed,
        "verification": (
            verification_evidence[-1] if verification_evidence else None
        ),
        "resume_availability": payload.get("resume_availability", ""),
        "session_context_receipt": {"delivered": bool(session_context)},
    }


def _mode_tool_visible_for_profile(mode: str, tool: str) -> bool:
    """Return whether a canonical kernel tool belongs to a product mode."""
    from cli.commands import mode_tool_visible

    return mode_tool_visible(mode, tool)


def _run_one_agent(
    request: str,
    repo: Path,
    state: Dict[str, Any],
    log_root: Path,
    file_config: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
    approve_fn_override: Optional[Any] = None,
    plan_guidance: Optional[str] = None,
    resume_history: Optional[str] = None,
    session_context: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Run one general agent task on the LIVE repo (the interactive engine).

    Drives harness.agent_loop.run_agent with live monitoring (the same
    LiveMonitor + _LIVE_RUN steering registration the fix path uses),
    renders the outcome (answer + diff + cost), and records the session.
    Edits happen IN PLACE on the live repo; the diff comes from the
    task's pristine reference. plan_guidance, when given, is injected
    as steering guidance (approved plan, not a contract).
    resume_history, when given, replays a resumed task's prior turns
    (history replay, not a restart). approve_fn_override lets the TUI supply its modal approver;
    otherwise agent_approval=require uses the REPL console prompt.
    Returns {"task_id", "log_root", "diff",
    "status", "answer"} for the session's /diff + /sessions commands.
    """
    import uuid as _uuid

    from cli.vexconfig import apply_config_defaults, normalize_runtime_keys
    from harness.agent_loop import run_agent

    con = ui.console()
    tid = task_id or f"agent-{_uuid.uuid4().hex[:8]}"
    if session_context is None:
        session_context = (state or {}).get("session_context")
    config = normalize_runtime_keys(
        apply_config_defaults(
            {
                k: v
                for k, v in {
                    "model": state.get("model"),
                    "provider": state.get("provider"),
                }.items()
                if v is not None
            },
            file_config,
        )
    )
    try:
        from cli.connectors import discover_mcp_servers

        config["mcp_server_resolver"] = lambda label: (
            discover_mcp_servers(str(repo)).get(str(label), {}).get("command")
        )
        config["mcp_server_labels_resolver"] = lambda: discover_mcp_servers(str(repo))
    except Exception:
        pass
    repo_name = Path(repo).name
    con.print(
        f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]working in[/] "
        f"[vex.accent]{repo_name}[/] [vex.muted]{ui.DOT} "
        f"{ui.GLYPHS['wait']} {tid}[/]"
    )
    # R2-15: resolve and PIN the daily boundary before the first model call, and
    # show it. `run_agent` dispatches to the kernel's `daily` strategy, whose
    # shell handler reads `agent_process_sandboxed` from THIS mapping -- so
    # pinning here is what makes the printed receipt true rather than hopeful.
    trust = resolve_session_trust(
        config,
        repo=repo,
        log_root=log_root,
        session_id=str(((state or {}).get("conversation") or {}).get("session_id") or ""),
        task_id=tid,
    )
    render_trust_banner(trust, _trust_ledger(), quiet=bool((state or {}).get("quiet")))
    from cli.main import _clear_router_context, _set_router_context

    _set_router_context(_RouterCtx(config))
    mon = LiveMonitor(tid, log_root).start(quiet=bool((state or {}).get("quiet")))
    _fire_task_start(tid)
    _set_live_run(tid, log_root)
    if approve_fn_override is not None:
        approve = _trusted_approver(approve_fn_override)
    else:
        approve = (
            _trusted_approver(_agent_approve_prompt)
            if str(config.get("agent_approval", "auto")).lower() == "require"
            else None
        )
    t0 = time.time()
    try:
        out = run_agent(
            request=request,
            repo_path=str(repo),
            config=config,
            log_root=log_root,
            task_id=tid,
            approve_fn=approve,
            plan_guidance=plan_guidance,
            resume_history=resume_history,
            session_context=session_context,
            session_id=(state or {}).get("conversation", {}).get("session_id")
            if isinstance((state or {}).get("conversation"), dict)
            else None,
        )
    except KeyboardInterrupt:
        record_session(log_root, tid, request, str(repo), "interrupted")
        raise
    except Exception as exc:
        mon.stop()
        from cli.errors import explain_exception

        explain_exception(exc, save_traceback=True)
        return None
    finally:
        _clear_live_run()
        _clear_router_context()
        mon.stop()
    elapsed = time.time() - t0

    status = str(out.get("status") or "failed")
    verification_evidence = out.get("verification") or out.get("verification_evidence") or []
    if isinstance(verification_evidence, Mapping):
        verification_evidence = [verification_evidence]
    from cli.runview import (
        effective_terminal_status,
        status_is_completed,
        status_is_verified,
        status_label,
    )

    display_status = effective_terminal_status(status, verification_evidence)
    display_label = status_label(display_status)
    verified = status_is_verified(display_status)
    completed = status_is_completed(display_status)
    style = "vex.ok" if verified else "vex.warn" if completed else "vex.error"
    mark = (
        ui.GLYPHS["ok"]
        if verified
        else ui.GLYPHS["wait"]
        if completed
        else ui.GLYPHS["fail"]
    )
    con.print()
    con.print(
        f"[{style}]{mark} {display_label}[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{len(out.get('model_calls', []))} model calls[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{elapsed:.0f}s[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.accent2]{ui.fmt_cost(out.get('cost_usd', 0.0))}[/]"
    )
    if out.get("answer"):
        con.print()
        con.print(ui.strip_ansi(str(out["answer"])))
    if out.get("diff"):
        con.print("[vex.muted]diff:[/]")
        ui.print_diff(str(out["diff"]))
    else:
        con.print("[vex.muted]no file changes[/]")
    con.print(
        f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]{Path(log_root).resolve() / tid / 'trace.jsonl'}[/]"
    )
    record_session(log_root, tid, request, str(repo), str(out.get("status", "?")))
    notify_done(str(out.get("status", "")))
    return {
        "task_id": tid,
        "log_root": log_root,
        "diff": out.get("diff"),
        "status": out.get("status"),
        "answer": out.get("answer"),
        "mode": "agent_task",
        "cost_usd": float(out.get("cost_usd") or 0.0),
        "model_calls": list(out.get("model_calls") or []),
        "elapsed_s": round(elapsed, 1),
        "files_touched": list(out.get("files_touched") or []),
        "verification": out.get("verification"),
        "session_context_receipt": {
            "delivered": bool((session_context or {}).get("text"))
            if isinstance(session_context, dict)
            else bool(session_context),
            "chars": len(str((session_context or {}).get("text") or ""))
            if isinstance(session_context, dict)
            else len(str(session_context or "")),
        },
    }


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

    from cli.vexconfig import apply_config_defaults, normalize_runtime_keys
    from shared.types import Task

    task_id = f"fix-{uuid.uuid4().hex[:8]}"
    # Session-explicit values win (None = unset, falls to the settings
    # chain); apply_config_defaults fills gaps from the two-tier files +
    # VEX_* env. base_url normalizes onto runtime's api_base (Task B) so
    # any OpenAI-compatible router works with any model name.
    config = normalize_runtime_keys(
        apply_config_defaults(
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
    )
    task = Task(task_id=task_id, repo_path=str(repo), issue_text=issue, config=config)
    try:
        result_info = _execute_task(
            task,
            log_root,
            preview=bool(config.get("plan_preview")),
            issue_for_index=issue,
            state=state,
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
    state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Shared run-and-report core for new fixes AND resumed runs.

    Live-monitors the trace, renders the result summary (status/cost/
    verification chips/diff/rationale), and applies the plan preview
    (Task D) when `preview` is set: a watcher thread renders the plan
    event (the harness's public decomposition) and prompts BEFORE any
    edit can happen â€” approval continues the run, rejection cancels it
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
        f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]fixing in[/] "
        f"[vex.accent]{repo_name}[/] [vex.muted]{ui.DOT} "
        f"{ui.GLYPHS['wait']} {task_id}[/]"
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
    mon = LiveMonitor(task_id, log_root).start(quiet=bool((state or {}).get("quiet")))
    _fire_task_start(task_id)  # embedded-UI hook: attach the live run-line
    # Steering round, Task A: register the run as LIVE so the reader
    # thread routes typed lines into logs/{task_id}/steering.jsonl
    # (via steer_live_run) instead of queueing them invisibly. Cleared
    # on every exit path â€” a leaked registration would silently steer
    # dead air (the reader's inject is refused by the fresh buffer's
    # rebuild semantics anyway, but the acks would lie).
    _set_live_run(task_id, log_root)
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
        _clear_live_run()
        _clear_router_context()
        mon.stop()
        if preview_thread is not None:
            preview_cancel.set()
            preview_thread.join(timeout=2.0)
    elapsed = time.time() - t0

    display_status, style, mark = _terminal_result_display(
        result.status, result.verification
    )
    con.print()
    con.print(
        f"[{style}]{mark} {display_status}[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{result.attempts} attempt(s)[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{len(result.model_calls)} model calls[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{elapsed:.0f}s[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.accent2]{ui.fmt_cost(result.cost_usd)}[/]"
    )
    if result.verification is not None:
        v = result.verification
        t_style = "vex.ok" if v.target_test_passed else "vex.error"
        r_style = "vex.ok" if v.regression_passed else "vex.error"
        con.print(
            f"  [{t_style}]{'PASS' if v.target_test_passed else 'FAIL'} target[/]"
            f"[vex.muted] {ui.DOT} [/]"
            f"[{r_style}]{'PASS' if v.regression_passed else 'FAIL'} regression[/]"
            f"[vex.muted] {ui.DOT} flaky: {v.flaky}[/]"
        )
    if result.diff:
        con.print("[vex.muted]diff:[/]")
        ui.print_diff(result.diff)
    # rationale.md (Task E): render the grounded paragraph if written
    rat = Path(log_root) / task_id / "rationale.md"
    if rat.is_file():
        from rich.markdown import Markdown

        con.print()
        con.print(Markdown(ui.strip_ansi(rat.read_text(encoding="utf-8"))))
    con.print(
        f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]{Path(log_root).resolve() / task_id / 'trace.jsonl'}[/]"
    )
    # R2-17 (item 6): the recovery card, in the surface where the run
    # failed — what failed, why, and what to do next.
    print_run_recovery(
        con,
        log_root,
        task_id,
        result.status,
        note=str(getattr(result, "note", "") or ""),
    )
    con.print(
        "[vex.muted]type[/] [vex.accent]status[/] [vex.muted]for the plan "
        "checklist,[/] [vex.accent]diff[/] [vex.muted]to re-render[/]"
    )
    notify_done(result.status)
    return {
        "task_id": task_id,
        "log_root": log_root,
        "diff": result.diff,
        "status": result.status,
        "mode": "fix",
        "attempts": result.attempts,
        "cost_usd": result.cost_usd,
        "model_calls": list(result.model_calls),
        "elapsed_s": round(elapsed, 1),
        "verification": result.verification,
    }


def _trace_feed_command(arg, last: Dict[str, Any], log_root: Path) -> None:
    """/trace in the rich REPL: print the last (or live) run's feed â€”
    the readable one-line-per-action view built from the SAME trace
    file the run writes (Task E: no second logging path). No arg lists
    entries; /trace <n> prints entry n's full detail inline (the REPL
    has no modal â€” plain print is its idiom)."""
    from cli import tracelog as tl

    con = ui.console()
    tid = last.get("task_id")
    if not tid:
        say_empty_state(con.print, "no_runs")
        return
    feed = tl.FeedBuilder(tid)
    trace_file = Path(log_root) / tid / "trace.jsonl"
    if trace_file.is_file():
        try:
            for ln in trace_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                try:
                    ev = json.loads(ln)
                except ValueError:
                    continue
                feed.consume(ev)
        except OSError:
            pass
    entries = feed.entries
    if not entries:
        con.print("[vex.muted]no feed entries yet[/]")
        return
    if arg is None:
        con.print(f"[vex.accent]trace feed[/] [vex.muted]({len(entries)} entries)[/]")
        for ent in entries[-60:]:
            con.print(
                f"[vex.muted]{ent.index:>2}[/] [{ui.TEXT_PRIMARY}]{ent.summary}[/]"
            )
        return
    try:
        n = int(arg)
    except ValueError:
        con.print("[vex.error]usage: /trace [n][/][vex.muted] â€” an entry number[/]")
        return
    match = next((e for e in entries if e.index == n), None)
    if match is None:
        con.print(
            f"[vex.error]no feed entry {n}[/] [vex.muted](0â€“{entries[-1].index})[/]"
        )
        return
    ui.rule(f"[vex.accent]trace {n} â€” {match.detail_title or match.category}[/]")
    con.print(f"[{ui.TEXT_PRIMARY}]{match.summary}[/]")
    detail = (match.detail or "").strip() or "(no detail recorded)"
    con.print(ui.strip_ansi(detail))


def _plan_preview_watch(
    log_root: Path, task_id: str, cancel: threading.Event, poll_s: float = 0.25
) -> None:
    """Task D: render the plan and ask before edits start.

    Watches the task's trace for the FIRST `plan` event (the harness's
    public decomposition: [{id, description, checkpoint}, ...]), renders
    it as a numbered list, and prompts. approve -> return (run proceeds
    into the edit phase); reject/timeout -> raise KeyboardInterrupt into
    the blocking run via the main thread's interrupt â€” simplest reliable
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
    ui.rule(f"[vex.accent]plan preview â€” {task_id}[/]")
    body_lines: List[str] = []
    for step in plan:
        sid = step.get("id", "?")
        desc = step.get("description", "")
        checkpoint = step.get("checkpoint", "")
        line = f"  [vex.accent]{sid}.[/] {desc}"
        if checkpoint:
            line += f" [vex.muted](done when: {checkpoint})[/]"
        con.print(line)
        body_lines.append(
            f"{sid}. {desc}" + (f" (done when: {checkpoint})" if checkpoint else "")
        )
    _fire_prompt_body("run this plan? [Y/n] ", body_lines)
    try:
        answer = input("run this plan? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("", "y", "yes"):
        con.print("[vex.ok]approved â€” starting edits[/]")
        return
    con.print(
        "[vex.warn]rejected â€” cancelling (checkpoints kept; "
        "`vex --continue` resumes this task)[/]"
    )
    # cancel the blocking run_task: interrupt the MAIN thread the same
    # way Ctrl+C does â€” run_task's KeyboardInterrupt path keeps state.
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
    """Deliver Ctrl+C semantics to the run (plan rejection, /cancel).

    Inside the full-screen TUI (cli.tui): the run blocks a WORKER
    thread while the main thread runs textual's event loop â€” SIGINT at
    main would kill the app, so the _CANCEL_RUN hook (set by the app)
    injects the KeyboardInterrupt into the worker instead (same
    semantics downstream: containers stop, checkpoints stay, the task
    is resumable via `vex --continue`).

    In the rich REPL / flag commands: signal.raise_signal(SIGINT)
    reaches the MAIN thread's interrupt handling (Python only runs
    signal handlers on the main thread) â€” exactly what a human pressing
    Ctrl+C does. run_task's KeyboardInterrupt path then fires.
    On any failure to signal, we degrade gracefully: the run is left
    to finish normally (the user was warned by the reject message).
    """
    import signal

    if _CANCEL_RUN is not None:
        try:
            _CANCEL_RUN()
            return
        except Exception:
            pass  # fall through to the SIGINT path
    try:
        signal.raise_signal(signal.SIGINT)
    except (ValueError, OSError, AttributeError):
        pass
