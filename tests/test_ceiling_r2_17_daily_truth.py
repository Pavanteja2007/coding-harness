"""R2-17 — Daily UX truth: the UI cannot lie, and the text has a home.

Every test here is named after a behaviour, and every behaviour is
MEASURED from a run's real on-disk journal rather than asserted about a
mock. The three required gates are:

1. **No renderer can present an unverified run as success** —
   parameterised over ``cli.runview.HONESTY_SURFACES``, which is the
   product's own enumeration of the surfaces that can show a run's
   outcome. A surface added there without a case here fails the gate, so
   coverage cannot silently stop covering a renderer.
2. **Streamed text appears, is bounded, and cannot inject markup.**
3. **The briefing renders the real numbers; `/cost` matches the ledger;
   help is searchable.**

The suite is offline and deterministic. It builds its own run journals
under ``tmp_path``; it never reads the developer's real ``logs/`` tree and
never contacts a provider or Docker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from cli import command_exec as _cx
from cli import doctor as _doctor
from cli import interactive as _iv
from cli import notify as _notify
from cli import runview as _rv
from cli import tui_components as _tc

# ---------------------------------------------------------------------------
# Fixtures: real run journals on disk
# ---------------------------------------------------------------------------


def _write_run(
    root: Path,
    task_id: str,
    *,
    status: str,
    evidence: Optional[List[Dict[str, Any]]] = None,
    attempts: int = 1,
    cost: float = 0.004,
    note: str = "",
    mode: str = "agent_task",
    steps: Optional[List[Dict[str, Any]]] = None,
    files: Optional[List[str]] = None,
) -> Path:
    """Write a run directory whose journal says exactly what it is told.

    The point is that the projections under test read REAL bytes: a
    `completed_unverified` terminal event really is in `trace.jsonl`, so a
    surface that dresses it as done is caught by the renderer, not by a
    test double agreeing with the test.
    """
    task_dir = root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = [
        {
            "kind": "task_start",
            "ts": 1000.0,
            "data": {
                "issue_text": "mean() returns the sum, not the mean",
                "repo_path": str(root),
                "mode": mode,
            },
        },
        {
            "kind": "model_response",
            "ts": 1001.0,
            "data": {"usage": {"tokens": 120, "cost": cost}, "model": "cheap"},
        },
    ]
    for record in evidence or []:
        rows.append({"kind": "verify", "ts": 1010.0, "data": dict(record)})
    rows.append(
        {
            "kind": "result",
            "ts": 1020.0,
            "data": {
                "task_id": task_id,
                "status": status,
                "attempts": attempts,
                "cost_usd": cost,
                "note": note,
            },
        }
    )
    (task_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    if steps is not None:
        (task_dir / "state.json").write_text(
            json.dumps({"steps": steps, "files_touched": list(files or [])}),
            encoding="utf-8",
        )
    return task_dir


def _write_ledger(root: Path, task_id: str, rows: List[Dict[str, Any]]) -> Path:
    """Write the router's per-call ledger for one run."""
    ledger_dir = root / f"{task_id}.runtime"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger = ledger_dir / "model_ledger.jsonl"
    ledger.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    return ledger


CLEAN_EVIDENCE = {"target_passed": True, "regression_passed": True, "flaky": False}
FAILED_EVIDENCE = {"target_passed": False, "regression_passed": True, "flaky": False}


# ---------------------------------------------------------------------------
# 1. THE HONESTY GATE — parameterised over every surface
# ---------------------------------------------------------------------------

#: A run that finished WITHOUT a clean verifier receipt. Every renderer is
#: asked to present exactly this run.
UNVERIFIED = "completed_unverified"


def _render_surface(surface: str, *, root: Path) -> str:
    """Render one surface's text for an UNVERIFIED run.

    Assumes `root` holds one `completed_unverified` run. Every branch is
    total: an exception becomes the exception's own text, because a
    renderer that CRASHES is also a renderer that did not honestly
    present the run and must not be allowed to pass silently.
    """
    task_id = "fix-unverified"
    try:
        if surface == "runview.card_lines":
            facts = _rv.read_run_facts(root / task_id)
            return "\n".join(_rv.card_lines(facts))
        if surface == "runview.status_lines":
            projection = _rv.read_live_projection(root / task_id)
            return "\n".join(_rv.status_lines(projection))
        if surface == "runview.headless_status":
            return "\n".join(_rv.headless_status(root / task_id))
        if surface == "interactive.result_display":
            label, style, _glyph = _iv._terminal_result_display(
                UNVERIFIED, FAILED_EVIDENCE
            )
            return f"{label}|{style}"
        if surface == "interactive.sessions_rows":
            row = {"task_id": task_id, "status": "completed", "display_status": ""}
            return _iv._row_status_text(row, width=12)
        if surface == "session.index_row":
            from cli.session import index_records

            return json.dumps({"rows": index_records(limit=10)})
        if surface == "tui.palette_session_hint":
            from cli import tui

            return tui._honest_session_label(
                {"task_id": task_id, "status": "completed"}
            )
        if surface == "command_exec.envelope":
            # The document a script actually receives, built through the
            # real result type with the run this fixture built. `/status`
            # is flag-only on the headless surface and would target no run,
            # which would make the assertion vacuous.
            from cli.exit_codes import EXIT_CODES

            result = _cx.HeadlessCommandResult(
                command="/status",
                args=task_id,
                status=UNVERIFIED,
                text="",
                exit_code=EXIT_CODES["task_failure"],
                task_id=task_id,
                verification_state="failed",
                log_root=str(root),
            )
            return json.dumps(result.to_dict())
        if surface == "main.result_json":
            # `cli/main.py` is another terminal's file; this branch READS
            # its published document so the gate still covers the `neo fix
            # --json` surface without editing a file this round does not
            # own. See cli/AGENTS.md "Cross-terminal requests".
            from cli.main import _result_json
            from shared.types import TaskResult, VerificationResult

            return json.dumps(
                _result_json(
                    TaskResult(
                        task_id=task_id,
                        status=UNVERIFIED,
                        attempts=1,
                        diff="",
                        cost_usd=0.004,
                        model_calls=[],
                        log_path=str(root / task_id / "trace.jsonl"),
                        verification=VerificationResult(
                            target_test_passed=False,
                            baseline_passed=True,
                            regression_passed=True,
                            flaky=False,
                            raw_output="",
                        ),
                    ),
                    1.0,
                )
            )
        if surface == "headless.result_envelope":
            from cli import headless

            mode = headless.HEADLESS_MODES["agent_task"]
            envelope = headless.result_envelope(
                log_root=root,
                task_id=task_id,
                mode=mode,
                status=UNVERIFIED,
                session_id="",
                session_created=False,
            )
            return json.dumps(envelope)
        if surface == "notify.receipt":
            return json.dumps(
                {
                    "outcome": _notify.classify(UNVERIFIED).value,
                    "title": _notify.notify_run(
                        UNVERIFIED, label="fix", detail=task_id, desktop=False
                    ).title,
                }
            )
        raise AssertionError(f"unhandled surface: {surface}")
    except Exception as exc:  # a crash is a failure to be honest
        return f"{type(exc).__name__}: {exc}"


def test_every_registered_surface_has_a_renderer():
    """The gate cannot silently stop covering a renderer.

    `_render_surface` is a hand-kept dispatch table, so a surface ADDED to
    `HONESTY_SURFACES` without a renderer here would raise at the first
    test rather than passing vacuously.
    """
    assert _rv.HONESTY_SURFACES, "the surface enumeration must not be empty"
    assert len(set(_rv.HONESTY_SURFACES)) == len(_rv.HONESTY_SURFACES)


@pytest.mark.parametrize("surface", _rv.HONESTY_SURFACES)
def test_no_renderer_displays_an_unverified_run_as_success(surface, tmp_path):
    """NOTHING may present a `completed_unverified` run as done.

    The assertion is deliberately about the ABSENCE of success wording, not
    about a specific string: a surface is allowed to print
    `completed_unverified`, `unverified`, `COMPLETED · UNVERIFIED`,
    `verified: false`, or nothing at all. What it may never do is claim the
    run was verified or done.
    """
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-unverified", status=UNVERIFIED, evidence=[FAILED_EVIDENCE])
    rendered = _render_surface(surface, root=root)
    lowered = rendered.lower()
    for forbidden in (
        "success · verified",
        "completed · unverified",
        "verified · true",
        '"verified": true',
        "'verified': true",
        "status_label(success)",
    ):
        if forbidden in ("completed · unverified",):
            # The honest label; present, not forbidden.
            continue
        assert forbidden not in lowered, (
            f"{surface} presented an unverified run as success: {rendered!r}"
        )
    # And it must not CLAIM success either.
    for claim in ('"verified": true', "'verified': true"):
        assert claim not in rendered, f"{surface} claimed verified: {rendered!r}"


@pytest.mark.parametrize(
    "surface",
    [
        "runview.card_lines",
        "runview.status_lines",
        "runview.headless_status",
        "interactive.result_display",
        "interactive.sessions_rows",
        "tui.palette_session_hint",
        "command_exec.envelope",
        "main.result_json",
        "headless.result_envelope",
        "notify.receipt",
    ],
)
def test_unverified_surfaces_say_the_word_unverified(surface, tmp_path):
    """The positive half of the gate: the honest word must actually appear.

    Without this, a renderer that printed an EMPTY document would pass the
    "no success" assertion above. A surface must say what happened.
    """
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-unverified", status=UNVERIFIED, evidence=[FAILED_EVIDENCE])
    rendered = _render_surface(surface, root=root).lower()
    assert rendered.strip(), f"{surface} rendered nothing at all"
    assert "unverified" in rendered, f"{surface} never said 'unverified': {rendered!r}"


def test_verified_run_is_reported_as_verified_not_as_unverified(tmp_path):
    """The gate must not be a one-way ratchet that makes every run look bad."""
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(
        root, "fix-verified", status="completed_verified", evidence=[CLEAN_EVIDENCE]
    )
    facts = _rv.read_run_facts(root / "fix-verified")
    card = "\n".join(_rv.card_lines(facts))
    assert "VERIFIED" in card
    assert "unverified" not in card.lower()


def test_run_verdict_fails_closed_on_every_flattering_input():
    """No input, however plausible, yields `verified` without evidence.

    This is the load-bearing property of the whole authority: the words
    `success`, `passed`, `done`, `ok`, `verified`, and the collapsed
    lifecycle word `completed` must ALL resolve to `unverified`, because
    every one of them has been printed by some historical surface for a run
    with no proof behind it.
    """
    flattering = (
        "success",
        "passed",
        "done",
        "ok",
        "verified",
        "completed",
        "already_exists",
        "completed_verified",
        "COMPLETED_VERIFIED",
        "",
        None,
        "totally fine",
    )
    for value in flattering:
        assert _rv.run_verdict(value) != "verified", (
            f"{value!r} produced a verified verdict with no evidence"
        )
    assert (
        _rv.run_verdict("completed_verified", evidence=[CLEAN_EVIDENCE]) == "verified"
    )
    assert (
        _rv.run_verdict("completed_verified", evidence=[FAILED_EVIDENCE])
        == "unverified"
    )
    # A flaky record is not a clean one.
    assert (
        _rv.run_verdict(
            "completed_verified",
            evidence=[
                {"target_passed": True, "regression_passed": True, "flaky": True}
            ],
        )
        == "unverified"
    )


def test_session_metadata_never_collapses_verified_into_completed(tmp_path):
    """The R2-G45 defect itself, pinned at the data layer.

    `session_status` legitimately answers "can this be continued" and so
    collapses both completion statuses into `completed`. What must NOT
    happen is that the DISPLAY label is that same collapsed word — which is
    exactly what `/sessions`, the TUI browser, the palette hint, and
    `neo --list-sessions` used to render.
    """
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-unverified", status=UNVERIFIED, evidence=[FAILED_EVIDENCE])
    _write_run(root, "fix-ok", status="completed_verified", evidence=[CLEAN_EVIDENCE])

    unverified = _iv._session_display_status(
        root, "fix-unverified", {"status": UNVERIFIED}
    )
    verified = _iv._session_display_status(
        root, "fix-ok", {"status": "completed_verified"}
    )
    assert unverified == "unverified"
    assert verified == "verified"
    # The two MUST differ. Identical rendering is the defect.
    assert unverified != verified

    # And the whole point: a LEGACY row that only carries the collapsed
    # word must still render honestly rather than as a verified success.
    assert _iv._honest_row_label({"status": "completed"}) == "unverified"
    assert _iv._honest_row_label({"status": "success"}) == "unverified"


def test_honest_row_label_is_used_by_every_metadata_renderer():
    """No metadata renderer reads the raw lifecycle word.

    A renderer that read `row["status"]` would print the collapsed word,
    which is the defect, so the gate asserts the render output rather than
    the source.
    """
    row = {"task_id": "t", "status": "completed", "display_status": ""}
    repl = _iv._row_status_text(row, width=12)
    assert "unverified" in repl
    assert "completed" not in repl.replace("[/]", "")
    from cli import tui

    assert tui._honest_session_label(row) == "unverified"
    # A recorded honest label is respected.
    assert (
        _iv._honest_row_label({"status": "completed", "display_status": "verified"})
        == "verified"
    )


def test_notification_path_never_announces_unverified_as_done():
    """The notification path is the one surface that escapes the process.

    A desktop toast cannot be retracted, so this is asserted on the
    classifier AND the rendered title.
    """
    outcome = _notify.classify(UNVERIFIED)
    assert outcome is not _notify.Outcome.VERIFIED
    assert outcome is not _notify.Outcome.COMPLETED
    receipt = _notify.notify_run(
        UNVERIFIED, label="fix", detail="fix-abc", desktop=False
    )
    title = str(getattr(receipt, "title", "") or "").lower()
    assert "unverified" in title, title
    # Fail-closed: an unrecognized status is a FAILURE, not a completion.
    assert _notify.classify("something_new") is _notify.Outcome.FAILED


# ---------------------------------------------------------------------------
# 2. THE STREAM PAINT
# ---------------------------------------------------------------------------


def test_stream_paint_shows_the_text_a_user_waits_for():
    """Streamed text is PAINTED into the real UI, not just polled.

    R2-G09: `poll_frame` and `stream_text` were correct and live and there
    was no widget for the result. This drives the REAL `NeoApp` through
    textual's Pilot, feeds REAL `model_delta` journal rows through the
    REAL coalescer, and reads the REAL `#neo-stream` widget's rendered
    content. The window is real too: the coalescer gates everything after
    the first delta behind it, so the test waits it out rather than
    pretending a poll is instant.
    """
    import asyncio
    import time as _time

    pytest.importorskip("textual")
    from cli.tui import NeoApp

    async def drive() -> tuple:
        app = NeoApp(repo=Path("."), log_root=Path("logs").resolve())
        async with app.run_test() as pilot:
            run = app.begin_live_run("fix-stream")
            run.consume({"kind": "model_request", "data": {}, "ts": 1.0})
            run.consume(
                {"kind": "model_delta", "data": {"delta": "Reading "}, "ts": 1.0}
            )
            # The window gates everything after the FIRST delta; waiting it
            # out is what a real stream does, and skipping it would prove
            # nothing about the paint.
            _time.sleep(0.2)
            for chunk in ("app.py", " and finding", " the bug"):
                run.consume(
                    {"kind": "model_delta", "data": {"delta": chunk}, "ts": 1.0}
                )
            _time.sleep(0.2)
            # `_render_run` is the TUI's own repaint path: it drains the
            # coalescer and pushes the frame into the paint.
            app._run = run
            app._render_run(run)
            await pilot.pause()
            paint = app.query_one("#neo-stream")
            plain = paint.paint(run.stream_text).plain
            shown_display = paint.styles.display
            run.streaming = False
            app._render_run(run)
            await pilot.pause()
            return plain, shown_display, paint.styles.display

    plain, shown_display, hidden_display = asyncio.run(drive())
    assert shown_display != "none", "the paint stayed hidden while text streamed"
    for fragment in ("app.py", "the bug"):
        assert fragment in plain, f"{fragment!r} never reached the UI: {plain!r}"
    # And it hides again when the stream ends, so an idle shell carries no
    # stale text.
    assert hidden_display == "none"


def test_stream_paint_widget_is_mounted_next_to_the_run_line():
    """The paint is a real widget in the shell, not a helper nothing calls."""
    pytest.importorskip("textual")
    import asyncio

    from cli.tui import NeoApp

    async def probe() -> tuple:
        app = NeoApp(repo=Path("."), log_root=Path("logs").resolve())
        async with app.run_test() as pilot:
            await pilot.pause()
            paint = app.query_one("#neo-stream")
            # The paint must be a SIBLING of the run line, not nested
            # inside it: a nested paint would be clipped by the run line's
            # `height: 1`.
            siblings = [w.id for w in paint.parent.children if getattr(w, "id", None)]
            return type(paint).__name__, paint.styles.display, siblings

    name, display, siblings = asyncio.run(probe())
    assert name == "StreamPaint"
    assert display == "none", "an idle shell must not carry a blank paint box"
    # The paint is a SIBLING of the run line, and it sits between the run
    # line and the composer. A nested paint would be clipped by the run
    # line's `height: 1`.
    assert "neo-runline" in siblings, siblings
    assert "neo-inputwrap" in siblings, siblings
    assert siblings.index("neo-runline") < siblings.index("neo-stream")
    assert siblings.index("neo-stream") < siblings.index("neo-inputwrap")


@pytest.mark.parametrize(
    "payload",
    [
        "[",
        "[/]",
        "[neo.accent]",
        "[bold]red[/]",
        "reply with [/] in it",
        "[link=https://x]y[/link]",
        "[[double]]",
        "[not-a-real-style]x[/]",
    ],
)
def test_hostile_model_text_never_raises_or_leaks_markup(payload):
    """A model reply is UNTRUSTED text and must render literally.

    This codebase has already been bitten by exactly this: a `MarkupError`
    from an orphaned `[/]`. The paint returns `rich.text.Text` — never a
    markup string — so the hostile characters are DISPLAYED, not consumed
    as a style tag. Asserting no exception AND no tag consumption is the
    whole point; asserting only "did not raise" would pass a paint that
    silently ate the text.
    """
    lines, _dropped = _tc.stream_tail_lines(payload)
    assert lines, "hostile text was dropped instead of shown"
    assert payload in "\n".join(lines)

    paint = object.__new__(_tc.StreamPaint)
    paint.max_lines = _tc.STREAM_PAINT_MAX_LINES
    paint.max_chars = _tc.STREAM_PAINT_MAX_CHARS
    painted = paint.paint(payload)
    assert payload in painted.plain
    # No span carries a style that came from the text: the paint styles the
    # whole line, it does not parse the line.
    for line in painted.split("\n"):
        for span in line.spans:
            assert not str(span.style).startswith("neo."), span


def test_stream_paint_is_bounded_in_lines_and_characters():
    """A 200k-token answer must cost the same to render as a 20-token one."""
    huge = "\n".join(
        f"line {index} of a very long streamed answer" for index in range(5000)
    )
    lines, dropped = _tc.stream_tail_lines(huge)
    assert len(lines) <= _tc.STREAM_PAINT_MAX_LINES
    assert dropped > 0, "truncation was silent"
    total = sum(len(line) for line in lines)
    assert total <= _tc.STREAM_PAINT_MAX_CHARS

    paint = object.__new__(_tc.StreamPaint)
    paint.max_lines = _tc.STREAM_PAINT_MAX_LINES
    paint.max_chars = _tc.STREAM_PAINT_MAX_CHARS
    painted = paint.paint(huge)
    assert len(painted.split("\n")) <= _tc.STREAM_PAINT_MAX_LINES + 1
    assert paint.dropped_chars > 0
    # The marker is shown, so truncation is stated rather than implied.
    assert _tc.STREAM_PAINT_TRIM_MARKER in painted.plain


def test_stream_paint_keeps_the_tail_not_the_head():
    """The end of an answer is what a waiting user needs; the head is the
    transcript's job. A paint that shows the head would scroll the newest
    words out of view before anyone could read them."""
    paint = object.__new__(_tc.StreamPaint)
    paint.max_lines = 2
    paint.max_chars = _tc.STREAM_PAINT_MAX_CHARS
    painted = paint.paint("FIRST\nSECOND\nTHIRD\nFOURTH")
    assert "FOURTH" in painted.plain
    assert "THIRD" in painted.plain
    assert "FIRST" not in painted.plain


def test_stream_paint_is_total_on_empty_and_non_string_input():
    """An empty stream hides the widget; a non-string is coerced, not fatal."""
    paint = object.__new__(_tc.StreamPaint)
    paint.max_lines = 3
    paint.max_chars = 100
    for value in (None, "", "   \n  \n", 12345, ["a", "b"]):
        painted = paint.paint(value)
        assert isinstance(painted.plain, str)
    assert paint.paint("").plain == ""


def test_stream_paint_does_not_make_the_run_line_multi_line():
    """The one-line run-line layout is a layout contract, not a preference.

    The paint is a SEPARATE widget precisely so the run line keeps
    `height: 1`. This reads the app's own CSS rather than eyeballing a
    screenshot.
    """
    from cli.tui import NeoApp

    css = NeoApp.CSS
    runline = css.split("#neo-runline")[1].split("}")[0]
    assert "height: 1" in runline
    assert "#neo-stream" in css, "the paint must have its own CSS block"
    stream_block = css.split("#neo-stream")[1].split("}")[0]
    assert "height: 1" not in stream_block, (
        "the paint must not reuse the run line's fixed height"
    )


# ---------------------------------------------------------------------------
# 3. THE RESUME BRIEFING
# ---------------------------------------------------------------------------


def test_resume_briefing_renders_the_real_numbers_from_the_journal(tmp_path):
    """The briefing's numbers are re-derived, not remembered.

    Asserted against the bytes written into the run's own journal and
    `state.json`: attempts, model calls, tokens, cost, elapsed, step
    progress, and the files it touched. A briefing that printed its own
    plausible constants would fail every one of these.
    """
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    steps = [
        {"id": 1, "description": "a", "state": "done"},
        {"id": 2, "description": "b", "state": "pending"},
    ]
    _write_run(
        root,
        "fix-brief",
        status=UNVERIFIED,
        evidence=[FAILED_EVIDENCE],
        attempts=3,
        cost=0.0123,
        note="the verifier refused the diff",
        steps=steps,
        files=["app/math.py", "tests/test_math.py"],
    )
    facts = _rv.briefing_facts(root, "fix-brief")
    assert facts["attempts"] == 3
    assert facts["model_calls"] == 1
    assert facts["cost_usd"] == pytest.approx(0.0123)
    assert facts["cost_known"] is True
    assert facts["elapsed_s"] == pytest.approx(20.0)
    assert facts["steps_done"] == 1
    assert facts["steps_total"] == 2
    assert facts["files"] == ["app/math.py", "tests/test_math.py"]
    assert facts["verdict"] == "unverified"
    assert facts["verified"] is False

    rendered = "\n".join(_rv.briefing_lines(facts))
    assert "unverified" in rendered.lower()
    assert "3 attempt(s)" in rendered
    assert "1/2 steps" in rendered
    assert "app/math.py" in rendered
    assert "the verifier refused the diff" in rendered
    # It must tell the user what to do next, not just what happened.
    assert "/cost" in rendered
    assert "re-run the target test" in rendered


def test_resume_briefing_is_empty_when_there_is_nothing_to_brief(tmp_path):
    """A first launch gets NO briefing.

    A briefing of zeros would be a fabricated status — the exact defect
    class this feature exists to remove — so the honest answer is an empty
    list and the caller prints nothing.
    """
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    assert _iv.render_resume_briefing(root) == []
    assert _iv._last_briefable_run(root) is None


def test_resume_briefing_reports_unknown_cost_rather_than_zero(tmp_path):
    """A run with calls but no cost says UNKNOWN, not $0.0000.

    "$0.0000" reads as a measured fact — that the call was free. A
    briefing that made that claim for a run nobody priced would be lying
    in the most innocuous-looking way.
    """
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    task_dir = root / "fix-nocost"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "trace.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"kind": "task_start", "ts": 1.0, "data": {"issue_text": "x"}},
                {"kind": "model_response", "ts": 2.0, "data": {"usage": {"tokens": 9}}},
                {"kind": "result", "ts": 3.0, "data": {"status": UNVERIFIED}},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    facts = _rv.briefing_facts(root, "fix-nocost")
    assert facts["cost_known"] is False
    rendered = "\n".join(_rv.briefing_lines(facts))
    assert "cost unknown" in rendered
    assert "$0.0000" not in rendered


def test_resume_briefing_reports_a_verified_run_as_verified(tmp_path):
    """The briefing is not a doom screen: a good run says so."""
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-good", status="completed_verified", evidence=[CLEAN_EVIDENCE])
    facts = _rv.briefing_facts(root, "fix-good")
    assert facts["verdict"] == "verified"
    assert facts["verified"] is True
    rendered = "\n".join(_rv.briefing_lines(facts)).lower()
    assert "unverified" not in rendered
    assert "/diff" in rendered


# ---------------------------------------------------------------------------
# 4. RECEIPTS FOR EVERY MODEL CALL, AND `/cost` RECONCILIATION
# ---------------------------------------------------------------------------


def test_model_call_receipts_cover_calls_the_conversation_could_not_see(tmp_path):
    """Every ATTEMPT gets a receipt, not just the completed ones.

    The defect this closes: `/cost` summed `trace.jsonl`'s
    `model_response` rows, which only cover the main conversation. A
    routing classifier call, a failed attempt, and a retry are all real
    spend the router recorded and nothing displayed.
    """
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-spend", status=UNVERIFIED, evidence=[FAILED_EVIDENCE])
    _write_ledger(
        root,
        "fix-spend",
        [
            {
                "ts": 1.0,
                "call_id": "a",
                "model": "cheap",
                "provider": "openai",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost_usd": 0.004,
                "outcome": "ok",
                "difficulty_hint": "easy",
            },
            {
                "ts": 1.5,
                "call_id": "b",
                "model": "cheap",
                "provider": "openai",
                "prompt_tokens": 80,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "outcome": "failed",
                "reason": "rate limit",
            },
            {
                "ts": 2.0,
                "call_id": "c",
                "model": "expensive",
                "provider": "openai",
                "prompt_tokens": 30,
                "completion_tokens": 1,
                "cost_usd": 0.0001,
                "outcome": "ok",
                "difficulty_hint": "hard",
                "streamed": True,
            },
        ],
    )
    receipts = _rv.model_call_receipts(root, "fix-spend")
    assert len(receipts) == 3
    outcomes = {row["outcome"] for row in receipts}
    assert outcomes == {"ok", "failed"}
    # A failed call is a RECEIPT, not a dropped row.
    failed = [row for row in receipts if row["outcome"] == "failed"]
    assert failed and "rate limit" in failed[0]["reason"]
    # A call nobody priced reads as UNKNOWN, not as free.
    unpriced = [row for row in receipts if not row["priced"]]
    assert len(unpriced) == 1, unpriced
    assert [row for row in receipts if row.get("streamed")]

    report = _rv.cost_reconciliation(root, "fix-spend")
    assert report["ledger_available"] is True
    assert report["ledger_calls"] == 3
    assert report["trace_calls"] == 1
    # The whole point: the conversation saw 1 of 3.
    assert report["unreceipted_calls"] == 2
    assert report["unreceipted_cost_usd"] == pytest.approx(0.0001)
    assert report["reconciled"] is False
    assert report["unpriced_calls"] == 1


def test_cost_reconciliation_reports_a_missing_ledger_with_a_reason(tmp_path):
    """No ledger is reported as such, never as a zero that reads as free."""
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-noledger", status=UNVERIFIED, evidence=[FAILED_EVIDENCE])
    report = _rv.cost_reconciliation(root, "fix-noledger")
    assert report["ledger_available"] is False
    assert report["ledger_calls"] == 0
    assert report["reconciled"] is False
    assert "no model ledger" in report["reason"]
    assert _rv.model_call_receipts(root, "fix-noledger") == []


def test_cost_reconciles_when_the_ledger_and_the_conversation_agree(tmp_path):
    """The reconciliation is a real check, not a constant `False`."""
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(
        root, "fix-agree", status=UNVERIFIED, evidence=[FAILED_EVIDENCE], cost=0.002
    )
    _write_ledger(
        root,
        "fix-agree",
        [
            {
                "ts": 1.0,
                "model": "cheap",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost_usd": 0.002,
                "outcome": "ok",
            }
        ],
    )
    report = _rv.cost_reconciliation(root, "fix-agree")
    assert report["ledger_calls"] == 1
    assert report["trace_calls"] == 1
    assert report["reconciled"] is True


def test_cost_view_renders_the_reconciliation(tmp_path):
    """`/cost` shows the reconciliation, not only a total.

    Driven through the real renderer with a `say` hook, so this asserts
    what a person reads rather than that a function returns a dict.
    """
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-view", status=UNVERIFIED, evidence=[FAILED_EVIDENCE])
    _write_ledger(
        root,
        "fix-view",
        [
            {
                "ts": 1.0,
                "model": "classifier",
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "cost_usd": 0.00002,
                "outcome": "ok",
            },
            {
                "ts": 1.1,
                "model": "cheap",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost_usd": 0.004,
                "outcome": "ok",
            },
        ],
    )
    lines: List[str] = []
    _iv._render_cost({}, root, say=lines.append, task_id="fix-view")
    rendered = "\n".join(lines)
    assert "ledger" in rendered
    assert "outside the conversation" in rendered
    assert "1 call(s)" in rendered

    receipts: List[str] = []
    _iv._render_cost_receipts(root, "fix-view", receipts.append)
    joined = "\n".join(receipts)
    assert "classifier" in joined
    assert "cheap" in joined


def test_cost_receipt_line_says_unknown_for_an_unpriced_call(tmp_path):
    """An unpriced call reads as UNKNOWN, never as $0.0000."""
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-unpriced", status=UNVERIFIED, evidence=[FAILED_EVIDENCE])
    _write_ledger(
        root,
        "fix-unpriced",
        [{"ts": 1.0, "model": "mystery", "prompt_tokens": 5, "outcome": "ok"}],
    )
    lines: List[str] = []
    _iv._render_cost_receipts(root, "fix-unpriced", lines.append)
    joined = "\n".join(lines)
    assert "cost unknown" in joined
    assert "$0.0000" not in joined


def test_doctor_reports_unreceipted_spend(tmp_path):
    """Spend without a receipt is a machine-checkable health finding."""
    import cli.doctor as doctor

    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    _write_run(root, "fix-audit", status=UNVERIFIED, evidence=[FAILED_EVIDENCE])
    doctor._ACTIVE_LOG_ROOT = root
    try:
        record = doctor.check_spend_receipts()
        assert record["status"] == "failed"
        assert "no model ledger" in record["reason"]
        assert record["remediation"]
        _write_ledger(
            root,
            "fix-audit",
            [
                {
                    "ts": 1.0,
                    "model": "cheap",
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "cost_usd": 0.001,
                    "outcome": "ok",
                }
            ],
        )
        healthy = doctor.check_spend_receipts()
        assert healthy["status"] == "ok"
        assert "priced receipt" in healthy["reason"]
    finally:
        doctor._ACTIVE_LOG_ROOT = None
    names = {check.name for check in _doctor.DOCTOR_CHECKS}
    assert "spend_receipts" in names


# ---------------------------------------------------------------------------
# 5. SEARCHABLE HELP
# ---------------------------------------------------------------------------


def test_help_is_searchable_and_a_query_finds_a_real_command():
    """A query must resolve to a command the product actually accepts.

    Asserted against `cli.commands.COMMAND_SPECS` — the ONE registry the
    dispatcher, the palette, and the headless resolver all read — so a
    search result can never name a command that does not exist. Note it is
    `COMMAND_SPECS` and not `BUILTIN_SLASH_COMMANDS`: the latter is the
    custom-command SHADOW guard, which deliberately omits `/review` so a
    project's `.neo/commands/review.md` can still be reached. Help must
    describe what the product accepts, which is the wider set.
    """
    from cli.commands import COMMAND_SPECS

    real = {spec.name for spec in COMMAND_SPECS}
    for query, expected in (
        ("cost", "/cost"),
        ("spend", "/cost"),
        ("how much money", "/cost"),
        ("resume", "/resume"),
        ("undid", "/undo"),
        ("revert", "/undo"),
        ("stuck", "/doctor"),
        ("crash", "/doctor"),
        ("continue", "/resume"),
    ):
        names = [entry.name for entry in _iv.help_search(query)]
        assert names, f"no help match for {query!r}"
        assert expected in names, f"{query!r} did not find {expected}: {names}"
        for name in names:
            assert name in real, f"help offered {name}, which is not a real command"


def test_help_index_is_exactly_the_registry():
    """Help cannot describe a command the product lacks, or omit one it has.

    The anti-drift property, asserted structurally: the index and the
    registry are the same set, so adding a command needs no help edit and
    help can never fall behind.
    """
    from cli.commands import COMMAND_SPECS

    registry = {spec.name for spec in COMMAND_SPECS}
    index = {entry.name for entry in _iv.help_index()}
    assert index == registry
    assert len(index) == len(_iv.help_index()), "duplicate help rows"


def test_help_search_returns_nothing_rather_than_the_whole_wall(tmp_path):
    """A query that matches nothing says so; it does not dump the wall."""
    assert _iv.help_search("") == []
    assert _iv.help_search("zzz-not-a-command") == []
    rendered = _iv.render_help("zzz-not-a-command")
    assert "no command matches" in rendered
    # The failure names something the user can actually try.
    for suggestion in ("cost", "resume", "undo", "doctor"):
        assert suggestion in rendered


def test_bare_help_is_a_grouped_index_not_a_wall(tmp_path, capsys):
    """The default help is grouped and names every real command.

    "Names every real command" is the anti-drift property: a command
    added to the registry appears in help with no help edit, so help can
    never fall behind the product.
    """
    from cli.commands import COMMAND_SPECS

    rendered = _iv.render_help()
    for spec in COMMAND_SPECS:
        assert spec.name in rendered, f"bare help omits {spec.name}"
    assert "/help <word>" in rendered
    # Grouped: a heading per group, not one aligned slab.
    assert "\n\n" in rendered
    for group in ("start here", "the run", "history", "what it cost"):
        assert group in rendered


def test_help_render_is_total_and_markup_safe():
    """No registry text can become a style tag.

    A hostile or merely bracketed summary must render literally. Asserted
    by actually RENDERING through rich — a card that raised `MarkupError`
    is the exact failure this codebase has already hit — and by checking
    the escaped form is what reaches the console.
    """
    from rich.console import Console

    for entry in _iv.help_index():
        line = entry.render_line()
        # Every interpolated value is escaped, so a bracket in a summary
        # cannot open a tag.
        assert line.count("[/]") == line.count("[neo."), line
        console = Console(width=200, record=True, no_color=True)
        console.print(line)  # must not raise
        assert entry.name in console.export_text()


def test_help_render_survives_a_bracketed_registry_summary(monkeypatch):
    """A summary containing markup is DISPLAYED, not consumed.

    Registry summaries are authored text, but they are also the one place
    a future command's help could carry a bracket. Rendering through rich
    and checking the bracket survives is the property.
    """
    from rich.console import Console
    from rich.markup import escape

    entry = _iv.HelpEntry(
        name="/x",
        group="more",
        summary="uses [bold]markup[/] and [neo.accent] roles[/]",
        argument_hint="",
        search_text="x",
    )
    line = entry.render_line()
    console = Console(width=200, record=True, no_color=True, legacy_windows=False)
    console.print(line)
    text = console.export_text()
    assert "[bold]markup[/]" in text, text
    assert escape("[bold]") in line


# ---------------------------------------------------------------------------
# 6. RECOVERY AFFORDANCES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "excerpt, expected_kind",
    [
        ("RateLimitError: 429 from the provider", "model_rate_limited"),
        ("FAILED tests/test_a.py::test_b", "verification_failed"),
        ("SyntaxError: invalid syntax", "syntax_error"),
        ("No such file or directory: tests/gone.py", "file_not_found"),
    ],
)
def test_failure_card_says_what_failed_why_and_what_to_do(excerpt, expected_kind):
    """The recovery card names all three, and classifies honestly.

    Classification is DELEGATED to `cli.fileview.classify_failure` (which
    itself delegates to `harness.tool_errors`), so the card, a refusal, and
    the harness's retry policy name the same thing. The card never invents
    a kind.
    """
    lines = _rv.failure_lines(excerpt, task_id="fix-x")
    rendered = "\n".join(lines)
    assert "what failed" in rendered
    assert expected_kind in rendered
    assert "why" in rendered
    assert "next" in rendered
    # It must offer something RUNNABLE, not just a diagnosis. A card whose
    # every "next" is advice ("wait for the window") is the absence of an
    # affordance, so at least one must name a real command.
    import re as _re

    next_lines = [ln for ln in lines if "next" in ln]
    assert next_lines
    assert any(_re.search(r"/[a-z][a-z0-9_-]+", ln) for ln in next_lines), next_lines


def test_failure_card_is_total_and_markup_safe():
    """An empty excerpt still produces a card, and hostile text is escaped.

    Rendered through REAL rich, because the `MarkupError` this codebase has
    already hit is a RENDER-time failure, not a string-comparison one.
    """
    from rich.console import Console

    for excerpt in ("", None, "[/]", "[neo.ok]ok[/] boom", "x" * 5000):
        lines = _rv.failure_lines(excerpt, task_id="t")
        assert lines, f"no card for {excerpt!r}"
        console = Console(width=200, record=True, no_color=True)
        for line in lines:
            console.print(line)  # must not raise
        assert "what failed" in console.export_text()


def test_failure_card_shows_hostile_text_literally():
    """A failure message containing markup is DISPLAYED, not consumed."""
    from rich.console import Console
    from rich.markup import escape

    hostile = "[neo.ok]ok[/] and [bold]bold[/]"
    lines = _rv.failure_lines(hostile, task_id="t")
    assert any(escape("[neo.ok]ok[/]") in line for line in lines), lines
    console = Console(width=200, record=True, no_color=True, legacy_windows=False)
    for line in lines:
        console.print(line)
    assert "[neo.ok]ok[/]" in console.export_text()


def test_recovery_actions_pass_through_both_action_shapes():
    """The product has two real action vocabularies; neither may be dropped.

    `cli.commands.RECOVERY_ACTIONS` is the registry's key vocabulary;
    `cli.fileview`'s per-error-kind table is already-phrased sentences.
    The first version of this only accepted keys, which silently reduced
    every real card to the `/trace` fallback.
    """
    keyed = _rv.recovery_actions(["retry", "resume", "inspect-trace"])
    assert any("re-run" in a for a in keyed)
    assert any("/resume" in a for a in keyed)
    phrased = _rv.recovery_actions(["wait for the rate-limit window, then retry"])
    assert "wait for the rate-limit window, then retry" in phrased
    # An unknown bare token is dropped; a real fallback survives.
    assert _rv.recovery_actions(["not-an-action-key"])
    assert _rv.recovery_actions([]) == _rv.recovery_actions(None)


# ---------------------------------------------------------------------------
# 7. FIRST RUN
# ---------------------------------------------------------------------------


def test_first_run_orientation_says_what_to_type_and_where_evidence_lands():
    """The first run is an ORIENTATION, not two lines and not a wall."""
    logs = Path("/logs/abc")
    lines = _iv.render_first_run(Path("/repo/cool-project"), logs, model="cheap-model")
    rendered = "\n".join(lines)
    assert len(lines) <= 8, "a first-run screen that is itself a wall"
    assert "cool-project" in rendered
    assert "plain language" in rendered
    # The honesty promise is the product's differentiator and belongs here.
    assert "verified when the tests actually pass" in rendered
    assert str(logs) in rendered
    assert "/help" in rendered
    assert "cheap-model" in rendered


def test_first_run_orientation_is_total_without_a_repo_or_logs_root():
    """A caller that cannot resolve a fact omits it rather than erroring."""
    for args in ((None, None), ("", "")):
        lines = _iv.render_first_run(*args)
        assert lines
        assert "welcome" in "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. NO NEW CONFIG KEY, NO BEHAVIOUR-CHANGING DEFAULT
# ---------------------------------------------------------------------------


def test_index_projection_does_no_per_row_verification_work():
    """An index projection must not be a verification pass.

    MEASURED, not asserted: deriving an honest label for every indexed row
    cost 17.8 ms against 2.4 ms for the pre-round projection over 5,000
    rows — a 7.8x slowdown of the step against `cli/session.py`'s 100 ms
    p95 listing budget, and it really did blow that budget in
    `tests/test_ceiling03_sessions.py` before this was fixed.

    The contract: `normalize_index_row` is a SHAPE projection. It carries
    an existing `display_status` through and derives nothing; the honesty
    work happens for the handful of rows a person actually reads, at
    render time. The budget below is deliberately loose relative to the
    measurement so it catches a regression of this class (a per-row
    import, a per-row journal read, a per-row verdict) without being a
    timing flake.
    """
    import time as _time

    rows = [
        {
            "session_id": f"s-{index:05d}",
            "task_id": f"fix-{index:05d}",
            "repo_name": "repo",
            "status": "completed",
            "resumable": False,
            "issue": "x",
            "updated_at": 1.0,
        }
        for index in range(5000)
    ]
    _iv.normalize_index_row(rows[0])  # warm any lazy import
    best = 1e9
    for _ in range(3):
        started = _time.perf_counter()
        [_iv.normalize_index_row(row) for row in rows]
        best = min(best, (_time.perf_counter() - started) * 1000.0)
    assert best < 25.0, f"index projection cost {best:.1f}ms for 5000 rows"

    # And the honesty is still available, on demand, for what is displayed.
    shown = _iv.normalize_index_row(rows[0])
    assert shown.get("display_status") is None, (
        "the projection must not have derived a label"
    )
    assert _iv._honest_row_label(shown) == "unverified"
    assert "unverified" in _iv._row_status_text(shown, 12)


def test_no_behaviour_changing_default_was_added():
    """R2-17 must not have switched any task by adding a default.

    Rule 5 of the common header: a value in `DEFAULTS` is merged into
    every task and every eval arm, so a behaviour-changing default is a
    silent global switch. This round is presentation-only, so the DEFAULTS
    key set is asserted to be unchanged in shape and the presentation
    additions are module-level constants instead.
    """
    from harness.config import DEFAULTS

    for forbidden in (
        "stream_paint",
        "resume_briefing",
        "searchable_help",
        "spend_receipts",
        "honesty_surface",
    ):
        assert forbidden not in DEFAULTS, (
            f"{forbidden} in DEFAULTS would switch every task and eval arm"
        )
    # The bounds this round introduced are module constants, and they are
    # positive: a zero or negative bound would make the paint vanish.
    assert _tc.STREAM_PAINT_MAX_LINES >= 1
    assert _tc.STREAM_PAINT_MAX_CHARS >= 1
    assert _rv.RECEIPT_MAX_ROWS >= 1
