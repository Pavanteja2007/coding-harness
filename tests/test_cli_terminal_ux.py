"""Terminal 06 motion, feedback, accessibility, and output regressions."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from cli import ui
from cli.tui import NeoApp, _RunState
from cli.tui_components import EventFeed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Use Textual's asyncio event loop for the terminal tests."""
    return "asyncio"


def _app(tmp_path, name: str = "repo", **kwargs: Any) -> NeoApp:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    return NeoApp(
        repo=repo,
        log_root=tmp_path / f"{name}-logs",
        state={"repo": str(repo), "file_config": {}, **kwargs.pop("state", {})},
        file_config=kwargs.pop("file_config", {}),
        **kwargs,
    )


def test_reduced_motion_removes_decorative_animation_but_keeps_state() -> None:
    run = _RunState("motion-task", mode="agent_task")
    run.consume({"kind": "model_request", "data": {"step": "step-1"}})

    animated = run.line(4, reduced_motion=False)
    reduced = run.line(4, reduced_motion=True)

    assert "model" in animated.lower() or "thinking" in animated.lower()
    assert ui.JOKES[0] in animated
    assert ui.JOKES[0] not in reduced
    assert "thinking" in reduced.lower() or "model" in reduced.lower()


def test_repl_live_monitor_does_not_animate_in_reduced_motion(
    tmp_path, monkeypatch
) -> None:
    from cli import interactive

    monkeypatch.setattr(ui, "motion_enabled", lambda: False)
    monitor = interactive.LiveMonitor("static-task", tmp_path / "logs")
    monitor.start(quiet=False)
    try:
        assert monitor._status is None
    finally:
        monitor.stop()


def test_pending_tool_state_is_cancellable_and_has_text_alternative() -> None:
    run = _RunState("pending-task", mode="agent_task")
    run.consume(
        {
            "kind": "tool_call",
            "data": {"tool": "bash", "command": "pytest -q", "turn": 1},
        }
    )
    pending = run.line(0)
    assert "tool pending" in pending
    assert "pytest" in pending
    assert "/cancel" in pending

    run.pending_since = time.monotonic() - 2
    assert "tool still running" in run.line(0)
    run.consume({"kind": "tool_result", "data": {"ok": True}})
    assert "tool pending" not in run.line(0)


def test_structured_stream_delta_updates_a_stable_phase() -> None:
    run = _RunState("stream-task", mode="agent_task")
    run.consume({"kind": "model_request", "data": {"step": "step-1"}})
    run.consume({"kind": "model_delta", "data": {"delta": "partial"}})
    line = run.line(0, reduced_motion=True)
    assert "streaming response" in line
    assert run.stream_text == "partial"


def test_input_echo_precedes_slow_session_persistence(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path)
    order: list[str] = []
    app.conversation = {"history": [], "turns": []}
    monkeypatch.setattr(app, "transcript", lambda value: order.append("echo"))

    def slow_save(*_args, **_kwargs):
        order.append("save")
        time.sleep(0.03)

    import cli.session as session

    monkeypatch.setattr(session, "save_session", slow_save)
    monkeypatch.setattr(app, "_handle_line", lambda _line: order.append("dispatch"))
    event = SimpleNamespace(value="hello", input=SimpleNamespace(value=""))
    app.on_input_submitted(event)

    assert order == ["echo", "save", "dispatch"]
    metrics = app.metrics_snapshot()
    # The submit echo has its OWN metric. It used to be recorded as
    # `input_ack_ms`, which meant the 100 ms keystroke gate was being
    # measured with submitted lines — a p95 over a handful of samples.
    assert metrics["submit_echo_ms"]["samples"] == 1
    assert metrics["input_ack_ms"]["samples"] == 0
    assert metrics["command_response_ms"]["samples"] == 1


async def test_input_acknowledgement_is_measured_per_keystroke(
    tmp_path, monkeypatch
) -> None:
    """The gate is about the character appearing, not the line submitting.

    A p95 over submitted lines cannot represent "the user pressed a key
    and nothing happened for 400 ms", which is the failure the 100 ms
    budget exists to catch.
    """
    from textual.widgets import Input

    app = _app(tmp_path)
    # Dispatch is stubbed: this test is about the composer, and a
    # submitted line would reach the real kernel behind it.
    monkeypatch.setattr(app, "_handle_line", lambda _line: None)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for character in "hello":
            await pilot.press(character)
            await pilot.pause()
        metrics = app.metrics_snapshot()
        assert metrics["input_ack_ms"]["samples"] >= 5, metrics["input_ack_ms"]
        assert metrics["input_ack_ms"]["p95"] is not None
        assert metrics["input_ack_ms"]["p95"] < 100.0, metrics["input_ack_ms"]
        assert metrics["input_ack_ms"]["max"] < 100.0, metrics["input_ack_ms"]
        assert app.query_one("#neo-input", Input).value == "hello"
        # Submitting clears the composer, which IS a visible value change
        # and therefore a real acknowledgement sample — so the count grows
        # by one and the submit's own echo is counted separately.
        await pilot.press("enter")
        await pilot.pause()
        after = app.metrics_snapshot()
        assert after["submit_echo_ms"]["samples"] == 1
        assert (
            after["input_ack_ms"]["samples"] == metrics["input_ack_ms"]["samples"] + 1
        ), after["input_ack_ms"]


async def test_transcript_and_diff_redact_secrets_before_render(tmp_path) -> None:
    from rich.text import Text

    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.transcript("token=sk-abcdefghijklmnopqrstuvwx")
        app.transcript(Text("bearer abcdefghijklmnop"))
        for line in ui.diff_render_lines("+api_key=supersecretvalue123"):
            app.transcript(line)
        await pilot.pause()
        body = app.query_one("#neo-body")
        plain = "\n".join(
            "".join(segment.text for segment in row._segments) for row in body.lines
        )
        assert "sk-abcdefghijklmnopqrstuvwx" not in plain
        assert "abcdefghijklmnop" not in plain
        assert "supersecretvalue123" not in plain
        assert "[REDACTED_SECRET]" in plain


async def test_status_has_plain_text_assistance_and_focus_css(tmp_path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        status = app.query_one("#neo-status")
        assert str(status.visual).strip() == "idle"
        assert "Status: idle" in str(getattr(status, "tooltip", ""))
        assert "OptionList:focus" in NeoApp.CSS
        assert "Input:focus" in NeoApp.CSS
        await pilot.press("tab")
        assert app.focused is not None


async def test_transcript_is_bounded_and_selectable(tmp_path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        feed = app.query_one("#neo-body", EventFeed)
        assert feed.max_lines == 1200
        assert feed.ALLOW_SELECT is True
        for index in range(1250):
            feed.write(f"bounded line {index}")
        await pilot.pause()
        assert len(feed.lines) <= 1200
        assert str(
            feed.lines[-1].plain if hasattr(feed.lines[-1], "plain") else feed.lines[-1]
        )


def test_copy_falls_back_to_the_structured_diff(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path)
    copied: list[str] = []

    import cli.session as session

    monkeypatch.setattr(
        session,
        "copy_text_to_clipboard",
        lambda value: copied.append(value) or True,
    )
    app.last = {
        "task_id": "copy-task",
        "diff": "--- a/a.py\n+++ b/a.py\n@@\n-old\n+new\n",
    }
    app._slash_command("/copy-diff", "/copy-diff")
    assert copied and "new" in copied[0]


def test_journal_tail_supports_legacy_two_argument_callbacks(tmp_path) -> None:
    import cli.tui as tui

    task_dir = tmp_path / "logs" / "legacy-tail"
    task_dir.mkdir(parents=True)
    trace = task_dir / "trace.jsonl"
    trace.write_text(
        json.dumps({"kind": "task_start", "data": {"mode": "agent"}}) + "\n",
        encoding="utf-8",
    )
    stop = threading.Event()
    run = _RunState("legacy-tail", mode="agent_task")
    seen: list[int] = []

    def callback(state, _entries):
        seen.append(state.events)

    thread = threading.Thread(
        target=tui._tail_trace,
        args=("legacy-tail", tmp_path / "logs", run, stop, callback),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=2)
    assert seen and run.projection.snapshot()["events"] == 1


async def test_pending_skeleton_and_cancel_text_are_visible(tmp_path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._show_pending_run("working")
        await pilot.pause()
        assert "waiting for task journal" in str(app.query_one("#neo-runline").visual)
        app._interrupt_worker()
        await pilot.pause()
        assert "cancel requested" in str(app.query_one("#neo-runline").visual)


def test_plain_mode_maps_missing_roles_to_a_valid_textual_style(monkeypatch) -> None:
    from textual.content import Content

    import cli.tui as tui

    monkeypatch.setattr(tui, "_ROLE_MAP", {})
    markup = tui._m("[neo.muted]plain[/]")
    assert markup == "[none]plain[/]"
    assert Content.from_markup(markup).plain == "plain"


async def test_plain_mode_app_resize_keeps_textual_markup_valid(tmp_path) -> None:
    import cli.tui as tui
    from cli import theme

    previous = ui.active_tokens()
    try:
        app = _app(tmp_path, theme_depth=theme.ColorDepth.NONE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.query_one("#neo-brand").is_attached
    finally:
        ui.set_active_tokens(previous)
        tui._refresh_role_map()


def test_long_diff_summarizes_before_detail(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path)
    lines: list[str] = []
    monkeypatch.setattr(app, "transcript", lambda value: lines.append(str(value)))
    diff = "\n".join(f"+line {index}" for index in range(300))
    app._render_diff_value(diff)
    assert any("diff summary" in line and "240" in line for line in lines)
    assert len(lines) <= 242


def test_theme_and_terminal_fallbacks_cover_dumb_no_color_and_legacy(
    monkeypatch,
) -> None:
    import cli.tui as tui
    from cli import theme

    monkeypatch.setenv("TERM", "dumb")
    assert tui.can_run_tui() is False
    assert (
        theme.resolve_color_depth({"TERM": "dumb"}, is_tty=True)
        is theme.ColorDepth.NONE
    )

    class LegacyStream:
        encoding = "cp1252"

    monkeypatch.setattr(ui.sys, "stdout", LegacyStream())
    assert ui.is_legacy_encoding() is True
    assert ui._enc_ok("→") is False
    assert ui._glyph("→", "->") == "->"


# ---------------------------------------------------------------------------
# Terminal 06 round 2 — the three gaps the first pass left open:
# status ANNOUNCEMENTS, long-output PAGINATION, and TEXT ALTERNATIVES for
# the compact state codes. One test per required behaviour, named after
# the behaviour rather than the function that implements it.
# ---------------------------------------------------------------------------


# -- status announcements ---------------------------------------------------


def test_announcements_are_plain_words_with_no_glyph_or_color() -> None:
    """An announcement must survive a dumb terminal and a screen reader.

    Both read the plain character stream, so a sentence carrying a
    spinner frame or a color escape is read as punctuation or mojibake.
    """
    from cli import a11y

    for sentence in (
        a11y.announce_idle(),
        a11y.announce_queued(),
        a11y.announce_run_started("fix-abc123", "agent_task"),
        a11y.announce_phase("model: thinking", events=4),
        a11y.announce_tool_pending("running a command", "pytest -q", elapsed_s=9),
        a11y.announce_approval("write src/app.py", "src/app.py"),
        a11y.announce_cancel_requested(),
        a11y.announce_steered("focus on the parser first"),
        a11y.announce_finished("completed_unverified"),
        a11y.announce_failure("sandbox unavailable", next_steps=("/doctor",)),
        a11y.announce_paged("page 2 of 3", "p previous page"),
    ):
        assert sentence
        assert "\x1b" not in sentence, sentence
        assert "[" not in sentence and "]" not in sentence, sentence
        assert sentence == sentence.strip()
        assert "\n" not in sentence
        assert len(sentence) <= a11y.ANNOUNCEMENT_MAX_CHARS


def test_an_announcement_can_never_upgrade_an_unverified_run() -> None:
    """The honesty invariant, as a unit: no bare success word reads verified.

    This is the same defect class the repository keeps rediscovering one
    surface at a time (R2-G45, the R2-09 receipts, the support bundle).
    An announcement is just another surface, and the cheapest place to
    enforce the rule is the sentence itself.
    """
    from cli import a11y

    for bare in (
        "success",
        "done",
        "ok",
        "passed",
        "completed",
        "completed_unverified",
    ):
        sentence = a11y.announce_finished(bare)
        assert "verified by the test suite" not in sentence, bare
        assert "not verified" in sentence, bare
    verified = a11y.announce_finished("completed_verified", verified=True)
    assert "verified by the test suite" in verified
    for failed in ("error", "failed", "timeout", "cancelled"):
        sentence = a11y.announce_finished(failed)
        assert "Task failed" in sentence, failed
        assert "not verified" in sentence.lower(), failed


def test_the_announcement_gate_speaks_once_per_transition_not_per_repaint() -> None:
    from cli import a11y

    gate = a11y.AnnouncementGate()
    assert gate.offer("phase:planning", "Working: planning.") == "Working: planning."
    assert gate.offer("phase:planning", "Working: planning.") is None
    assert gate.offer("phase:editing", "Working: editing.") == "Working: editing."
    # A phase the user watched come and go is news again when it returns.
    assert gate.offer("phase:planning", "Working: planning.") == "Working: planning."
    # An empty sentence must not burn the key it was offered under: doing
    # so would suppress the real announcement that follows.
    assert gate.offer("phase:verify", "") is None
    assert gate.offer("phase:verify", "Working: verifying.") == "Working: verifying."
    gate.reset()
    assert gate.offer("phase:planning", "Working: planning.") == "Working: planning."


async def test_the_shell_publishes_announced_transitions_as_new_output(
    tmp_path,
) -> None:
    """The announcement region writes NEW bytes, not an in-place repaint.

    A status chip updated in place is invisible to a screen reader. The
    region's whole purpose is to be new output, so the test drives the
    real transitions and checks the transcript received the sentence.
    """
    app = _app(tmp_path)
    spoken: list[str] = []
    app.transcript = lambda value: spoken.append(str(value))  # type: ignore[method-assign]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        region = app.query_one("#neo-announce")
        assert region.styles.display == "none", "an idle shell must carry no blank band"
        assert app.announcement_text() == ""

        app._show_pending_run("working in")
        await pilot.pause()
        assert "no task id yet" in app.announcement_text()
        assert region.styles.display == "block"

        run = _RunState("fix-announce", mode="agent_task")
        run.consume({"kind": "model_request", "data": {"step": "step-1"}})
        app._run = run
        app._render_run()
        await pilot.pause()
        assert "Working" in app.announcement_text()
        # 125 Hz of the same phase must not produce 125 announcements.
        before = len(spoken)
        for _ in range(5):
            app._render_run()
        await pilot.pause()
        assert len(spoken) == before, "a repeated phase must not re-announce"

        run.cancel_requested = True
        app._render_run()
        await pilot.pause()
        assert "Cancel requested" in app.announcement_text()
        assert any("Cancel requested" in line for line in spoken)

        app._clear_announcement()
        await pilot.pause()
        assert app.announcement_text() == ""
        assert region.styles.display == "none"


async def test_the_announcement_region_hides_at_every_required_size(tmp_path) -> None:
    """80x24, 100x30, 120x36, and ultrawide all mount the region and it stays hidden."""
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        for width, height in ((80, 24), (100, 30), (120, 36), (200, 50)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            region = app.query_one("#neo-announce")
            assert region.is_attached
            assert app.announcement_text() == ""
            assert region.styles.display == "none"
            assert app.query_one("#neo-input").is_attached, (
                "the composer never disappears"
            )


def test_the_completion_announcement_uses_the_card_verdict_not_the_raw_status(
    tmp_path,
) -> None:
    """The announcement and the card must not be able to disagree.

    A bare `"success"` status with no verifier evidence renders the card
    as UNVERIFIED. If the announcement read the raw field it would say
    "success, not verified" while the card said something else; routing
    both through `runview.effective_terminal_status` makes that
    structurally impossible.
    """
    from cli import runview, tui_components

    def status_for(raw: str, evidence: list) -> str:
        return runview.effective_terminal_status(raw, evidence)

    app = _app(tmp_path)
    clean = [{"target_passed": True, "regression_passed": True, "flaky": False}]
    for index, (raw, evidence, expect_verified) in enumerate(
        (
            ("completed_verified", clean, True),
            ("completed_verified", [{"target_passed": True}], False),
            ("completed_unverified", [], False),
            ("success", [], False),
            ("error", [], False),
        )
    ):
        # A DISTINCT task id per case, because that is what a real second
        # run looks like: the gate suppresses CONSECUTIVE duplicates, and
        # two different runs that reduce to the same sentence SHOULD be
        # both spoken (they are separate outcomes a user was told about).
        announced = app._announce_outcome(
            f"fix-ann-{index}",
            {
                "status": raw,
                "verification_evidence": evidence,
                "files": ["a.py"],
                "elapsed_s": 12,
                "cost_usd": 0.0,
                "model_calls": 2,
            },
        )
        assert announced, (raw, evidence)
        assert ("verified by the test suite" in announced) is expect_verified, raw
        if not expect_verified and not runview.status_is_completed(
            status_for(raw, evidence)
        ):
            assert "Task failed" in announced or "not verified" in announced, raw
    # A recorded zero cost is announced as nothing, not "$0.000000".
    assert "Cost" not in announced or "0.000000" not in announced
    # The ResultCard is the surface the announcement is a text mirror of,
    # so the two must agree on an unverified bare-"success" run. The card
    # renders a `rich.console.Group`, so it needs a Console to become text.
    import io

    from rich.console import Console

    console = Console(
        file=io.StringIO(), width=100, no_color=True, legacy_windows=False
    )
    console.print(
        tui_components.ResultCard(
            task_id="fix-ann",
            mode="fix",
            facts={"status": "success", "verification_evidence": []},
        ).render()
    )
    rendered = console.file.getvalue()
    assert "UNVERIFIED" in rendered or "unverified" in rendered.lower(), rendered
    assert "verified" not in rendered.lower().replace("unverified", ""), rendered


# -- long-output pagination ------------------------------------------------


def test_the_pager_names_the_omitted_remainder_instead_of_hiding_it() -> None:
    from cli import a11y

    page = a11y.paginate(list(range(1000)), size=24)
    label = page.label()
    assert "lines 1-24 of 1000" in label
    assert "976 more below" in label
    assert page.has_more is True
    assert page.has_previous is False
    assert a11y.omitted_note(page) == (
        "... 976 more line(s) not shown; open the detail view for the rest."
    )
    assert a11y.omitted_note(a11y.Page(1000, page.last_index, 24)) == ""
    # A single page says so, so "page 1 of 1" never appears.
    assert a11y.paginate(range(5), size=24).label() == "lines 1-5 of 5 (all of it)"
    # Round-tripping through advance/rewind returns the same page, so a
    # user who pages forward and back lands where they started.
    assert page.advance().rewind() == page


def test_the_pager_is_total_and_never_wraps() -> None:
    from cli import a11y

    empty = a11y.paginate(None)
    assert empty.total == 0 and empty.pages == 1 and empty.label() == "no lines"
    assert empty.advance() is empty and empty.rewind() is empty
    assert a11y.page_lines("", empty) == []
    # A hostile size clamps to 1 rather than producing an empty page.
    assert a11y.Page(10, 0, 0).size == 1
    assert a11y.Page(10, 99, 24).index == a11y.Page(10, 0, 24).last_index
    one = a11y.Page(3, 0, 24)
    assert one.advance() is one, "a single page must not wrap to itself-around"
    assert a11y.long_output_policy(range(1000), page_size=24)["paged"] is True
    assert a11y.long_output_policy(range(5), page_size=24)["paged"] is False
    assert a11y.omitted_note(a11y.Page(0, 0, 24)) == ""


async def test_the_detail_view_pages_the_whole_body_and_reaches_the_end(
    tmp_path,
) -> None:
    """The regression: the body was `splitlines()[:400]` with no way past it.

    120 lines is enough to prove paging; the assertion that matters is
    that the LAST line is reachable, because that is what the old slice
    made impossible.
    """
    from rich.text import Text

    from cli import a11y
    from cli.tui import _TraceDetailScreen

    app = _app(tmp_path)
    lines = [Text(f"line {index}") for index in range(120)]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = _TraceDetailScreen("detail", lines)
        app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()

        def body_text() -> str:
            return "".join(
                segment.text
                for line in screen.query_one("#trace-body").lines
                for segment in line._segments
            )

        assert "line 0" in body_text()
        assert "line 119" not in body_text()
        assert "lines 1-24 of 120" in str(screen.query_one("#trace-page").visual)

        await pilot.press("n")
        await pilot.pause()
        assert "line 24" in body_text()
        await pilot.press("G")
        await pilot.pause()
        assert "line 119" in body_text(), "the end of the body must be reachable"
        assert "line 0" not in body_text()
        await pilot.press("n")
        await pilot.pause()
        assert "line 119" in body_text(), "paging must not wrap past the end"
        # The label counts POSITIONS (1-based inclusive); the body holds
        # the matching elements, so page 4 is positions 73-96 = the
        # zero-indexed `line 72` .. `line 95`. Both are checked, because a
        # label that disagreed with its own body is exactly the silent
        # truncation this screen was rewritten to remove.
        await pilot.press("p")
        await pilot.pause()
        assert "line 119" not in body_text(), "p must actually leave the last page"
        assert "lines 73-96 of 120" in str(screen.query_one("#trace-page").visual)
        assert "line 72" in body_text() and "line 95" in body_text()
        assert "line 96" not in body_text()
        await pilot.press("g")
        await pilot.pause()
        assert "line 0" in body_text()
        assert screen.page().has_more is True
        assert a11y.DEFAULT_PAGE_SIZE == 24
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, _TraceDetailScreen)


def test_no_long_output_surface_truncates_silently() -> None:
    """A guard, not a rendering test: no `[:NNN]` slice of a detail body.

    The old code hid four thousand lines behind a slice with nothing on
    screen saying so. Reading the source is the only way to catch the
    NEXT one appearing, and it is a two-line assertion.
    """
    from pathlib import Path

    source = Path("cli/tui.py").read_text(encoding="utf-8")
    for banned in (
        "splitlines()[:400]",
        "splitlines()[:200]",
        "splitlines()[:100]",
    ):
        assert banned not in source, banned
    from cli import a11y

    assert a11y.OMITTED_MARKER.startswith("... ")


# -- text alternatives for glyphs / state codes ----------------------------


def test_every_state_code_has_a_word_and_an_unknown_code_is_never_silent() -> None:
    from cli import a11y

    assert a11y.render_code("S") == "staged"
    assert a11y.render_code("U") == "unstaged"
    assert a11y.render_code("V") == "verified"
    assert a11y.render_code("?") == "not verified"
    assert a11y.render_code("U?") == "unstaged, not verified"
    assert a11y.render_code("Vcp") == "verified, in a checkpoint"
    # An unrecognized mark displayed as nothing is indistinguishable from
    # no state at all, so it is named instead.
    assert a11y.render_code("Z") == "code Z"
    assert a11y.render_code("") == ""
    legend = a11y.legend_lines()
    assert len(legend) == len(a11y.FILE_STATE_LEGEND)
    assert any(line.startswith("? = ") for line in legend)


def test_file_state_words_match_the_compact_code_the_rail_renders() -> None:
    from cli import a11y

    for staged, verified, checkpoint in (
        (True, True, True),
        (False, False, False),
        (True, False, True),
    ):
        code = a11y.file_state_codes(
            staged=staged, verified=verified, checkpoint=checkpoint
        )
        spoken = a11y.describe_file_state(
            "src/app.py", staged=staged, verified=verified, checkpoint=checkpoint
        )
        # The expansion of the rendered code must contain every word the
        # worded form claims, so the compact and expanded views cannot
        # describe different states.
        for word in a11y.render_code(code).split(", "):
            assert word in spoken, (code, word, spoken)
        assert "src/app.py" in spoken


def test_glyph_text_alternatives_cover_the_shipped_glyph_table() -> None:
    from cli import a11y

    # Every glyph `ui.GLYPHS` can render must have a declared word, so
    # the encoding fallback and the text alternative agree.
    for name, glyph in ui.GLYPHS.items():
        assert a11y.glyph_text(glyph), f"{name} -> {glyph!r} has no text alternative"
    assert a11y.glyph_text("\u2714") == "ok"
    assert a11y.glyph_text("OK") == "ok", "the ASCII fallback needs a word too"
    assert a11y.glyph_text("\u0000nonexistent") == ""
    assert "running" in a11y.describe_status("running", "*")
    assert a11y.describe_status("", "") == "Status: unknown."


async def test_the_context_rail_ships_the_legend_with_the_codes(tmp_path) -> None:
    """`[U?]` next to a filename is decoration without the legend beside it.

    The legend moved OUT of the files block and into its own
    `#neo-context-legend` block in the round-2 layout round. It used to be
    five rows inside the files block, which meant it outranked the
    diagnostics in the rail's row budget: at 200x50 DIAGNOSTICS was squeezed
    to a bare `+4 more` with its heading gone, because five rows of static
    prose about letter codes had been given priority over three real
    diagnostics. The legend is still in the rail, still adjacent to the
    codes, and still all five entries — it is just not pretending to be a
    file.
    """
    from cli.tui_components import ContextPanel

    app = _app(tmp_path)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        panel = app.query_one("#neo-context", ContextPanel)
        panel.update_snapshot(
            {
                "task_id": "fix-a11y",
                "changed_files": ["src/app.py"],
                "file_changes": [
                    {
                        "path": "src/app.py",
                        "staged": False,
                        "verified": False,
                        "checkpoint_ids": ["cp-1"],
                    }
                ],
            }
        )
        await pilot.pause()
        rendered = str(app.query_one("#neo-context-files").visual)
        assert "[U?cp]" in rendered, rendered
        legend = str(app.query_one("#neo-context-legend").visual)
        assert "not verified = not verified" in legend or "not verified" in legend
        assert "in a checkpoint" in legend, legend
        assert app.query_one("#neo-context-legend").styles.display == "block"


# -- keyboard navigation order ---------------------------------------------


async def test_the_declared_focus_order_is_complete_and_reaches_the_composer(
    tmp_path,
) -> None:
    """Keyboard navigation order is a declared contract, not an accident.

    `cli.a11y.FOCUS_ORDER` is the product's own enumeration; the report
    resolves it against the live mount tree, so a control that becomes
    unreachable is a value the shell reports rather than a claim in a
    docstring.
    """
    from cli import a11y

    assert a11y.FOCUS_ORDER == ("neo-body", "neo-input")
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        report = app.focus_order()
        assert report["complete"] is True, report
        assert report["missing"] == []
        assert report["order"] == list(a11y.FOCUS_ORDER)
        # The composer is reachable by tab, and it is the mount focus.
        assert app.focused is not None
        assert getattr(app.focused, "id", "") == "neo-input"
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None, "tab must land on a focusable control"
        assert app.focus_order()["complete"] is True
        # A missing target is reported, not silently tolerated.
        assert a11y.focus_order_report(["neo-body"])["complete"] is False
        assert a11y.focus_order_report(["neo-body"], modal=True)["scope"] == "modal"
        assert app.focus_order(modal=True)["declared"] == list(a11y.MODAL_FOCUS_ORDER)


async def test_focus_is_visible_on_the_transcript_not_just_bold(tmp_path) -> None:
    """Focus-visible must be a visible cue, and the transcript had none.

    `Input:focus, OptionList:focus { text-style: bold }` never matched
    the transcript, so a keyboard user tabbing into it got bold text on a
    black background — the near-invisible default the prompt forbids.
    """
    app = _app(tmp_path)
    assert "#neo-body:focus" in NeoApp.CSS
    assert "$neo-focus" in NeoApp.CSS
    assert "#neo-announce" in NeoApp.CSS
    # No reference to a variable the theme does not define: an
    # UnresolvedVariableError has taken this whole app down before.
    from cli import ui as _ui

    defined = set(_ui.textual_theme_variables())
    import re

    referenced = set(re.findall(r"\$(neo-[a-z0-9-]+)", NeoApp.CSS))
    assert referenced, "the app CSS must use theme variables"
    assert referenced <= defined, sorted(referenced - defined)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        body = app.query_one("#neo-body")
        body.focus()
        await pilot.pause()
        assert app.focused is body
        assert body.has_focus is True
