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
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.markup import escape

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
    "steering": "steered: applying your instruction",
    "steering_abort": "steered: aborting at the next checkpoint",
    "steering_replan": "steered: re-planning around your instruction",
    "steering_step_yield": "steered: yielding step to the task level",
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

# Embedded-UI hooks (full-screen TUI round, 2026-09-13). cli.tui sets
# these at app mount; the rich REPL leaves them None. They let the TUI
# (a) attach its live run-line the moment a task id exists (before any
# trace event can land) and (b) redirect /cancel's interrupt at the
# TUI's worker thread instead of SIGINT-at-main (in the TUI, the main
# thread is textual's event loop — SIGINT there would kill the app).
# Both are optional callables; firing them is wrapped in try/except so
# a broken UI hook can never take a run down.
_ON_TASK_START: Optional[Any] = None  # callable(task_id: str) -> None
_CANCEL_RUN: Optional[Any] = None  # callable() -> None (no args)
# Fired right before a blocking prompt (plan preview, approval gate):
# callable(prompt: str, body_lines: List[str]) -> None. The embedded UI
# (cli.tui) uses it to render the prompt's CONTEXT (the plan steps / the
# diff) in its modal body — the plain REPL leaves it unset, and the
# console prints below remain the source of truth for the transcript.
_PROMPT_BODY: Optional[Any] = None

# ---------------------------------------------------------------------------
# Mid-task steering (steering round) — live-run registration + the REPL's
# stdin-owning reader thread
# ---------------------------------------------------------------------------

# The CURRENTLY-RUNNING harness task (fix/build/resume — the long,
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
) -> Optional[str]:
    """Inject one mid-run user line as steering into the LIVE task.

    Returns an ack string for the user, or None when refused:
    - conversational input (cli.intent: greetings/chit-chat) is NEVER
      injected — steering must not pollute a task with small talk;
      the ack says so (cost asymmetry: a wrong injection burns a
      verifier cycle, a skipped one costs one line).
    - steering disabled in the merged config -> honest refusal.
    - the buffer's pending cap -> honest refusal (inject() contract).

    `say` (optional) renders the ack lines — the REPL passes nothing
    (console print, its idiom); the TUI passes a transcript renderer
    (its console output is captured away from the user, so the ack
    must land in the transcript instead). Assumes task_id/log_root
    identify the run currently inside run_task (the harness's
    SteeringBuffer for that task lives at log_root/task_id/
    steering.jsonl — same journal the loop polls).
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
    # instruction silently lost — refuse honestly instead. A RESUMED
    # run keeps its prior trace (the dir is never archived on resume),
    # so steering right after a resume correctly passes this gate and
    # the loop's freshly constructed buffer replays it.
    if not (Path(log_root) / task_id / "trace.jsonl").is_file():
        _say(
            "[vex.muted]the run is still starting (before the fix loop) "
            "— steering applies once the loop is live; send it again in "
            "a moment[/]"
        )
        return "starting"

    buf = steering_mod.SteeringBuffer(Path(log_root) / task_id, task_id)
    intent, text = steering_mod.parse_steering_line(line)
    ev = buf.inject(text, intent=intent, source=source)
    if ev is None:
        _say(
            "[vex.warn]steering refused[/] [vex.muted]— the task's steering "
            "queue is full or steering is disabled for this run[/]"
        )
        return "refused"
    if ev.intent == "abort":
        _say(
            f"[vex.warn]abort requested[/] [vex.muted]({ev.seq}) — the task "
            "stops cleanly at its next checkpoint (resumable via "
            "[vex.accent]vex --resume[/][vex.muted])[/]"
        )
    elif ev.intent == "replan":
        _say(
            f"[vex.ok]steered (re-plan)[/] [vex.muted]({ev.seq}) — the task "
            "re-plans at the next step boundary, keeping work done so far[/]"
        )
    else:
        _say(
            f"[vex.ok]steered[/] [vex.muted]({ev.seq}) — applies at the next "
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
    must pass a LIVE thread's ident (idents get recycled — check
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

    Before steering, the REPL's main thread called input() directly —
    so while a run was live NOTHING could be typed (mid-task steering
    impossible from the REPL without this restructure). One daemon
    thread owns stdin for the whole session and hands each line to
    the main loop via a queue; while a harness run is live, lines are
    consumed HERE instead (steering injected into the live task,
    slash commands dispatched, conversational input answered inline).

    Ctrl+C semantics are preserved: SIGINT is delivered to the MAIN
    thread (inside the blocking run_task — its KeyboardInterrupt path
    keeps checkpoints); the reader's own blocked input() aborts on
    Windows too, which the reader swallows when a run is live (the
    interrupt was for the run; the session continues) and treats as
    leave when idle (the previous at-prompt behavior).
    """

    def __init__(self, main_ident: int) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._main_ident = main_ident
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

        # Slash commands work mid-run (same set the TUI allows).
        if low.startswith("/"):
            cmd = low.split()[0]
            if cmd == "/cancel":
                con = ui.console()
                con.print(
                    "[vex.warn]cancel requested — sending Ctrl+C semantics "
                    "to the running task (checkpoints kept)[/]"
                )
                if not inject_async_interrupt(self._main_ident):
                    con.print(
                        "[vex.warn](main thread unreachable — press Ctrl+C again)[/]"
                    )
                return True
            if cmd in (
                "/status",
                "/diff",
                "/sessions",
                "/trace",
                "/feed",
                "/help",
                "/cost",
                "/skills",
                "/history",
                "/model",
                "/mcp",
            ):
                # render-only commands answer inline (best-effort from
                # this thread; they read the public log files only).
                # Mutating newcomers (/init /login /logout /clear /undo,
                # /model with a name, /mcp with a label) fall through to
                # the honest in-flight warning below — applied when idle.
                if cmd == "/model" and len(line.split(None, 1)) > 1:
                    pass  # pinning waits for idle -> warning below
                elif cmd == "/mcp" and len(line.split(None, 1)) > 1:
                    pass  # server spawn waits for idle -> warning below
                else:
                    _reader_slash_render(cmd, live, line)
                    return True
            if cmd in ("/approve", "/reject"):
                con = ui.console()
                decided = _decide_pending(
                    Path(live["log_root"]),
                    live["task_id"],
                    approve=(cmd == "/approve"),
                )
                if decided is None:
                    con.print(
                        f"[vex.muted]no pending approval request for "
                        f"{live['task_id']}[/]"
                    )
                return True
            if cmd == "/quiet":
                _reader_toggle_quiet()
                return True
            if cmd == "/steer":
                body = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
                if not body.strip():
                    ui.console().print(
                        "[vex.muted]usage: /steer <instruction> (plain text "
                        "while a run is live does the same)[/]"
                    )
                    return True
                steer_live_run(body, live["task_id"], Path(live["log_root"]))
                return True
            # /resume + custom commands mid-run: explain (explicit
            # new-task requests — never silently swallowed)
            ui.console().print(
                "[vex.warn]a run is in flight — /cancel it first, or retype "
                "when it finishes (plain text STEERS the live run)[/]"
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
# session-scoped flag in `state`, and the reader has no state ref —
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
    read the PUBLIC log files only (state.json/trace.jsonl — never
    run_task internals); any failure prints one honest line, never
    raises. `line` carries the raw input so /sessions and /feed can
    take a filter query mid-run.
    """
    con = ui.console()
    arg = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
    try:
        log_root = Path(live["log_root"])
        if cmd == "/status":
            data = None
            sf = log_root / live["task_id"] / "state.json"
            if sf.is_file():
                data = json.loads(sf.read_text(encoding="utf-8"))
            if not data:
                con.print(
                    f"[vex.muted]{live['task_id']}: no state yet (early phase)[/]"
                )
                return
            done = len(data.get("completed_steps") or [])
            total = len(data.get("plan") or [])
            con.print(
                f"[vex.accent]{live['task_id']}[/] [vex.muted]{done}/{total} "
                f"steps[/] [vex.muted]{ui.DOT}[/] "
                f"[vex.running]{data.get('phase', 'running')}[/]"
            )
            for f in (data.get("files_touched") or [])[:8]:
                con.print(f"  [vex.muted]{ui.GLYPHS['bullet']} {f}[/]")
        elif cmd == "/diff":
            from cli.tracelog import live_diff

            lines = live_diff(
                log_root / live["task_id"] / "pristine",
                log_root / live["task_id"] / "work",
                max_lines=20,
            )
            if not lines:
                con.print("[vex.muted]no diff yet[/]")
                return
            for text in ui.diff_render_lines(lines):
                con.print(text)
        elif cmd in ("/sessions", "/feed", "/trace"):
            if cmd == "/sessions":
                _print_sessions(con, log_root, arg)
                return
            _print_feed(con, log_root, live["task_id"], arg)
        elif cmd == "/help":
            con.print(_HELP)
        elif cmd == "/cost":
            _render_cost({"task_id": live["task_id"]}, log_root)
        elif cmd == "/skills":
            _render_skills(Path.cwd())
        elif cmd == "/mcp":
            try:
                from cli.vexconfig import merged_settings as _merged

                _mcfg: Optional[Dict[str, Any]] = _merged()
            except Exception:
                _mcfg = None
            _render_mcp(_mcfg)
        elif cmd == "/history":
            con.print("[vex.muted]history is unavailable mid-run — retype when idle[/]")
        elif cmd == "/model":
            try:
                from cli.onboard import format_model_display as _fmt_model
                from cli.vexconfig import merged_settings as _merged2

                con.print(f"[vex.muted]{_fmt_model(None, _merged2())}[/]")
            except Exception:
                con.print("[vex.muted](model unavailable mid-run)[/]")
    except Exception as exc:  # render-only; never raise from the reader
        con.print(f"[vex.muted]({cmd.strip('/')} unavailable: {exc})[/]")


def _print_sessions(con: Any, log_root: Path, query: str = "") -> None:
    """Print the (optionally filtered) session list — the plain-echo
    idiom of the reader thread (the TUI has the searchable browser; the
    REPL gets the same filter grammar via /sessions <query>)."""
    sessions = search_sessions(log_root, query, limit=40)
    if not sessions:
        con.print(
            "[vex.muted]no sessions match[/]"
            if query
            else "[vex.muted]no recorded sessions yet[/]"
        )
        return
    head = "[vex.accent]recent sessions[/]"
    if query:
        head += f" [vex.muted]matching {query!r}[/]"
    con.print(head + " [vex.muted]([vex.running]R[vex.muted] = resumable)[/]")
    for s in sessions[:12]:
        mark = "[vex.running]R[/]" if s.get("resumable") else " "
        con.print(
            f"  {mark} [vex.accent]{s.get('task_id', '?')}[/] "
            f"[vex.muted]{s.get('status', '?'):8} "
            f"{str(s.get('issue') or '')[:50]}[/]"
        )


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
    contract with the TUI, cli.tui.feed_style — the italic-dim vs
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
    "final verification — target + full suite pass",
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
        """Begin rendering (quiet=True: track but don't render — for tests)."""
        self._quiet = quiet
        if not quiet:
            self._status = self._console.status(
                f"[vex.running]{self._last_label}[/]", spinner=ui.SPINNER
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
        kind = obj.get("kind")
        data = obj.get("data") or {}
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
            bits = f"[vex.running]{self._last_label}[/]"
            # While the model thinks, add the rotating tech joke
            # (Qwen-Code-style flavor — same behavior as the TUI run-line).
            if self._thinking:
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

#: Ring the completion bell from the REPL's run paths (Task F,
#: interaction-polish round). The full-screen TUI owns its OWN ring
#: (VexApp._finish_run) and disables this one on mount so a TUI fix —
#: which reuses _execute_task — never double-beeps. VEX_NOTIFY=0 (ui.
#: bell) silences both for CI / ssh / audio-free machines.
NOTIFY = True


def notify_done(status: str = "") -> None:
    """Best-effort 'task finished' signal for a run that completed on
    the REPL path. Reads the outcome into the message but NEVER raises
    (a UI nicety must not take down a verified fix's reporting).

    When the full-screen TUI is mounted (`_ON_TASK_START` is its hook),
    the REPL-side ring is skipped: the TUI rings its own at the true end
    of the run (`VexApp._finish_run`, which also covers the question /
    research modes) — one bell per finished task, never two."""
    if not NOTIFY or _ON_TASK_START is not None:
        return
    try:
        label = "done" if status == "success" else (status or "finished")
        ui.bell(f"vex task {label}")
    except Exception:
        pass


def session_store_path(log_root: Path) -> Path:
    """logs/.vex-sessions.jsonl — the interactive session index."""
    return Path(log_root) / ".vex-sessions.jsonl"


def record_session(
    log_root: Path, task_id: str, issue: str, repo: str, status: str
) -> None:
    """Append one completed/attempted interactive run to the session index.

    Never raises: the index is an enhancement — a failed append must not
    take down a verified fix's reporting. Also ingests the run's facts
    into memory (memory-first: Boundary-4 poll + one session row) so
    future sessions recall this one with no manual `vex memory` call.
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
    try:
        from cli.session import ingest_session_facts

        ingest_session_facts(log_root, task_id, issue, repo, status)
    except Exception:
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
    """The sessions satisfying `query` (order preserved — newest first
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


def _resume_task(
    task_id: str, log_root: Path, state: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Continue a previous interactive run by task id (Task A).

    Rebuilds the Task from the run's own trace (task_start carries
    repo_path/issue_text/config verbatim — the harness's public
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
        return None
    if data.get("mode") == "agent":
        # Agent sessions edit the live repo; "resume" continues the same
        # task id (pristine/orig references are kept, the loop starts
        # from the current tree with the prior turns replayed as
        # resume history — history replay, not a restart).
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
    the persistent conversation — the "keeps talking" half of resume.
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
# ^ superseded by the block wordmark + splash/compact split (branding
# round, Task C) — kept one release for any external script that
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
    wordmark splash ONCE — first launch into an empty session
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
    else:
        con.print()
        ui.print_compact_header(repo=repo, model=model, version=_get_version())
        con.print(
            f"[vex.muted]logs[/] [{ui.TEXT_PRIMARY}]{Path(log_root).resolve()}[/] "
            f"[vex.muted]{ui.DOT} plain language just works[/]"
        )
        con.print()


_HELP = """[vex.accent]what you can say[/]
  [vex.muted]<anything>[/]          just say it — explain, change, run, or debug
  [vex.accent]/steer <text>[/]      steer the RUNNING task (also: plain text while a run is live;
                                    "replan: …" replaces the plan; "abort" stops cleanly)
  [vex.accent]/status[/]            current/last task's structured state
  [vex.accent]/trace[/]             live action feed (TUI: /trace <n> expands an entry)
  [vex.accent]/feed <text>[/]       the whole trace feed as a scrollable, searchable history
                                    (TUI opens a browser; type to filter; enter expands one)
  [vex.accent]/diff[/]              re-render the last run's diff (syntax-highlighted)
  [vex.accent]/diff undo[/]         undo the last agent edit ([vex.muted]/diff undo all[/] reverts all)
  [vex.accent]/sessions <query>[/]  search previous sessions — filters: [vex.muted]status:failed[/]
                                    [vex.muted]repo:name[/] [vex.muted]since:YYYY-MM-DD[/]
                                    [vex.muted]resumable[/][vex.muted] + free text (TUI: a browser)[/]
  [vex.accent]/resume[/] [vex.muted][<task_id>][/]  continue an interrupted task
                                     (no id = most recent resumable)
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
  [vex.accent]/undo[/] [vex.muted][<file>|all][/]  alias of /diff undo (agent sessions)
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


def _is_first_launch(log_root: Path) -> bool:
    """True when no interactive run has EVER been recorded under this
    log root (no session index, no resumable task dirs) — the state
    that earns the wordmark splash (Task C). Cheap: two stat calls."""
    root = Path(log_root)
    if session_store_path(root).is_file():
        return False
    try:
        for d in root.iterdir():
            if d.is_dir() and not d.name.startswith((".", "_")):
                if (d / "trace.jsonl").is_file():
                    return False
    except OSError:
        pass
    return True


def run_interactive(argv_quote: str = "", log_root: Optional[Path] = None) -> int:
    """The `vex` no-args session. Returns a process exit code (0/130).

    Assumes stdin is an interactive terminal (the CLI dispatches here only
    when sys.stdin.isatty() and no subcommand was given; scripted callers
    use the flag commands).
    """
    con = ui.console()
    repo = _detect_repo()
    log_root = log_root or Path("logs")
    # Plugins round (Task C): installed plugins' tool verbs extend the
    # BATCH read-only allowlist for this session (best-effort, never
    # raises — a broken plugin is skipped by the loader).
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
        # the session's repo, kept in state so slash-command handlers
        # (custom commands run fixes) see the CURRENT repo without a
        # new parameter threading through every _slash_command caller
        "repo": str(repo),
        # config-file defaults for custom-command-driven fixes (they
        # call _run_one_fix with file_config=None — the session's own
        # load applies, same as plain-language fixes)
        "file_config": None,
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
            "— `vex config list` to see what's set[/]"
        )
    # First-`vex`-in-a-repo: scaffold <repo>/.vex/ (settings.toml +
    # settings.local.toml + commands/ + skills/ examples) when inside a
    # git repo. Never overwrites, never outside a repo, never raises.
    scaffold = maybe_scaffold_repo()
    if scaffold and scaffold.get("created"):
        con.print(
            "[vex.ok]repo setup:[/] [vex.muted]created "
            + ", ".join(f".vex/{c}" for c in scaffold["created"])
            + " — `vex config list` shows the chain[/]"
        )
    # Two-tier chain (global + project .vex/settings{,.local}.toml);
    # env vars are applied per-fix by apply_config_defaults so explicit
    # session pins still win.
    file_config = merged_settings()
    # First-run onboarding: no usable model/auth anywhere in the chain
    # -> offer the inline wizard ONCE here (skippable via /skip,
    # VEX_NO_ONBOARD=1; never prompts without a TTY — pipe-safe).
    try:
        from cli.onboard import maybe_onboard_repl

        file_config = maybe_onboard_repl(file_config) or {}
    except Exception:
        pass
    state["file_config"] = file_config
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

        conversation = _session_mod.load_or_create(log_root, repo)
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

    # Steering round, Task A: the reader thread owns stdin for the
    # whole session so lines typed WHILE a harness run is live can
    # STEER it (injected into the run's steering journal; consumed at
    # the loop's safe checkpoints) instead of being invisible until the
    # run finishes. Idle lines flow to this loop unchanged; a console
    # Ctrl+C while a run is live is delivered at the MAIN thread (inside
    # run_task — its KI path keeps checkpoints), while idle it stays the
    # leave behavior. The prompt still renders here (the reader never
    # prints it — a blocked run may need the console).
    _init_readline_history(log_root)
    reader = _ReplReader(threading.get_ident())
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
            if handled == "continue":
                continue
            if handled is None:
                # /clear swaps state["conversation"] for a fresh file —
                # the loop's cached handle must follow it (the old file
                # stays on disk; subsequent turns join the new one).
                if isinstance(state.get("conversation"), dict):
                    conversation = state["conversation"]
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
        # line is classified — a mention is conversation context, and
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
            except Exception:
                pass

        # -- agent dispatch: classify the line (deterministic rules
        # first; ONE cheap model call for the gray zone) into question
        # (read-only answer), agent_task (ONE tool loop for fix/build/
        # refactor/run/debug — not 4 modes), or chit_chat (inline reply,
        # nothing launched). `vex fix` (the flag command) still drives
        # harness.core.run_task directly — this dispatch is only the
        # interactive `vex` session engine. The cost asymmetry stands: a
        # wrong run burns minutes + model budget, a question costs one
        # line — so unsure input asks instead of launching.
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
            result_info = _run_one_agent(line, repo, state, log_root, file_config)
        except KeyboardInterrupt:
            con.print(
                "\n[vex.warn]interrupted — partial edits stay in the repo; "
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
    """
    con = ui.console()
    diff = (last or {}).get("diff")
    if diff:
        con.print("[vex.muted]diff:[/]")
        try:
            ui.print_diff(str(diff))
        except Exception:
            con.print(str(diff)[:4000])
    else:
        con.print("[vex.muted]no diff from the last run[/]")
    tid = (last or {}).get("task_id")
    body = last_rationale_text(log_root, tid) if tid else None
    if body:
        con.print()
        con.print("[vex.muted]rationale:[/]")
        try:
            from rich.markdown import Markdown

            con.print(Markdown(body))
        except Exception:
            con.print(body[:4000])
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
# Shared slash helpers (surface-wiring round) — one implementation serves
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
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("kind") != "model_response":
                continue
            try:
                usage = (obj.get("data") or {}).get("usage") or {}
            except Exception:
                continue
            try:
                calls += 1
                tokens += int(usage.get("tokens") or 0)
                cost += float(usage.get("cost") or 0.0)
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


def _render_cost(
    last: Dict[str, Any], log_root: Path, say: Optional[Any] = None
) -> None:
    """`/cost`: last run + session total, both from trace usage-sums.
    Assumes last is the session's last-run dict (may be empty) and
    log_root the logs dir. Never raises."""
    say = say or _slash_say_default
    try:
        tid = (last or {}).get("task_id")
        if tid:
            c, t, m = trace_usage_sum(Path(log_root) / str(tid) / "trace.jsonl")
            say(
                f"[vex.accent]last run[/] [vex.muted]{tid} — {c} model call(s) "
                f"· {t:,} tokens · [/][vex.accent2]{ui.fmt_cost(m)}[/]"
            )
        else:
            say("[vex.muted]no run in this session yet[/]")
        c, t, m = session_spend_total(log_root)
        say(
            f"[vex.accent]session total[/] [vex.muted]{c} model call(s) "
            f"· {t:,} tokens · [/][vex.accent2]{ui.fmt_cost(m)}[/]"
        )
    except Exception as exc:
        say(f"[vex.muted](cost unavailable: {type(exc).__name__})[/]")


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
            head += f" [vex.muted]matching {filt.strip()!r}[/]"
        say(head)
        for s in skills:
            name = str(getattr(s, "name", "?"))
            origin = str(getattr(s, "origin", "?"))
            desc = str(getattr(s, "description", "") or "").replace("\n", " ")
            say(
                f"  [vex.accent]/{name}[/] [vex.muted]({origin})[/]"
                + (f" [vex.muted]{desc[:100]}[/]" if desc else "")
            )
    except Exception as exc:
        say(f"[vex.muted](skills unavailable: {type(exc).__name__})[/]")


def mcp_server_table(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Configured MCP servers as [{label, command, source}]. Sources: the
    config's agent_mcp_servers map, then installed plugins' mcp_servers
    maps (the same two sources the agent loop resolves). Never raises."""
    out: List[Dict[str, str]] = []
    try:
        cfg_map = (cfg or {}).get("agent_mcp_servers") or {}
        if isinstance(cfg_map, dict):
            for k, v in cfg_map.items():
                out.append({"label": str(k), "command": str(v), "source": "config"})
    except Exception:
        pass
    try:
        from cli import plugins as plugins_mod

        for entry in plugins_mod.list_plugins():
            try:
                if entry.get("error"):
                    continue
                servers = entry.get("mcp_servers") or {}
                if isinstance(servers, dict):
                    for k, v in servers.items():
                        out.append(
                            {
                                "label": str(k),
                                "command": str(v),
                                "source": f"plugin:{entry.get('name', '?')}",
                            }
                        )
            except Exception:
                continue
    except Exception:
        pass
    return out


def _render_mcp(
    cfg: Optional[Dict[str, Any]] = None,
    say: Optional[Any] = None,
    label: str = "",
) -> None:
    """`/mcp [label]`: configured servers, or one server's tools.
    Assumes cfg is the session's effective config (may be None). A label
    spawns that server for a tool listing (best-effort — failures render
    honestly). Never raises."""
    say = say or _slash_say_default
    try:
        servers = mcp_server_table(cfg)
        lab = (label or "").strip()
        if not lab:
            if not servers:
                say(
                    "[vex.muted]no MCP servers configured[/] "
                    "[vex.muted](agent_mcp_servers in config, or a "
                    "plugin's mcp_servers map)[/]"
                )
                return
            say("[vex.accent]mcp servers[/] [vex.muted](/mcp <label> lists tools)[/]")
            for s in servers:
                say(
                    f"  [vex.accent]{s['label']}[/] "
                    f"[vex.muted]({s['source']})[/] "
                    f"[vex.muted]{s['command'][:80]}[/]"
                )
            return
        # one server's tools (read-only surface names from the agent
        # loop for resolution; the public client for the listing)
        from harness.agent_loop import _resolve_mcp_server
        from memory.mcp_client import list_mcp_tools

        try:
            command = _resolve_mcp_server(lab, cfg or {})
        except Exception:
            command = None
        if not command:
            say(f"[vex.error]unknown MCP server: {lab}[/]")
            if servers:
                say(
                    "[vex.muted]configured: "
                    + ", ".join(s["label"] for s in servers)
                    + "[/]"
                )
            return
        try:
            out = list_mcp_tools(command)
        except Exception as exc:
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        tools = (out or {}).get("tools") or []
        if not (out or {}).get("ok"):
            say(
                f"[vex.error]{lab} unavailable[/] "
                f"[vex.muted]{(out or {}).get('error', '?')}[/]"
            )
            return
        if not tools:
            say(f"[vex.muted]{lab}: no tools exposed[/]")
            return
        say(f"[vex.accent]{lab}[/] [vex.muted]({len(tools)} tools)[/]")
        for tool in tools[:30]:
            try:
                name = tool.get("name", "?")
                desc = (tool.get("description") or "")[:100]
            except Exception:
                continue
            say(f"  [vex.accent]{name}[/]" + (f" [vex.muted]{desc}[/]" if desc else ""))
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
            say("[vex.muted].vex/ already set up — nothing to create[/]")
    except Exception as exc:
        say(f"[vex.muted](init unavailable: {type(exc).__name__})[/]")


def _do_logout(say: Optional[Any] = None) -> None:
    """`/logout`: strip the stored api_key via the onboarding module's
    own command (no new auth code). Never raises."""
    say = say or _slash_say_default
    try:
        from cli.onboard import cmd_logout

        # cmd_logout renders its own result lines through the shared
        # console (the REPL's idiom); the TUI captures them into the
        # transcript via its console capture.
        cmd_logout(None)
    except Exception as exc:
        say(f"[vex.muted](logout failed: {type(exc).__name__})[/]")


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
            f"[vex.ok]cleared[/] [vex.muted]— fresh conversation {new} "
            f"(previous {old} kept)[/]"
        )
        return new
    except Exception as exc:
        say(f"[vex.muted](clear failed: {type(exc).__name__})[/]")
        return None


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
        res = undo_edits(
            str(tid),
            Path(log_root),
            str(repo or Path.cwd()),
            steps=scope,
            targets=targets,
        )
        files = list(res.get("restored", [])) + list(res.get("deleted", []))
        if not files:
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
    history line — history carries no status — rather than matching
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


def _slash_command(
    line: str, low: str, last: Dict[str, Any], log_root: Path, state: Dict[str, Any]
):
    """Dispatch one /command (Task B + custom commands). Returns "continue"
    (handled), None (handled, no further processing), or "unknown".
    Kept small and genuinely useful per the brief — anything else goes
    to cli/AGENTS.md's future-work list instead of here.

    CUSTOM COMMANDS (Plugins round, Task B): /<name> that is not a
    built-in resolves via cli.commands.load_command (project .vex/
    commands/ + global + plugin roots). The template's $ARGUMENTS is
    filled with everything after the command name and the session runs
    it as a fix request (a custom command IS a reusable instruction for
    the harness — exactly what an issue is).
    """
    con = ui.console()
    parts = line.split()
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
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        low_rest = rest.lower()
        if (
            low_rest == "undo"
            or low_rest.startswith("undo ")
            or low_rest == "undo all"
            or low_rest == "all"
        ):
            arg = (
                rest[len("undo") :].strip()
                if low_rest.startswith("undo")
                else rest.strip()
            )
            _render_undo_result(
                undo_result(last, log_root, state.get("repo"), arg), last
            )
            return None
        if last.get("diff"):
            ui.print_diff(last["diff"])
            return None
        # Agent runs that made no recorded diff yet: recompute live
        # (pristine reference vs the repo) so /diff works post-session.
        tid = last.get("task_id")
        if tid and str(tid).startswith("agent-"):
            from harness.agent_loop import agent_diff as _adiff

            live = _adiff(tid, Path(log_root), str(state.get("repo") or Path.cwd()))
            if live:
                last["diff"] = live
                ui.print_diff(live)
                return None
        con.print("[vex.muted]no diff from the last run[/]")
        return None

    if cmd in ("/sessions",):
        _print_sessions(
            con,
            log_root,
            line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else "",
        )
        return None

    if cmd in ("/feed",):
        tid = last.get("task_id")
        if not tid:
            con.print("[vex.muted]no run in this session yet[/]")
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
                con.print("[vex.muted]usage: /resume <task_id> (see /sessions)[/]")
                return None
            con.print(
                f"[vex.accent]resuming[/] [vex.muted]{recent['task_id']} — "
                f"{str(recent.get('issue') or '')[:80]}[/]"
            )
            try:
                _fold_resumed(
                    _resume_task(recent["task_id"], log_root, state),
                    last,
                    log_root,
                    state,
                )
            except KeyboardInterrupt:
                con.print(
                    "\n[vex.warn]interrupted — resumable via "
                    "[vex.accent]vex --continue[/][/]"
                )
            except Exception as exc:
                from cli.errors import explain_exception

                con.print(f"[vex.error]resume of {recent['task_id']!r} failed:[/]")
                explain_exception(exc)
            return None
        try:
            _fold_resumed(
                _resume_task(parts[1], log_root, state), last, log_root, state
            )
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
                        "\n[vex.warn]interrupted — live repo kept as-is "
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
                "[vex.accent]plan mode[/] [vex.muted]— previewing steps "
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
                    "\n[vex.warn]interrupted — containers cleaned, "
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
                con.rule("[vex.accent]/review — running as a fix request[/]")
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
                        "\n[vex.warn]interrupted — containers cleaned, "
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
                "[vex.ok]compacted[/] [vex.muted]— older turns summarized "
                "(recall-backed), recent turns kept[/]"
            )
            con.print(f"[vex.muted]{summary[:400]}[/]")
        else:
            con.print("[vex.muted]nothing to compact yet[/]")
        return None

    if cmd in ("/copy-diff", "/copy"):
        diff = last.get("diff")
        if not diff:
            con.print("[vex.muted]no diff from the last run[/]")
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
                "[vex.warn]clipboard unavailable[/] [vex.muted]— "
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
            try:
                from cli.vexconfig import merged_settings

                state["file_config"] = merged_settings()
            except Exception:
                pass
            con.print("[vex.ok]model configured[/]")
        elif rc == 1:
            con.print("[vex.muted]login skipped — `vex login` any time[/]")
        return None

    if cmd in ("/logout",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            con.print("[vex.muted]usage: /logout (removes the stored api_key)[/]")
            return None
        _do_logout()
        return None

    if cmd in ("/mcp",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if len(rest.split()) > 1:
            con.print("[vex.muted]usage: /mcp [label][/]")
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
        _render_mcp(cfg, label=rest)
        return None

    if cmd in ("/skills",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        _render_skills(state.get("repo"), filt=rest)
        return None

    if cmd in ("/cost",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            con.print("[vex.muted]usage: /cost (last run + session total)[/]")
            return None
        _render_cost(last, log_root)
        return None

    if cmd in ("/undo",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        _render_undo_result(undo_result(last, log_root, state.get("repo"), rest), last)
        return None

    if cmd in ("/clear",):
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if rest:
            con.print("[vex.muted]usage: /clear (starts a fresh conversation)[/]")
            return None
        _do_clear(log_root, state.get("repo"), state)
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
        con.rule(f"[vex.accent]/{name} — running as an agent task[/]")
        if arguments:
            con.print(f"[vex.muted]arguments: {arguments}[/]")
        con.print(f"[vex.muted]instruction:[/]\n{filled}")
        con.print()
        try:
            result_info = _run_one_agent(
                filled,
                Path(state["repo"]) if state.get("repo") else _detect_repo(),
                state,
                log_root,
                file_config=state.get("file_config"),
            )
        except KeyboardInterrupt:
            con.print(
                "\n[vex.warn]interrupted — containers cleaned, "
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
        hint = " — custom commands available: " + ", ".join(f"/{n}" for n in available)
    con.print(
        f"[vex.error]unknown command: {line.split()[0]}[/] "
        f"[vex.muted]— try /help{hint}[/]"
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
    from harness.qa_mode import run_question

    from cli.main import _clear_router_context, _set_router_context

    cfg = _mode_config(state, file_config)
    # Pre-generate the task id so the embedded-UI hook (the TUI's live
    # sidebar/run-line) can attach BEFORE the blocking model call —
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
        con.print(out["answer"])
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
    }


class _RouterCtx:
    """Minimal duck-typed stand-in for Task for _set_router_context (it
    only reads .config — building a full Task just for router context
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
    from harness.research_mode import run_research

    from cli.main import _clear_router_context, _set_router_context

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
        con.print(out["answer"])
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
    run_task — so we replicate _execute_task's monitor/result core
    around the build call).
    """
    import uuid

    from harness.build_mode import run_build

    from cli.main import _clear_router_context, _set_router_context

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
    # logs/{task_id}/steering.jsonl — the same journal this injects).
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

    style = "vex.ok" if result.status == "success" else "vex.error"
    mark = ui.GLYPHS["ok"] if result.status == "success" else ui.GLYPHS["fail"]
    con.print()
    con.print(
        f"[{style}]{mark} {result.status.upper()}[/] [vex.muted]{ui.DOT}[/] "
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
        con.print(Markdown(rat.read_text(encoding="utf-8")))
    con.print(
        f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]{Path(log_root).resolve() / task_id / 'trace.jsonl'}[/]"
    )
    record_session(log_root, task_id, request, str(repo), result.status)
    notify_done(result.status)
    return {
        "task_id": task_id,
        "log_root": log_root,
        "diff": result.diff,
        "status": result.status,
    }


def _agent_approve_prompt(tool: str, args: Dict[str, Any], preview: str) -> bool:
    """approve_fn for agent BASH/EDIT/WRITE when agent_approval=require.

    Prompts on the REPL console with a short preview; "y" approves one
    call. Never raises (a broken prompt refuses — fail-safe).
    """
    try:
        import cli.ui as _ui

        try:
            _ui.bell("vex approval needed")
        except Exception:
            pass
        con = ui.console()
        con.print(f"[vex.warn]approval needed — {tool.upper()}[/]")
        if preview:
            for ln in preview.splitlines()[:12]:
                con.print(f"[vex.muted]  {ln[:120]}[/]")
        ans = input(f"allow this {tool}? [y/N] ").strip().lower()
        return ans in ("y", "yes")
    except Exception:
        return False


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
    con.rule("[vex.accent]agent plan preview[/]")
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
        con.print("[vex.ok]approved — starting the agent[/]")
        return request
    if answer in ("e", "edit"):
        try:
            edit = input("steer the plan (one line, empty cancels): ").strip()
        except (EOFError, KeyboardInterrupt):
            edit = ""
        if not edit:
            con.print("[vex.warn]cancelled — nothing ran[/]")
            return None
        return request + "\n\nUser-steered plan: " + edit
    con.print("[vex.warn]cancelled — nothing ran[/]")
    return None


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
    repo_name = Path(repo).name
    con.print(
        f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]working in[/] "
        f"[vex.accent]{repo_name}[/] [vex.muted]{ui.DOT} "
        f"{ui.GLYPHS['wait']} {tid}[/]"
    )
    from cli.main import _clear_router_context, _set_router_context

    _set_router_context(_RouterCtx(config))
    mon = LiveMonitor(tid, log_root).start(quiet=bool((state or {}).get("quiet")))
    _fire_task_start(tid)
    _set_live_run(tid, log_root)
    if approve_fn_override is not None:
        approve = approve_fn_override
    else:
        approve = (
            _agent_approve_prompt
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

    style = "vex.ok" if out.get("status") == "success" else "vex.error"
    mark = ui.GLYPHS["ok"] if out.get("status") == "success" else ui.GLYPHS["fail"]
    con.print()
    con.print(
        f"[{style}]{mark} {str(out.get('status', '?')).upper()}[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{len(out.get('model_calls', []))} model calls[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{elapsed:.0f}s[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.accent2]{ui.fmt_cost(out.get('cost_usd', 0.0))}[/]"
    )
    if out.get("answer"):
        con.print()
        con.print(str(out["answer"]))
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

    from shared.types import Task

    from cli.vexconfig import apply_config_defaults, normalize_runtime_keys

    con = ui.console()
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
    # on every exit path — a leaked registration would silently steer
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

    style = "vex.ok" if result.status == "success" else "vex.error"
    mark = ui.GLYPHS["ok"] if result.status == "success" else ui.GLYPHS["fail"]
    con.print()
    con.print(
        f"[{style}]{mark} {result.status.upper()}[/] [vex.muted]{ui.DOT}[/] "
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
        con.print(Markdown(rat.read_text(encoding="utf-8")))
    con.print(
        f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]{Path(log_root).resolve() / task_id / 'trace.jsonl'}[/]"
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
    }


def _trace_feed_command(arg, last: Dict[str, Any], log_root: Path) -> None:
    """/trace in the rich REPL: print the last (or live) run's feed —
    the readable one-line-per-action view built from the SAME trace
    file the run writes (Task E: no second logging path). No arg lists
    entries; /trace <n> prints entry n's full detail inline (the REPL
    has no modal — plain print is its idiom)."""
    from cli import tracelog as tl

    con = ui.console()
    tid = last.get("task_id")
    if not tid:
        con.print("[vex.muted]no run in this session yet[/]")
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
        con.print("[vex.error]usage: /trace [n][/][vex.muted] — an entry number[/]")
        return
    match = next((e for e in entries if e.index == n), None)
    if match is None:
        con.print(
            f"[vex.error]no feed entry {n}[/] [vex.muted](0–{entries[-1].index})[/]"
        )
        return
    con.rule(f"[vex.accent]trace {n} — {match.detail_title or match.category}[/]")
    con.print(f"[{ui.TEXT_PRIMARY}]{match.summary}[/]")
    detail = (match.detail or "").strip() or "(no detail recorded)"
    con.print(detail)


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
    _fire_prompt_body(f"run this plan? [Y/n] ", body_lines)
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
    """Deliver Ctrl+C semantics to the run (plan rejection, /cancel).

    Inside the full-screen TUI (cli.tui): the run blocks a WORKER
    thread while the main thread runs textual's event loop — SIGINT at
    main would kill the app, so the _CANCEL_RUN hook (set by the app)
    injects the KeyboardInterrupt into the worker instead (same
    semantics downstream: containers stop, checkpoints stay, the task
    is resumable via `vex --continue`).

    In the rich REPL / flag commands: signal.raise_signal(SIGINT)
    reaches the MAIN thread's interrupt handling (Python only runs
    signal handlers on the main thread) — exactly what a human pressing
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
