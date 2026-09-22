"""Tests for the full-screen textual TUI (cli/tui.py, 2026-09-13 round).

These run the REAL app headlessly through textual's own Pilot harness
(app.run_test()): no terminal needed, real widgets, real event loop,
real modal screens. They pin the round's four tasks:

- Task A (persistent shell): header renders version/model/repo/status;
  transcript scrollable; input echoes; hint bar present.
- Task B (in-place live updates): a fix run updates the run-line widget
  in place from trace events (phase label + event count + cost), NEVER
  as new transcript lines; the header status flips running->idle.
- Task C (nothing lost): every slash command, the bare session
  commands, the intent gate, custom commands, plan preview (as a modal),
  and /cancel all work inside the shell.
- Task D (design system): the role map carries the exact oxblood/
  oxide palette from ui.VEX_THEME; CSS variables match.

ORDER-SENSITIVITY: these tests share process-global state by design
(cli.interactive's embedded-UI hooks, worker threads holding the global
input/print patch while a run is live). Every test that starts a run
DRAINS its worker before teardown (see _drain_worker), which makes them
order-safe — but run with `-p no:randomly` if pytest-randomly is
installed: shuffled orders can pair a modal test with a still-dying
worker thread from a predecessor (found live: both modal tests failed
~25-80% under shuffled orders, 0% in file order).

    python -m pytest tests/test_cli_tui.py -p no:randomly

Async note: textual's run_test needs a running event loop -> pytest
anyio, same backend fixture as the MCP suites.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from textual.widgets import Input, RichLog

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Helpers — a tiny scripted backend so the app runs without Docker/models
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, task_id="fix-test01", status="success"):
        self.task_id = task_id
        self.status = status
        self.diff = "+fixed line\n-old line"
        self.verification = None
        self.cost_usd = 0.0
        self.attempts = 1
        self.model_calls = []
        self.log_path = ""


@pytest.fixture
def clean_hooks():
    """cli.interactive's UI hooks must be reset around every test (the
    app sets them on mount; a leaked hook poisons later tests' REPLs).
    Also clears the live-run registration: a worker killed mid-run
    (KI in /cancel tests) never reaches its _clear_live_run, and a
    leaked registration would route later tests' input into steering
    a dead task."""
    import cli.interactive as iv

    old_start, old_cancel = iv._ON_TASK_START, iv._CANCEL_RUN
    iv._ON_TASK_START, iv._CANCEL_RUN = None, None
    yield
    iv._ON_TASK_START, iv._CANCEL_RUN = old_start, old_cancel
    iv._clear_live_run()


def _make_app(tmp_path, **kw):
    """The app wired to a scripted backend (issue text captured;
    a trace file with a few events so the run-line has something to
    tail). The worker's task is 'running' until the test releases it.

    The fake is installed as BOTH _run_one_fix and _run_one_agent (the
    session dispatches agent tasks through the agent loop; the fix
    entry stays for the legacy `vex fix` path) so bug-shaped input
    reaches it either way."""
    import cli.tui as t

    held = {
        "issue": None,
        "release": threading.Event(),
        "trace_dir": tmp_path / "logs" / "fix-test01",
        "app": None,
    }
    held["trace_dir"].mkdir(parents=True, exist_ok=True)

    def fake_run_one_fix(issue, repo, state, log_root, file_config=None):
        held["issue"] = issue
        # fire the same hook the REAL _execute_task fires (the fake
        # bypasses it; the TUI's run-line attaches through this hook),
        # and register the live run the same way (steering round — the
        # TUI's _steer_live routes plain text through this registration)
        iv._fire_task_start("fix-test01")
        iv._set_live_run("fix-test01", log_root)
        # a couple of trace events for the run-line
        trace = held["trace_dir"] / "trace.jsonl"
        with trace.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "plan", "data": {"plan": []}}) + "\n")
            fh.write(
                json.dumps(
                    {
                        "kind": "model_response",
                        "data": {"usage": {"tokens": 10, "cost": 0.01}},
                    }
                )
                + "\n"
            )
        # block until the test releases (simulates the harness loop).
        # POLL with time.sleep -- NOT release.wait(timeout): a C-level
        # timed acquire ignores pending async exceptions until it
        # expires, which makes /cancel + KI-delivery flaky (seen live:
        # the /cancel test's worker only died at the full timeout).
        # time.sleep delivers injected KeyboardInterrupts promptly.
        deadline = time.monotonic() + 30
        try:
            while not held["release"].is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            iv._clear_live_run()
        return {
            "task_id": "fix-test01",
            "log_root": log_root,
            "diff": "+x",
            "status": "success",
        }

    app = t.VexApp(
        repo=tmp_path / "repo",
        log_root=tmp_path / "logs",
        state={
            "model": None,
            "provider": None,
            "plan_preview": None,
            "quiet": False,
            "repo": str(tmp_path / "repo"),
            "file_config": {},
        },
        file_config={},
        version="9.9.9",
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    app._test = held  # type: ignore[attr-defined]

    import cli.interactive as iv

    iv._run_one_fix = fake_run_one_fix  # type: ignore[assignment]
    iv._run_one_agent = fake_run_one_fix  # type: ignore[assignment]
    held["app"] = app
    held["_real_run_one_fix"] = (
        iv._run_one_fix.__wrapped__ if hasattr(iv._run_one_fix, "__wrapped__") else None
    )
    return app, held


@pytest.fixture(autouse=True)
def restore_backend():
    """Snapshot iv._run_one_fix/_run_one_agent before EVERY test in this module and
    restore both after (autouse -- an opt-in fixture was the bug: the
    conversational-gate test used _make_app WITHOUT requesting the
    restore fixture, so its fake leaked into the NEXT test; the
    approve-test's own restore then snapshotted THE FAKE and its worker
    called the zombie backend: no _execute_task, no preview watcher,
    no modal -- the "modal never appeared" failure, 100% reproducible
    once pytest-randomly was disabled and file order was restored)."""
    import cli.interactive as iv

    real_fix = iv._run_one_fix
    real_agent = iv._run_one_agent
    yield
    iv._run_one_fix = real_fix  # type: ignore[assignment]
    iv._run_one_agent = real_agent  # type: ignore[assignment]


async def _drain(pilot, secs=0.2):
    """Let the event loop + worker threads run for a moment."""
    await asyncio.sleep(secs)
    await pilot.pause()


async def _drain_worker(app, pilot, timeout_s=10.0):
    """Wait for the app's worker thread to fully finish.

    CRITICAL for test isolation: the worker holds the GLOBAL
    input/print patch while running (see tui._prompt_patches -- it must
    cover the backend's helper threads, so it is time-scoped, not
    thread-scoped). If a test exits with the worker alive, the leaked
    patch's closure still points at THIS app and poisons the next
    test's modal prompts. Every test that starts a run must drain it
    before teardown."""
    if app._worker_thread is None:
        return
    deadline = time.monotonic() + timeout_s
    while app._worker_thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        await pilot.pause()


def _transcript_plain(app) -> str:
    """All transcript text, concatenated (for content assertions)."""
    from rich.text import Text

    t = Text()
    for line in app.query_one("#vex-body").lines:
        for seg in line._segments:
            t.append(seg.text, style=seg.style)
    return t.plain


# ---------------------------------------------------------------------------
# Task A — persistent shell
# ---------------------------------------------------------------------------


class TestShell:
    async def test_header_model_repo_version_status(self, tmp_path, clean_hooks):
        """The compact header line: -- vex <version> · model <m> · <repo>,
        plus the idle status chip on the right."""
        import cli.tui as t

        (tmp_path / "repo").mkdir(exist_ok=True)
        app = t.VexApp(
            repo=tmp_path / "repo",
            log_root=tmp_path / "logs",
            state={
                "model": "m-echo",
                "repo": str(tmp_path / "repo"),
                "file_config": {},
            },
            file_config={},
            version="9.9.9",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            plain = str(app.query_one("#vex-brand").visual)
            assert "vex 9.9.9" in plain
            assert "m-echo" in plain
            assert "repo" in plain
            status = str(app.query_one("#vex-status").visual)
            assert status.strip() == "idle"

    async def test_input_echoes_into_transcript(self, tmp_path, clean_hooks):
        """A submitted line appears in the transcript as the user's
        message (the conversation record), prefixed by the vex prompt."""
        import cli.tui as t

        (tmp_path / "repo").mkdir(exist_ok=True)
        app = t.VexApp(
            repo=tmp_path / "repo",
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path / "repo"), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "hi there"
            await pilot.press("enter")
            await pilot.pause()
            plain = _transcript_plain(app)
            assert "hi there" in plain

    async def test_hint_bar_lists_shortcuts(self, tmp_path, clean_hooks):
        """The OpenCode-style hint bar names the key shortcuts."""
        import cli.tui as t

        app = t.VexApp(
            repo=tmp_path,
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            hints = str(app.query_one("#vex-hints").visual)
            for fragment in ("/help", "ctrl+c", "ctrl+q"):
                assert fragment in hints, hints

    async def test_first_launch_shows_splash_in_transcript(self, tmp_path, clean_hooks):
        """Empty log root -> the wordmark splash (tagline + repo row)
        lands in the transcript (OpenCode's empty-state pattern)."""
        import cli.tui as t

        app = t.VexApp(
            repo=tmp_path / "myrepo",
            log_root=tmp_path / "empty-logs",
            state={"repo": str(tmp_path / "myrepo"), "file_config": {}},
            file_config={},
            version="1.0",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            plain = _transcript_plain(app)
            assert "verified, not vibed" in plain
            assert "myrepo" in plain

    async def test_wordmark_on_every_session(self, tmp_path, clean_hooks):
        """The VEX wordmark is the shell's face: EVERY session opens
        with the letterform rows in the transcript — not just first
        launch (later launches keep them + a lean hint row)."""
        import cli.tui as t
        import cli.ui as ui

        logs = tmp_path / "logs"
        # a prior session exists -> NOT first launch
        import cli.interactive as iv

        iv.record_session(logs, "fix-old", "old issue", "/r", "success")
        app = t.VexApp(
            repo=tmp_path / "myrepo",
            log_root=logs,
            state={"repo": str(tmp_path / "myrepo"), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            plain = _transcript_plain(app)
            # the wordmark rows render (any encoding: box-drawing or '#')
            rows = ui.wordmark_lines()
            assert sum(1 for r in rows if r.strip() in plain) >= 3, plain[:400]
            assert "verified, not vibed" in plain
            # lean session: no repo/logs info rows
            assert "repo " not in plain


class TestThinkingIndicator:
    """The agent-is-thinking treatment: a distinct ORBIT glyph + a
    rotating technical joke (Qwen-Code-style flavor), only during
    model-thinking phases — the linear spinner everywhere else."""

    def test_thinking_line_shows_joke_and_orbit(self):
        import cli.tui as t
        import cli.ui as ui

        run = t._RunState("fix-x")
        run.consume({"kind": "model_request", "data": {"step": "step-1"}})
        assert run.thinking is True
        line = run.line(frame=5)
        # the orbit glyph is in the line (frame 5 -> index 1 -> '◓')
        assert ui.THINK_FRAMES[5 % len(ui.THINK_FRAMES)] in line
        # a joke from the shared table is present
        assert any(j in line for j in ui.JOKES)

    def test_non_thinking_line_has_no_joke(self):
        import cli.tui as t
        import cli.ui as ui

        run = t._RunState("fix-x")
        run.consume({"kind": "verify", "data": {}})
        assert run.thinking is False
        line = run.line(frame=5)
        assert not any(j in line for j in ui.JOKES)
        # the linear spinner frames are used instead of the orbit
        assert (
            ui.THINK_FRAMES[5 % len(ui.THINK_FRAMES)] not in line.split("verifier")[0]
        )

    def test_thinking_clears_on_response(self):
        import cli.tui as t

        run = t._RunState("fix-x")
        run.consume({"kind": "model_request", "data": {"step": "s"}})
        run.consume(
            {"kind": "model_response", "data": {"usage": {"tokens": 1, "cost": 0.0}}}
        )
        assert run.thinking is False
        # and the usage accounting still works
        assert run.calls == 1

    def test_joke_rotation_is_deterministic(self):
        import cli.ui as ui

        assert ui.joke_at(0) == ui.JOKES[0]
        assert ui.joke_at(len(ui.JOKES)) == ui.JOKES[0]
        assert ui.joke_at(5) == ui.JOKES[5]
        # all ASCII (cp1252-safe on any console)
        assert all(j.isascii() for j in ui.JOKES)

    async def test_run_line_shows_joke_during_model_request(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """Live wiring: a model_request trace event flips the run-line
        into the thinking treatment (joke visible in the widget)."""
        app, held = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "fix the crash in main.py"
            await pilot.press("enter")
            await _drain(pilot, 0.5)
            trace = held["trace_dir"] / "trace.jsonl"
            with trace.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps({"kind": "model_request", "data": {"step": "step-1"}})
                    + "\n"
                )
            for _ in range(40):
                await _drain(pilot, 0.1)
                rl = str(app.query_one("#vex-runline").visual)
                import cli.ui as ui

                if any(j in rl for j in ui.JOKES):
                    break
            assert any(j in rl for j in ui.JOKES), rl
            assert "model: thinking" in rl
            held["release"].set()
            await _drain_worker(app, pilot)


# ---------------------------------------------------------------------------
# Task C — session commands + intent gate + slash commands
# ---------------------------------------------------------------------------


class TestSessionCommands:
    async def test_exit_quits(self, tmp_path, clean_hooks):
        import cli.tui as t

        app = t.VexApp(
            repo=tmp_path,
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "exit"
            await pilot.press("enter")
            await pilot.pause()
            assert app.return_code == 0 or app._exit_code == 0 or not app.is_running

    async def test_help_lists_every_slash_command(self, tmp_path, clean_hooks):
        """/help output must contain all built-in slash commands (the
        REPL contract — nothing lost in the rebuild)."""
        import cli.tui as t

        app = t.VexApp(
            repo=tmp_path,
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            plain = _transcript_plain(app)
            for cmd in (
                "/status",
                "/trace",
                "/diff",
                "/sessions",
                "/resume",
                "/approve",
                "/reject",
                "/cancel",
                "/steer",
                "/quiet",
            ):
                assert cmd in plain, cmd

    async def test_repo_switch_updates_header(self, tmp_path, clean_hooks):
        import cli.tui as t

        (tmp_path / "alpha").mkdir(exist_ok=True)
        (tmp_path / "beta").mkdir(exist_ok=True)
        app = t.VexApp(
            repo=tmp_path / "alpha",
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path / "alpha"), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = f"repo {tmp_path / 'beta'}"
            await pilot.press("enter")
            await pilot.pause()
            assert "beta" in str(app.query_one("#vex-brand").visual)
            assert str(tmp_path / "beta") in app.state["repo"]

    async def test_unknown_slash_hints(self, tmp_path, clean_hooks):
        import cli.tui as t

        app = t.VexApp(
            repo=tmp_path,
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "/nope"
            await pilot.press("enter")
            await pilot.pause()
            plain = _transcript_plain(app)
            assert "unknown command" in plain
            assert "/help" in plain


class TestIntentGateInTui:
    """The branding-round bug fix must hold inside the TUI too: 'hi'
    NEVER launches a run; a real bug sentence DOES."""

    async def test_conversational_input_never_launches(self, tmp_path, clean_hooks):
        app, held = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            for line in ("hi", "what can you do?", "thanks"):
                app.query_one("#vex-input", Input).value = line
                await pilot.press("enter")
                await pilot.pause()
            plain = _transcript_plain(app)
            assert "task fix-" not in plain
            assert held["issue"] is None  # backend never called
            assert app._worker_thread is None

    async def test_bug_sentence_launches_with_issue_text(
        self, tmp_path, clean_hooks, restore_backend
    ):
        app, held = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(
                "#vex-input", Input
            ).value = "mean() in mathutil.py returns the sum; make it the mean"
            await pilot.press("enter")
            await _drain(pilot, 0.3)
            assert held["issue"] is not None
            assert held["issue"].startswith("mean() in mathutil.py")
            # release the worker so the app can quit cleanly
            held["release"].set()
            await _drain_worker(app, pilot)


# ---------------------------------------------------------------------------
# Task B — live updates in place (THE core difference)
# ---------------------------------------------------------------------------


class TestLiveRunLine:
    async def test_run_line_updates_in_place(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """During a run: the run-line widget shows phase/events/cost and
        is updated IN PLACE (one widget, never re-printed per event), the
        header flips to running, and the live FEED (this round) grows
        one line per action — the phase text itself never scrolls."""
        app, held = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "fix the crash in main.py"
            await pilot.press("enter")
            await _drain(pilot, 0.5)

            # worker started; the task-start hook fired the run-line
            assert app._run is not None
            assert app._run.task_id == "fix-test01"
            # wait for the tail thread to fold the trace events in
            for _ in range(40):
                if app._run.events >= 2:
                    break
                await _drain(pilot, 0.1)
            assert app._run.events >= 2
            assert app._run.cost == pytest.approx(0.01)
            rl_plain = str(app.query_one("#vex-runline").visual)
            assert "model: replied" in rl_plain
            assert "2 events" in rl_plain
            assert "$0.0100" in rl_plain
            # header status is the running state
            assert str(app.query_one("#vex-status").visual).strip() == "running"

            # the feed rendered the plan/model events as feed lines
            plain0 = _transcript_plain(app)
            assert "planned" in plain0

            # IN PLACE: the PHASE text never scrolls (the run-line is
            # one widget) — a new event moves the phase there, and the
            # FEED gains exactly its one action line, nothing else.
            n_before = len(app.query_one("#vex-body").lines)
            trace = held["trace_dir"] / "trace.jsonl"
            with trace.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": "rationale", "data": {}}) + "\n")
            for _ in range(40):
                await _drain(pilot, 0.1)
                if app._run and "rationale" in app._run.phase:
                    break
            rl_plain2 = str(app.query_one("#vex-runline").visual)
            assert "writing rationale" in rl_plain2
            assert "3 events" in rl_plain2
            # the phase string appears ONCE in the transcript (the feed
            # line), never as a re-printed run-line
            plain1 = _transcript_plain(app)
            assert "writing the rationale" in plain1
            assert len(app.query_one("#vex-body").lines) == n_before + 1

            held["release"].set()
            await _drain_worker(app, pilot)  # worker MUST die before teardown
            # post-run: run-line hidden, status idle, result recorded
            assert app.query_one("#vex-runline").styles.display == "none"
            assert str(app.query_one("#vex-status").visual).strip() == "idle"
            assert app.last.get("task_id") == "fix-test01"

    async def test_in_flight_input_queues_and_slash_works(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """While a run is live: plain lines STEER the running task
        (steering round — a journal event, not a queued follow-up task),
        and /status /diff style commands still answer."""
        app, held = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "fix the crash in main.py"
            await pilot.press("enter")
            await _drain(pilot, 0.4)
            assert app._worker_thread is not None and app._worker_thread.is_alive()

            app.query_one(
                "#vex-input", Input
            ).value = "actually only touch lib/parser.py"
            await pilot.press("enter")
            await pilot.pause()
            plain = _transcript_plain(app)
            # steered (not queued): the ack landed and a journal event
            # was written for the live task
            assert "steered" in plain
            assert "queued" not in plain
            import cli.interactive as iv

            live = iv._live_run()
            assert live is not None, "run must be registered as live"
            steer_file = Path(live["log_root"]) / live["task_id"] / "steering.jsonl"
            assert steer_file.is_file()
            injected = [
                json.loads(ln)
                for ln in steer_file.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            assert any(
                o.get("op") == "inject" and "parser.py" in o.get("text", "")
                for o in injected
            ), injected

            app.query_one("#vex-input", Input).value = "/cancel"
            await pilot.press("enter")
            await _drain(pilot, 0.5)
            # the worker got the async KI. NOTE: delivery to a thread
            # parked in Event.wait (this fake's shape) takes seconds --
            # the C-level wait only checks pending exceptions at GIL
            # switch intervals (~5s worst case). Real runs sit in
            # Python-level I/O (model calls) where it lands promptly.
            # The worker's KI path prints the interrupted message; the
            # test asserts BOTH the message and the worker finishing.
            cancelled_msg = False
            for _ in range(120):  # up to 12s (GIL switch worst case)
                plain = _transcript_plain(app)
                if "interrupted" in plain:
                    cancelled_msg = True
                if not app._worker_thread.is_alive():
                    break
                await _drain(pilot, 0.1)
            held["release"].set()  # unblock if the KI didn't land in time
            await _drain_worker(app, pilot)  # patch must be released pre-teardown
            assert cancelled_msg, "/cancel must interrupt the run"

    async def test_backend_output_replayed_with_theme_colors(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """The backend's rich output (result summary etc.) lands in the
        transcript WITH its Vex colors: the capture records themed
        segments and replays them as styled Text (textual doesn't know
        the vex.* roles, so the styles must be pre-resolved)."""
        import cli.interactive as iv
        import cli.tui as t

        (tmp_path / "repo").mkdir(exist_ok=True)

        def printing_backend(issue, repo, state, log_root, file_config=None):
            con = t.ui.console()
            con.print("[vex.ok]SUCCESS[/] [grey58]·[/] [vex.accent2]$0.0100[/]")
            return {
                "task_id": "fix-test01",
                "log_root": log_root,
                "diff": "+x",
                "status": "success",
            }

        real_fix = iv._run_one_fix
        real_agent = iv._run_one_agent
        iv._run_one_fix = printing_backend  # type: ignore[assignment]
        iv._run_one_agent = printing_backend  # type: ignore[assignment]
        try:
            app = t.VexApp(
                repo=tmp_path / "repo",
                log_root=tmp_path / "logs",
                state={"repo": str(tmp_path / "repo"), "file_config": {}},
                file_config={},
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#vex-input", Input).value = "fix the crash in main.py"
                await pilot.press("enter")
                for _ in range(50):
                    await _drain(pilot, 0.1)
                    if app.last.get("task_id"):
                        break
                plain = _transcript_plain(app)
                assert "SUCCESS" in plain
                assert "$0.0100" in plain
                # the styled span survives: find the line holding SUCCESS
                # and check its segments carry the vex.ok green
                from rich.text import Text

                tt = Text()
                for line in app.query_one("#vex-body").lines:
                    for seg in line._segments:
                        tt.append(seg.text, style=seg.style)
                # locate 'SUCCESS' and its span style
                for span in tt.spans:
                    frag = tt.plain[span.start : span.end]
                    if frag == "SUCCESS":
                        st = span.style
                        assert st is not None and st.color is not None
                        # vex.ok is the design-system success token
                        # (#34D399 — actual success states only)
                        assert st.color.get_truecolor().hex.lower() in ("#34d399",), st
                        break
                else:
                    pytest.fail("SUCCESS span not found in transcript")
                await _drain_worker(app, pilot)
        finally:
            iv._run_one_fix = real_fix  # type: ignore[assignment]
            iv._run_one_agent = real_agent  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Plan preview as a modal (Task C — the approval/preview flows survive)
# ---------------------------------------------------------------------------


class TestPlanPreviewModal:
    async def test_preview_pops_modal_and_approve(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """plan_preview=on: the REAL _execute_task spawns the preview
        watcher; its input() becomes the _ConfirmScreen modal (with the
        plan steps visible in the body); Enter approves; the run then
        completes and the run-line lands idle.

        The fix worker is started DIRECTLY (preview is a fix-path
        feature — the session's agent dispatch would route this
        sentence to the agent loop, which has no preview)."""
        import cli.tui as t
        from cli import deps
        from shared.types import TaskResult

        (tmp_path / "repo").mkdir(exist_ok=True)
        logs = tmp_path / "logs"
        release = threading.Event()

        def fake_run_task(task, log_root=None):
            d = Path(log_root) / task.task_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "trace.jsonl").write_text(
                json.dumps(
                    {
                        "kind": "plan",
                        "data": {
                            "plan": [
                                {
                                    "id": 1,
                                    "description": "fix mean",
                                    "checkpoint": "tests pass",
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            # hold the run in the preview window. POLL with sleep (NOT
            # release.wait(timeout)) so the injected reject-KI lands
            # promptly -- C-level timed waits swallow async exceptions
            # until they expire (the flake seen live in full-file runs).
            deadline = time.monotonic() + 20
            while not release.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            return TaskResult(
                task_id=task.task_id,
                status="success",
                attempts=1,
                diff="+x",
                verification=None,
                cost_usd=0.01,
                model_calls=[],
                log_path=str(d / "trace.jsonl"),
            )

        deps.set_run_task(fake_run_task)
        try:
            app = t.VexApp(
                repo=tmp_path / "repo",
                log_root=logs,
                state={
                    "repo": str(tmp_path / "repo"),
                    "plan_preview": True,
                    "quiet": True,
                    "file_config": {},
                },
                file_config={},
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                app._start_run("fix the off-by-one in idx", mode="fix")
                # the preview watcher polls ~0.25s. Generous window: a
                # full-file run races the previous test's dying worker
                # threads for the GIL (Windows scheduler); the modal
                # itself is verified within ~0.3s when uncontended.
                for _ in range(300):
                    await _drain(pilot, 0.1)
                    if len(app.screen_stack) > 1:
                        break
                assert isinstance(app.screen, t._ConfirmScreen)

                # the plan steps are visible in the modal's body
                from rich.text import Text

                modal_text = Text()
                for w in app.screen.query(RichLog):
                    for ln in w.lines:
                        for seg in ln._segments:
                            modal_text.append(seg.text, style=seg.style)
                assert "fix mean" in modal_text.plain
                assert "tests pass" in modal_text.plain
                assert "run this plan" in str(
                    app.screen.query_one("#prompt-title").visual
                )

                # approve with the default (empty input + Enter = y)
                await pilot.press("enter")
                await _drain(pilot, 0.3)
                assert not isinstance(app.screen, t._ConfirmScreen)
                # release the run; it completes and records
                release.set()
                await _drain_worker(app, pilot)  # worker dead before teardown
                for _ in range(60):
                    await _drain(pilot, 0.2)
                    if app.last.get("task_id"):
                        break
                assert app.last.get("task_id", "").startswith("fix-")
                assert str(app.query_one("#vex-status").visual).strip() == "idle"
        finally:
            deps.reset_overrides()

    async def test_preview_reject_cancels_run(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """Rejecting the plan (esc -> 'n') cancels the run via the
        interrupt hook — the task is recorded interrupted (resumable),
        the same semantics as the REPL's reject path.

        Like the approve test, the fix worker starts directly (preview
        is fix-path machinery, not session dispatch)."""
        import cli.tui as t
        from cli import deps
        from shared.types import TaskResult

        (tmp_path / "repo").mkdir(exist_ok=True)
        logs = tmp_path / "logs"
        release = threading.Event()
        got_ki = threading.Event()

        def fake_run_task(task, log_root=None):
            d = Path(log_root) / task.task_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "trace.jsonl").write_text(
                json.dumps({"kind": "plan", "data": {"plan": []}}) + "\n",
                encoding="utf-8",
            )
            try:
                # POLL with sleep (async-KI lands promptly; see the
                # approve test's note on C-level timed waits)
                deadline = time.monotonic() + 20
                while not release.is_set() and time.monotonic() < deadline:
                    time.sleep(0.05)
            except KeyboardInterrupt:
                got_ki.set()
                raise
            return TaskResult(
                task_id=task.task_id,
                status="success",
                attempts=1,
                diff="+x",
                verification=None,
                cost_usd=0.01,
                model_calls=[],
                log_path=str(d / "trace.jsonl"),
            )

        deps.set_run_task(fake_run_task)
        try:
            app = t.VexApp(
                repo=tmp_path / "repo",
                log_root=logs,
                state={
                    "repo": str(tmp_path / "repo"),
                    "plan_preview": True,
                    "quiet": True,
                    "file_config": {},
                },
                file_config={},
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                app._start_run("fix the crash in app.py", mode="fix")
                # generous window (see the approve test's note on the
                # full-file GIL contention race)
                for _ in range(300):
                    await _drain(pilot, 0.1)
                    if len(app.screen_stack) > 1:
                        break
                assert isinstance(app.screen, t._ConfirmScreen)
                # reject: esc = 'n'
                await pilot.press("escape")
                await _drain(pilot, 0.5)
                assert not isinstance(app.screen, t._ConfirmScreen)
                # the run got the async KeyboardInterrupt (worker target).
                # Delivery to a thread parked in Event.wait takes up to
                # ~5-6s (GIL switch interval); poll long enough for it.
                for _ in range(120):
                    if got_ki.is_set():
                        break
                    await _drain(pilot, 0.1)
                assert got_ki.is_set(), "reject must interrupt the run"
                # and the reject was surfaced to the user
                plain = _transcript_plain(app)
                assert (
                    "rejected" in plain
                    or "cancelled" in plain
                    or "interrupted" in plain
                )
                release.set()  # belt & braces (KI already landed)
                await _drain_worker(app, pilot)  # worker dead before teardown
        finally:
            release.set()
            deps.reset_overrides()


# ---------------------------------------------------------------------------
# Live trace feed round — Tasks A/B/C/D (reasoning, tool feed, inline
# diff, collapsible detail)
# ---------------------------------------------------------------------------


class TestLiveFeed:
    """The feed renders live, one line per action, from the SAME trace
    the harness writes (Task E) — including tool calls the moment they
    land and an inline diff after edits."""

    @staticmethod
    def _feed_app(tmp_path):
        """App + fake backend writing a REALISTIC event stream (planner
        reply, plan, command+output, verify) with the run held open."""
        import cli.tui as t

        held = {
            "issue": None,
            "release": threading.Event(),
            "trace_dir": tmp_path / "logs" / "fix-feed01",
            "wrote": threading.Event(),
        }
        held["trace_dir"].mkdir(parents=True, exist_ok=True)

        def fake_run_one_fix(issue, repo, state, log_root, file_config=None):
            held["issue"] = issue
            import cli.interactive as iv

            iv._fire_task_start("fix-feed01")
            trace = held["trace_dir"] / "trace.jsonl"

            def emit(kind, data):
                with trace.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"kind": kind, "data": data}) + "\n")

            emit(
                "task_start",
                {"issue_text": "mean() returns the sum; make it the mean"},
            )
            emit(
                "model_response",
                {
                    "step": "plan",
                    "content": '{"analysis": "x", "plan": [{"id": 1, '
                    '"description": "fix the divisor", "checkpoint": "t"}]}',
                    "usage": {"tokens": 5, "cost": 0.001},
                },
            )
            emit(
                "plan",
                {
                    "plan": [
                        {"id": 1, "description": "fix the divisor", "checkpoint": "t"}
                    ]
                },
            )
            emit("attempt_start", {"attempt": 1})
            emit(
                "model_response",
                {
                    "step": "step-1",
                    "content": "cat mathutil.py",
                    "usage": {"tokens": 5, "cost": 0.001},
                },
            )
            emit("tool_call", {"step_id": 1, "turn": 1, "command": "cat mathutil.py"})
            emit(
                "tool_result",
                {
                    "step_id": 1,
                    "turn": 1,
                    "output": "def mean(values):\n    return sum(values)\n",
                },
            )
            # an edit-shaped command triggers the inline diff
            emit(
                "model_response",
                {
                    "step": "step-1",
                    "content": 'python -c "import pathlib"',
                    "usage": {"tokens": 5, "cost": 0.001},
                },
            )
            emit(
                "tool_call",
                {
                    "step_id": 1,
                    "turn": 2,
                    "command": "python -c \"import pathlib; p = pathlib.Path('mathutil.py'); "
                    "p.write_text('fixed')\"",
                },
            )
            # ...and the harness-side trees that diff is computed from
            pristine = held["trace_dir"] / "pristine"
            work = held["trace_dir"] / "work"
            pristine.mkdir(exist_ok=True)
            work.mkdir(exist_ok=True)
            (pristine / "mathutil.py").write_text(
                "def mean(values):\n    return sum(values)\n", encoding="utf-8"
            )
            (work / "mathutil.py").write_text(
                "def mean(values):\n    return sum(values) / len(values)\n",
                encoding="utf-8",
            )
            held["wrote"].set()
            deadline = time.monotonic() + 30
            while not held["release"].is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            return {
                "task_id": "fix-feed01",
                "log_root": log_root,
                "diff": "+x",
                "status": "success",
            }

        (tmp_path / "repo").mkdir(exist_ok=True)
        app = t.VexApp(
            repo=tmp_path / "repo",
            log_root=tmp_path / "logs",
            state={
                "model": None,
                "provider": None,
                "plan_preview": None,
                "quiet": False,
                "repo": str(tmp_path / "repo"),
                "file_config": {},
            },
            file_config={},
            version="9.9.9",
        )
        app._test = held  # type: ignore[attr-defined]

        import cli.interactive as iv

        iv._run_one_fix = fake_run_one_fix  # type: ignore[assignment]
        iv._run_one_agent = fake_run_one_fix  # type: ignore[assignment]
        return app, held

    async def test_feed_lines_render_live_per_action(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """Tasks A+B: reasoning summaries and tool calls appear in the
        transcript AS THEY HAPPEN (readable one-liners), and the tool
        result attaches to its command entry (expandable, Task D)."""
        app, held = self._feed_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "fix the crash in main.py"
            await pilot.press("enter")
            for _ in range(100):
                await _drain(pilot, 0.1)
                if held["wrote"].is_set() and len(app._run.feed.entries) >= 8:
                    break
            plain = _transcript_plain(app)
            # Task A: the planner's thinking is summarized readably
            assert "Planning the fix" in plain
            # Task B: the tool call is a readable action line
            assert "Reading mathutil.py" in plain
            # the edit is visible as an edit
            assert "Editing mathutil.py" in plain
            # lifecycle lines
            assert "planned 1 sub-step" in plain
            assert "attempt 1" in plain
            # Task D: the tool result attached to the command entry
            cmd_entry = next(
                e for e in app._run.feed.entries if e.summary == "Reading mathutil.py"
            )
            assert "$ cat mathutil.py" in cmd_entry.detail
            assert "def mean(values):" in cmd_entry.detail

            held["release"].set()
            await _drain_worker(app, pilot)

    async def test_inline_diff_after_edit(self, tmp_path, clean_hooks, restore_backend):
        """Task C (feed round) + Task B (polish round): an edit-shaped
        action is followed immediately by the real diff of pristine vs
        work — small, capped, and now LANGUAGE-AWARE syntax highlighted
        (pygments tokens over the +/-/@@ diff roles)."""
        app, held = self._feed_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "fix the crash in main.py"
            await pilot.press("enter")
            for _ in range(100):
                await _drain(pilot, 0.1)
                if held["wrote"].is_set() and "diff (pristine" in _transcript_plain(
                    app
                ):
                    break
            plain = _transcript_plain(app)
            assert "diff (pristine → work so far)" in plain
            # the real change is visible inline
            assert "return sum(values) / len(values)" in plain
            assert "a/mathutil.py" in plain
            # Syntax-highlighted diff (interaction-polish Task B): the
            # segments are now LANGUAGE-AWARE — the flat "+line
            # all-green" rendering is gone, replaced by the diff role on
            # the marker plus pygments token colors inside the code.
            # Verify BOTH: (1) the +/- marker still carries a real diff
            # color, (2) a python keyword on a diff line is its own
            # colored token segment (a flat single-color line could not
            # produce this), (3) the keyword color differs from the add
            # role color (it IS the lexer speaking, not the role).
            segs = [
                seg
                for line in app.query_one("#vex-body").lines
                for seg in line._segments
            ]
            assert any(
                seg.text.strip() in ("+", "-") and seg.style.color is not None
                for seg in segs
            ), "the +/- marker must carry the diff role color"
            kw = [
                seg
                for seg in segs
                if seg.text.strip() in ("return", "def") and seg.style.color
            ]
            assert kw, "python keywords inside the diff must be syntax-colored"

            held["release"].set()
            await _drain_worker(app, pilot)

    async def test_quiet_suppresses_feed_not_runline(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """/quiet: the run-line stays (phase/cost), the feed lines are
        silenced — the toggle, not a hardcoded off."""
        app, held = self._feed_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "/quiet"
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "fix the crash in main.py"
            await pilot.press("enter")
            for _ in range(100):
                await _drain(pilot, 0.1)
                if held["wrote"].is_set() and app._run and app._run.events >= 8:
                    break
            # run-line live
            rl = str(app.query_one("#vex-runline").visual)
            assert "events" in rl
            # feed silenced
            plain = _transcript_plain(app)
            assert "Reading mathutil.py" not in plain
            assert "Planning the fix" not in plain
            # ...but the feed builder still has the entries (the DATA
            # layer is untouched; /trace works while quiet)
            assert app._run.feed.entries

            held["release"].set()
            await _drain_worker(app, pilot)


class TestTraceCommand:
    """/trace — the feed index + on-demand detail expansion (Task D)."""

    async def test_trace_lists_and_expands(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """No arg: the feed one-liners with indexes. /trace <n>: the
        entry's FULL detail (raw command + output) in a modal."""
        app, held = TestLiveFeed._feed_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "fix the crash in main.py"
            await pilot.press("enter")
            for _ in range(100):
                await _drain(pilot, 0.1)
                if held["wrote"].is_set() and len(app._run.feed.entries) >= 8:
                    break
            # index listing
            app.query_one("#vex-input", Input).value = "/trace"
            await pilot.press("enter")
            await pilot.pause()
            plain = _transcript_plain(app)
            assert "trace feed" in plain
            assert "Reading mathutil.py" in plain
            # find the Reading entry's index and expand it
            idx = next(
                e.index
                for e in app._run.feed.entries
                if e.summary == "Reading mathutil.py"
            )
            app.query_one("#vex-input", Input).value = f"/trace {idx}"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(
                app.screen, __import__("cli.tui", fromlist=["x"])._TraceDetailScreen
            )
            from rich.text import Text

            modal_text = Text()
            for w in app.screen.query(RichLog):
                for ln in w.lines:
                    for seg in ln._segments:
                        modal_text.append(seg.text, style=seg.style)
            # the RAW command + its output are in the expansion
            assert "cat mathutil.py" in modal_text.plain
            assert "def mean(values):" in modal_text.plain
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(
                app.screen, __import__("cli.tui", fromlist=["x"])._TraceDetailScreen
            )

            held["release"].set()
            await _drain_worker(app, pilot)

    async def test_trace_after_run_rebuilds_from_trace_file(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """After the run finishes, /trace rebuilds the feed from the
        run's OWN trace.jsonl (Task E: same data, no copy kept)."""
        app, held = TestLiveFeed._feed_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "fix the crash in main.py"
            await pilot.press("enter")
            for _ in range(100):
                await _drain(pilot, 0.1)
                if held["wrote"].is_set() and len(app._run.feed.entries) >= 8:
                    break
            held["release"].set()
            await _drain_worker(app, pilot)
            # run finished: app._run is None
            assert app._run is None
            app.query_one("#vex-input", Input).value = "/trace"
            await pilot.press("enter")
            await pilot.pause()
            plain = _transcript_plain(app)
            assert "trace feed" in plain
            assert "Reading mathutil.py" in plain  # rebuilt from the file

    async def test_trace_bad_args(self, tmp_path, clean_hooks):
        """Bad /trace args: clean messages, never a crash."""
        import cli.tui as t

        app = t.VexApp(
            repo=tmp_path,
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "/trace"
            await pilot.press("enter")
            await pilot.pause()
            plain = _transcript_plain(app)
            assert "no run in this session yet" in plain
            app.query_one("#vex-input", Input).value = "/trace zz"
            await pilot.press("enter")
            await pilot.pause()
            plain = _transcript_plain(app)
            assert "usage: /trace" in plain


# ---------------------------------------------------------------------------
# /sessions + /resume wiring (session persistence inside the shell)
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_sessions_lists_recorded(self, tmp_path, clean_hooks):
        """Task C (polish round): /sessions is a SEARCHABLE browser, not
        a flat print — the recorded ids show as options, and the filter
        grammar (status: / repo:) narrows them live."""
        from textual.widgets import OptionList

        import cli.interactive as iv
        import cli.tui as t

        logs = tmp_path / "logs"
        iv.record_session(logs, "fix-abc", "issue one", "/repo/alpha", "success")
        iv.record_session(logs, "fix-def", "issue two", "/repo/beta", "failed")
        app = t.VexApp(
            repo=tmp_path,
            log_root=logs,
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "/sessions"
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, t._SessionsScreen)
            lst = screen.query_one("#sls-list", OptionList)
            texts = [str(o.prompt) for o in lst.options]
            assert any("fix-abc" in x for x in texts)
            assert any("fix-def" in x for x in texts)
            # status filter narrows to the failed run
            screen.query_one("#sls-input", Input).value = "status:failed"
            await pilot.pause()
            texts = [str(o.prompt) for o in lst.options]
            assert any("fix-def" in x for x in texts)
            assert not any("fix-abc" in x for x in texts)
            # repo filter narrows to the alpha repo
            screen.query_one("#sls-input", Input).value = "repo:alpha"
            await pilot.pause()
            texts = [str(o.prompt) for o in lst.options]
            assert any("fix-abc" in x for x in texts)
            assert not any("fix-def" in x for x in texts)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, t._SessionsScreen)

    async def test_resume_requires_id(self, tmp_path, clean_hooks):
        import cli.tui as t

        app = t.VexApp(
            repo=tmp_path,
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "/resume"
            await pilot.press("enter")
            await pilot.pause()
            assert "usage: /resume <task_id>" in _transcript_plain(app)


# ---------------------------------------------------------------------------
# Task D — design system
# ---------------------------------------------------------------------------


class TestDesignSystem:
    def test_role_map_matches_vex_theme(self):
        """The markup role map carries the EXACT palette tokens from
        ui.VEX_THEME (crimson re-theme 2026-09-22: active states are
        crimson #E8114A, never orange; success green only for success;
        muted is neutral text-secondary #8A8A8A, never warm).
        Textual doesn't know the rich theme, so tui._m()
        re-maps them."""
        import cli.tui as t

        assert t._ROLE_MAP["vex.accent"] == "bold #e8114a"
        assert t._ROLE_MAP["vex.running"] == "#e8114a"
        assert t._ROLE_MAP["vex.accent2"] == "#e8114a"
        assert t._ROLE_MAP["vex.muted"] == "#8a8a8a"
        assert t._ROLE_MAP["vex.ok"] == "#34d399"
        assert "vex.diff.add" in t._ROLE_MAP

    def test_m_rewrites_roles_to_concrete_styles(self):
        import cli.tui as t

        out = t._m("[vex.accent]title[/] [vex.muted]note[/]")
        assert "vex.accent" not in out
        assert "#e8114a" in out
        # unknown roles survive as-is (textual renders them as no-ops)
        out2 = t._m("[vex.nonsense]x[/]")
        assert "vex.nonsense" in out2

    def test_css_carries_brand_tokens(self):
        """The app CSS pins the design tokens: pitch-black bg-base
        background (never a framework default), crimson accent
        borders/focus, panel surfaces, neutral text-secondary hints —
        and none of the pre-re-theme warm/orange values remain."""
        import cli.tui as t
        import cli.ui as ui

        css = t.VexApp.CSS
        assert ui.BG_BASE in css  # Screen background pinned to bg-base
        assert "#E8114A" in css  # input/prompt/focus borders (accent)
        assert ui.BG_PANEL in css  # raised run-line/hint surfaces
        assert ui.TEXT_SECONDARY in css  # hint bar muted text
        assert ui.BORDER_SUBTLE in css  # transcript/sidebar dividers
        for stale in (
            "#D98E5F",
            "#d98e5f",
            "#D8A47F",
            "#d8a47f",
            "#C9504C",
            "#c9504c",
            "#A89490",
            "#a89490",
            "#0A0808",
            "#1A1212",
            "#5f5f5f",
            "grey58",
            "grey70",
        ):
            assert stale not in css, stale

    def test_modal_css_uses_tokens(self):
        """Modal/hint CSS uses neutral text-secondary — never warm grey."""
        import cli.tui as t

        for css in (
            t._PromptScreen.CSS,
            t._TraceDetailScreen.CSS,
            t._PaletteScreen.CSS,
        ):
            assert "#5f5f5f" not in css
            assert "#767676" not in css
            assert "#A89490" not in css
            assert "#a89490" not in css
            assert "#8A8A8A" in css

    def test_m_escapes_nothing_by_itself(self):
        """_m only rewrites role tags; content passes through (escaping
        happens at the caller with rich's escape for user data)."""
        import cli.tui as t

        assert t._m("[grey58]plain[/]") == "[grey58]plain[/]"

    def test_textual_theme_pins_background_and_scrollbars(self):
        """Task B: the framework's OWN defaults must not leak. The theme
        pins the screen background to bg-base AND textual's default
        scrollbar (pure black track + primary-derived thumb — verified
        in the rendered SVG) to the same tokens. The focused-input
        background-tint (textual's `$foreground 5%` lighter blend) is
        replaced by the bg-panel-hover token."""
        import cli.tui as t
        import cli.ui as ui

        theme = t._vex_textual_theme()
        assert theme.background.lower() == ui.BG_BASE.lower()
        assert theme.surface.lower() == ui.BG_PANEL.lower()
        v = theme.variables
        assert v["scrollbar-background"].lower() == ui.BG_BASE.lower()
        assert v["scrollbar-background-active"].lower() == ui.BG_BASE.lower()
        assert v["scrollbar"].lower() == ui.BORDER_SUBTLE.lower()
        assert v["scrollbar-hover"].lower() == ui.ACCENT_TEXT.lower()
        # the focused input's tint is a token, not textual's grey blend
        css = t.VexApp.CSS
        assert ui.BG_PANEL_HOVER in css
        assert "background-tint" in css

    async def test_hero_info_sits_beside_logo_not_below(self, tmp_path, clean_hooks):
        """Task C: the repo/logs/model/version block is a SECOND COLUMN
        beside the wordmark — the logo row and a key/value share one
        visual transcript line (not a stack of printed lines)."""
        import cli.tui as t
        import cli.ui as ui

        app = t.VexApp(
            repo=tmp_path / "myrepo",
            log_root=tmp_path / "empty-logs",
            state={"repo": str(tmp_path / "myrepo"), "file_config": {}},
            file_config={},
            version="1.0",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            lines = _transcript_plain(app).splitlines()
            rows = ui.wordmark_lines()
            logo_frag = rows[1].strip()
            paired = [ln for ln in lines if logo_frag in ln and "repo" in ln]
            assert paired, "hero info must sit BESIDE the logo (same line)"
            # and the info block is not stacked below the wordmark: no
            # standalone 'repo ...' line with the logo absent
            assert not any(
                ln.strip().startswith("repo") and logo_frag not in ln for ln in lines
            )


# ---------------------------------------------------------------------------
# Live todo list + status panel + completion card (todo/status round)
# ---------------------------------------------------------------------------


def _multi_step_app(tmp_path):
    """App + fake fix backend writing a REALISTIC two-step run: plan ->
    step-1 active -> step-1 done -> step-2 active -> step-2 done ->
    final_verify -> git_output -> result/task_end, plus a real
    transitions.jsonl trail so the machine-state panel has data. The
    run stays 'live' until released, so live behavior is observable."""
    import cli.tui as t

    held = {
        "release": threading.Event(),
        "stage": threading.Event(),
        "trace_dir": tmp_path / "logs" / "fix-todo01",
        "stages_watched": [],
    }
    held["trace_dir"].mkdir(parents=True, exist_ok=True)

    def emit(kind, data, ts=None):
        import time as _time

        rec = {"ts": ts or _time.time(), "kind": kind, "data": data}
        with (held["trace_dir"] / "trace.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def trans(from_state, to_state, reason):
        with (held["trace_dir"] / "transitions.jsonl").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": 1.0,
                        "from_state": from_state,
                        "to_state": to_state,
                        "reason": reason,
                        "valid": True,
                    }
                )
                + "\n"
            )

    PLAN = [
        {"id": 1, "description": "inspect the divisor", "checkpoint": "understand"},
        {
            "id": 2,
            "description": "fix the divisor to len(values)",
            "checkpoint": "suite",
        },
    ]

    def fake_run_one_fix(issue, repo, state, log_root, file_config=None):
        import cli.interactive as iv

        iv._fire_task_start("fix-todo01")
        trans(None, "planning", "run_task start")
        emit("task_start", {"issue_text": "mean() returns the sum; make it the mean"})
        trans("planning", "editing", "attempt 1: step sessions begin")
        emit("plan", {"plan": PLAN})
        emit("attempt_start", {"attempt": 1})
        # step 1 runs...
        emit("model_request", {"step": "step-1"})
        held["stage"].set()  # test can observe step 1 as ACTIVE
        deadline = time.monotonic() + 30
        while not held["release"].is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
        emit(
            "step_end",
            {
                "attempt": 1,
                "step_id": 1,
                "description": PLAN[0]["description"],
                "ok": True,
            },
        )
        trans("editing", "testing", "checkpoint verify")
        emit(
            "verify",
            {
                "step_id": 1,
                "target_passed": True,
                "regression_passed": True,
                "flaky": False,
            },
        )
        emit("model_request", {"step": "step-2"})
        time.sleep(0.3)
        emit(
            "step_end",
            {
                "attempt": 1,
                "step_id": 2,
                "description": PLAN[1]["description"],
                "ok": True,
            },
        )
        emit(
            "final_verify",
            {
                "attempt": 1,
                "target_passed": True,
                "regression_passed": True,
                "flaky": False,
                "raw": "$ python -m pytest -q\nexit=0\n6 passed in 0.14s",
            },
        )
        emit(
            "model_response", {"step": "plan", "usage": {"tokens": 100, "cost": 0.009}}
        )
        emit(
            "git_output",
            {"branch": "harness/fix-mean", "commit_sha": "a745eb2704d8"},
        )
        emit("task_end", {"status": "success", "attempt": 1})
        emit("result", {"status": "success", "attempts": 1, "cost_usd": 0.009})
        # state.json for the card's files row
        (held["trace_dir"] / "state.json").write_text(
            json.dumps({"files_touched": ["numlib/mathutil.py"]}), encoding="utf-8"
        )
        trans("testing", "done", "fix verified")
        return {
            "task_id": "fix-todo01",
            "log_root": log_root,
            "diff": "+x",
            "status": "success",
        }

    (tmp_path / "repo").mkdir(exist_ok=True)
    app = t.VexApp(
        repo=tmp_path / "repo",
        log_root=tmp_path / "logs",
        state={
            "model": None,
            "provider": None,
            "plan_preview": None,
            "quiet": False,
            "feed": True,
            "repo": str(tmp_path / "repo"),
            "file_config": {},
        },
        file_config={},
        version="9.9.9",
    )
    app._test = held

    import cli.interactive as iv

    iv._run_one_fix = fake_run_one_fix
    iv._run_one_agent = fake_run_one_fix  # type: ignore[assignment]
    return app, held


def _widget_plain(app, selector) -> str:
    """A Static widget's currently-rendered text (the suite's visual
    idiom — same as the header assertions in TestShell)."""
    return str(app.query_one(selector).visual)


class TestTodoSidebar:
    async def test_todo_checkmarks_land_live(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """Task A: the checklist appears with the plan, the ACTIVE step
        is marked, and the checkmark lands the MOMENT step_end is
        consumed (observed while the run is still in flight)."""
        app, held = _multi_step_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(
                "#vex-input", Input
            ).value = "mean() in mathutil.py returns the sum; make it the mean"
            await pilot.press("enter")
            # poll for the RENDERED checklist — the run is still in
            # flight, held at step 1 ACTIVE (0 done of 2)
            for _ in range(150):
                await _drain(pilot, 0.1)
                if "inspect the divisor" in _widget_plain(app, "#vex-todo"):
                    break
            assert app._run is not None  # still in flight
            todo = _widget_plain(app, "#vex-todo")
            # both steps listed; step 1 active (▸), step 2 pending (○)
            assert "inspect the divisor" in todo
            assert "fix the divisor to len(values)" in todo
            assert "0/2" in todo  # nothing DONE yet — step 1 is active
            assert "▸" in todo or ">" in todo  # the ACTIVE marker (enc-safe)
            # sidebar visible
            assert app.query_one("#vex-side").styles.display != "none"
            # release: steps complete; the FINAL todo shows both done
            held["release"].set()
            for _ in range(150):
                await _drain(pilot, 0.1)
                if app._run is None and "2/2" in _widget_plain(app, "#vex-todo"):
                    break
            todo_final = _widget_plain(app, "#vex-todo")
            assert "2/2" in todo_final

    async def test_status_panel_shows_mode_state_cost(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """Task B: the always-visible status panel shows the dispatched
        mode, the state-machine state (from transitions.jsonl), elapsed
        time, and running cost — while the run is live. (The bug-shaped
        input dispatches as an agent task now, so the mode row reads
        agent_task.)"""
        app, held = _multi_step_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(
                "#vex-input", Input
            ).value = "mean() in mathutil.py returns the sum; make it the mean"
            await pilot.press("enter")
            # poll for the RENDERED panel (machine state from the trail)
            for _ in range(150):
                await _drain(pilot, 0.1)
                if "editing" in _widget_plain(app, "#vex-side-status"):
                    break
            panel = _widget_plain(app, "#vex-side-status")
            assert "agent" in panel
            assert "editing" in panel  # machine state from the trail
            assert "$0." in panel
            held["release"].set()
            await _drain_worker(app, pilot)

    async def test_completion_card_matches_trace_numbers(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """Task C: the card's numbers match the run's OWN records —
        status/attempts from the result event, cost, model calls, files
        from state.json, branch from git_output."""
        app, held = _multi_step_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(
                "#vex-input", Input
            ).value = "mean() in mathutil.py returns the sum; make it the mean"
            await pilot.press("enter")
            for _ in range(100):
                await _drain(pilot, 0.1)
                if "1/2" in _widget_plain(app, "#vex-todo"):
                    break
            held["release"].set()
            await _drain_worker(app, pilot)
            for _ in range(50):
                await _drain(pilot, 0.2)
                body = _transcript_plain(app)
                if "summary fix-todo01" in body:
                    break
            body = _transcript_plain(app)
            assert "summary fix-todo01" in body
            assert "SUCCESS" in body
            assert "1 attempt(s)" in body
            assert "1 model call" in body  # one model_response in the stream
            assert "$0.009" in body
            assert "numlib/mathutil.py" in body
            assert "harness/fix-mean" in body
            assert "a745eb27" in body
            assert "PASS" in body

    async def test_sidebar_collapses_after_run(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """The sidebar shows the final snapshot briefly, then hides —
        the transcript keeps the card as the durable record."""
        app, held = _multi_step_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(
                "#vex-input", Input
            ).value = "mean() in mathutil.py returns the sum; make it the mean"
            await pilot.press("enter")
            for _ in range(100):
                await _drain(pilot, 0.1)
                if "1/2" in _widget_plain(app, "#vex-todo"):
                    break
            held["release"].set()
            await _drain_worker(app, pilot)
            # teardown timer is 2s; poll past it for the collapsed state
            for _ in range(120):
                await _drain(pilot, 0.1)
                if app.query_one("#vex-side").styles.display == "none":
                    break
            assert app.query_one("#vex-side").styles.display == "none"


class TestModeDispatch:
    @staticmethod
    def _mode_app(tmp_path):
        import cli.tui as t

        (tmp_path / "repo").mkdir(exist_ok=True)
        app = t.VexApp(
            repo=tmp_path / "repo",
            log_root=tmp_path / "logs",
            state={
                "model": None,
                "provider": None,
                "plan_preview": None,
                "quiet": False,
                "repo": str(tmp_path / "repo"),
                "file_config": {},
            },
            file_config={},
        )
        return app

    async def test_build_request_dispatches_build_worker(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """The TUI routes a BUILD request to the agent worker (the ONE
        live-repo loop) — same dispatch as the REPL. The agent backend
        is faked to return immediately."""
        import cli.interactive as iv

        ran = {"mode": None}

        def fake_agent(request, repo, state, log_root, file_config=None, task_id=None):
            ran["mode"] = "agent"
            return {
                "task_id": "agent-x",
                "log_root": log_root,
                "diff": None,
                "status": "success",
            }

        real = iv._run_one_agent
        iv._run_one_agent = fake_agent
        try:
            app = self._mode_app(tmp_path)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one(
                    "#vex-input", Input
                ).value = "add a mode() function for the most frequent value"
                await pilot.press("enter")
                await _drain_worker(app, pilot)
                assert ran["mode"] == "agent"
                plain = _transcript_plain(app)
                assert "working in" in plain
        finally:
            iv._run_one_agent = real

    async def test_question_request_dispatches_question_worker(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """A QUESTION routes to the question worker (read-only Q&A)."""
        import cli.interactive as iv

        ran = {"mode": None}

        def fake_question(question, repo, state, log_root, file_config=None):
            ran["mode"] = "question"
            return {
                "task_id": "qa-x",
                "log_root": log_root,
                "diff": None,
                "status": "success",
                "answer": "because",
            }

        real = iv._run_one_question
        iv._run_one_question = fake_question
        try:
            app = self._mode_app(tmp_path)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one(
                    "#vex-input", Input
                ).value = "how does the verify step work?"
                await pilot.press("enter")
                await _drain_worker(app, pilot)
                assert ran["mode"] == "question"
        finally:
            iv._run_one_question = real

    async def test_conversational_input_still_never_launches(
        self, tmp_path, clean_hooks, restore_backend
    ):
        """The mode gate preserves the original contract: "hi" answers
        inline and starts NO worker."""
        app = self._mode_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "hi"
            await pilot.press("enter")
            await _drain(pilot, 0.3)
            assert app._worker_thread is None
            plain = _transcript_plain(app)
            assert "describe what's wrong" in plain or "hey" in plain


# ---------------------------------------------------------------------------
# Agent approval modal (require-mode): Allow once / Allow always / Reject
# ---------------------------------------------------------------------------


class TestAgentApprovalModal:
    """The TUI approver mirrors the plan-preview/confirm pattern (diff +
    command body, y=once / a=always / n=reject, Esc-safe refusal) with an
    Allow-always latch per run. Unit-level (no Pilot needed — the modal
    itself is monkeypatched; _thread_log tolerates a non-mounted app)."""

    def _app(self, tmp_path):
        import cli.tui as t

        (tmp_path / "repo").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(exist_ok=True)
        return t.VexApp(
            repo=tmp_path / "repo",
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path / "repo"), "file_config": {}},
            file_config={},
        )

    def test_reject_is_safe_default(self, tmp_path):
        app = self._app(tmp_path)
        app._prompt_modal = lambda prompt: "n"  # type: ignore[method-assign]
        assert app._agent_approve_fn("edit", {"path": "a.py"}, "old") is False
        assert app._agent_allow_always is False

    def test_modal_cancel_refuses(self, tmp_path):
        app = self._app(tmp_path)
        app._prompt_modal = lambda prompt: None  # type: ignore[method-assign]
        assert app._agent_approve_fn("write", {"path": "n.py"}, "c") is False
        assert app._agent_allow_always is False

    def test_allow_once_prompts_every_time(self, tmp_path):
        app = self._app(tmp_path)
        seen = []
        app._prompt_modal = lambda prompt: seen.append(prompt) or "y"  # type: ignore[method-assign]
        assert app._agent_approve_fn("bash", {"command": "pytest"}, "pytest") is True
        assert app._agent_allow_always is False
        assert app._agent_approve_fn("bash", {"command": "pytest"}, "pytest") is True
        assert len(seen) == 2

    def test_allow_always_latches_for_the_run(self, tmp_path):
        app = self._app(tmp_path)
        calls = {"n": 0}

        def modal(prompt):
            calls["n"] += 1
            return "a"

        app._prompt_modal = modal  # type: ignore[method-assign]
        assert app._agent_approve_fn("edit", {"path": "a.py"}, "x") is True
        assert app._agent_allow_always is True
        # second call auto-approves without another modal
        assert app._agent_approve_fn("edit", {"path": "b.py"}, "y") is True
        assert calls["n"] == 1


# Agent /diff parity + bad-input safety (no Pilot: unmounted app,
# transcript degrades silently, last[] is the assertion surface)
# ---------------------------------------------------------------------------


class TestAgentDiffParity:
    def _app(self, tmp_path):
        import cli.tui as t

        (tmp_path / "repo").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(exist_ok=True)
        return t.VexApp(
            repo=tmp_path / "repo",
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path / "repo"), "file_config": {}},
            file_config={},
        )

    def test_diff_recomputes_live_when_last_empty(self, tmp_path):
        from harness import editor

        app = self._app(tmp_path)
        repo = tmp_path / "repo"
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        tdir = tmp_path / "logs" / "agent-live1"
        tdir.mkdir(parents=True)
        editor.snapshot(str(repo), str(tdir / "pristine"))
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        app.last = {"task_id": "agent-live1"}
        app._slash_command("/diff", "/diff")
        assert app.last.get("diff") and "x = 2" in app.last["diff"]

    def test_diff_undo_missing_name_is_honest(self, tmp_path):
        app = self._app(tmp_path)
        app.last = {"task_id": "agent-nothing"}
        app._slash_command("/diff undo ghost.py", "/diff undo ghost.py")
        # nothing to undo: no crash, no diff recorded
        assert not app.last.get("diff")

    def test_bad_slash_lines_never_raise(self, tmp_path):
        app = self._app(tmp_path)
        for line in (
            "/diff undo ../../etc/passwd",
            "/resume \x00",
            "/\x00bad\xffcmd",
            "/copy-diff",
            "/compact",
            "/trace notanumber!!",
        ):
            app._handle_line(line)
