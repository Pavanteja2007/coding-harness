"""Responsive shell anatomy and independently testable TUI component tests."""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.text import Text
from textual.widgets import Input, RichLog, Static

from cli import ui
from cli.tui import NeoApp, _PromptScreen
from cli.tui_components import (
    CommandPaletteFrame,
    ContextPanel,
    EmptyState,
    ErrorState,
    EventFeed,
    HeaderModel,
    LoadingState,
    ModalFrame,
    PlanRail,
    ResultCard,
    ShellFooter,
    ShellHeader,
    bound_block_text,
    bounded_lines,
    fit_header,
    fit_region_blocks,
    rail_content_width,
    rail_rows,
    resolve_shell_layout,
)

pytestmark = pytest.mark.anyio

#: The viewports the prompt names, plus the two it implies by describing a
#: terminal rather than a size: a very short window, and a very narrow one.
#: Every responsive assertion is parameterised over this set, because "the
#: composer never disappears" is only a claim if it holds at the extremes.
PROMPT_VIEWPORTS = [
    (80, 24),
    (100, 30),
    (120, 36),
    (200, 50),
    (60, 24),
    (50, 160),
    (80, 12),
    (40, 12),
]

#: The shell's own status words, including the longest one.
PROMPT_STATUSES = ["idle", "running", "waiting for approval", "success", "failed"]


@pytest.fixture
def anyio_backend() -> str:
    """Use Textual's asyncio event loop for the shell tests."""
    return "asyncio"


@pytest.fixture
def clean_tui_hooks():
    """Restore process-global interactive hooks after each mounted app."""
    import cli.interactive as iv

    original = (iv._ON_TASK_START, iv._CANCEL_RUN, iv._PROMPT_BODY)
    yield
    iv._ON_TASK_START, iv._CANCEL_RUN, iv._PROMPT_BODY = original
    iv._clear_live_run()


def _console_text(*renderables: object) -> str:
    console = Console(width=110, no_color=True, force_terminal=False, record=True)
    console.print(*renderables)
    return console.export_text()


def _app(tmp_path, name: str) -> NeoApp:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    return NeoApp(
        repo=repo,
        log_root=tmp_path / f"{name}-logs",
        state={"repo": str(repo), "mode": "build", "file_config": {}},
        file_config={},
        version="9.9.9",
    )


def _frame(app: NeoApp) -> list[str]:
    """The RENDERED screen, one string per terminal row.

    The compositor is the only place a layout claim can be checked for real.
    Reading `widget.visual` proves what a widget CONTAINS; it says nothing
    about whether the row was on screen, and the round-2 audit found three
    regions whose content was complete, correct, and invisible.
    """
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def _row(app: NeoApp, selector: str) -> str:
    """The rendered text of the rows a region occupies, as one string."""
    region = app.screen_stack[0].query_one(selector).region
    if region.height <= 0:
        return ""
    rows = _frame(app)
    return "\n".join(
        rows[y][region.x : region.x + region.width]
        for y in range(region.y, min(region.y + region.height, len(rows)))
    )


def _published_lines(selector_value: str) -> int:
    """Rows a published rail block claims, blanks excluded."""
    return len([line for line in str(selector_value).splitlines() if line.strip()])


async def _stop_run(app: NeoApp) -> None:
    if app._run_stop is not None:
        app._run_stop.set()
    if app._tail_thread is not None:
        app._tail_thread.join(timeout=2)


@pytest.mark.parametrize(
    ("size", "plan_visible", "context_visible", "split", "vertical"),
    [
        ((80, 24), False, False, False, False),
        ((100, 30), True, False, False, False),
        ((120, 36), True, True, False, False),
        ((200, 50), True, True, False, False),
        ((60, 24), False, False, True, False),
        ((50, 160), False, False, True, True),
    ],
)
def test_responsive_policy_collapses_rails_deliberately(
    size: tuple[int, int],
    plan_visible: bool,
    context_visible: bool,
    split: bool,
    vertical: bool,
) -> None:
    """Viewport policy preserves central content instead of squeezing both rails."""
    layout = resolve_shell_layout(*size)
    assert layout.plan_rail_visible is plan_visible
    assert layout.context_rail_visible is context_visible
    assert layout.split_terminal is split
    assert layout.vertical_terminal is vertical
    assert layout.composer_height == 3
    assert layout.modal_width < layout.width
    assert layout.modal_max_width <= layout.width


def _header(size: tuple[int, int], status: str, **kwargs) -> HeaderModel:
    """A header model with this round's measured inputs."""
    base: dict = {
        "version": "0.2.1",
        "model": "router (adaptive)",
        "repo": "/tmp/coding-harness",
        "mode": "build",
        "task_id": "audit-task-1234",
        "layout": resolve_shell_layout(*size),
        "status": status,
    }
    base.update(kwargs)
    return HeaderModel(**base)


def test_header_keeps_task_discoverable_when_secondary_details_collapse() -> None:
    """The task survives at every width; the optional facts collapse whole.

    The previous round's version of this test asserted that the model and
    the repo were both present at 80 columns. That is not reachable with a
    23-character model id, a 26-character task id, and a status word: the
    shell was silently clipping to make it look reachable, and a clip that
    lands mid-value is what produced the `◆ neo 0.2.1 · mode` header — a
    label with no value. The contract is now the honest one: whole segments
    in the anatomy order, dropped from the tail.
    """
    standard = fit_header(_header((80, 24), "running"))
    assert "0.2.1" in standard.brand
    assert "repo coding-harness" in standard.brand
    assert standard.task.endswith("task-1234")
    assert standard.status == "running"

    vertical = fit_header(_header((50, 160), "running"))
    assert "0.2.1" in vertical.brand
    assert "model" not in vertical.brand
    assert "repo" not in vertical.brand
    assert vertical.task.endswith("task-1234")
    assert vertical.status == "running"

    wide = fit_header(_header((120, 36), "running"))
    for expected in ("repo coding-harness", "model router (adaptive)", "mode build"):
        assert expected in wide.brand


@pytest.mark.parametrize("size", PROMPT_VIEWPORTS)
@pytest.mark.parametrize("status", PROMPT_STATUSES)
def test_the_header_never_presents_a_label_without_its_value(
    size: tuple[int, int], status: str
) -> None:
    """A labelled fact is whole or absent — never a label with no value.

    This is the defect class the round-2 audit rendered: at 50 columns the
    header read `◆ neo 0.2.1 · mode` followed by the task chip, and the
    reader was left guessing what the mode was.
    """
    fit = fit_header(_header(size, status))
    for segment in fit.segments:
        assert segment.plain in fit.brand
        assert segment.value in fit.brand
    for key in fit.dropped:
        if key in {"repo", "model", "mode"}:
            assert f"{key} " not in fit.brand.replace("mode build", "")


@pytest.mark.parametrize("size", PROMPT_VIEWPORTS)
@pytest.mark.parametrize("status", PROMPT_STATUSES)
def test_the_header_reserves_both_chips_and_never_overflows(
    size: tuple[int, int], status: str
) -> None:
    """The status and the task are non-negotiable, and the row is bounded.

    Before this contract the two chips were welded together — the rendered
    row read `task audit-task-1234running` at EVERY width — so the shell's
    two most load-bearing facts were one token.

    The bound is asserted against `fit.brand` itself, which is the text the
    brand widget publishes: an earlier version of the budget joined segments
    with one space while the markup spent three per separator, and the
    difference came off the end of the last segment's value in the real
    compositor.
    """
    fit = fit_header(_header(size, status))
    assert fit.status == status or status in fit.status
    assert fit.task, "the active task must remain discoverable at every width"
    assert not fit.overflow, f"the header overflowed at {size} with {status!r}"
    # Two columns of the header's padding sit outside the chips.
    assert fit.columns + 2 <= fit.layout.width, (
        f"decided header is {fit.columns + 2} columns in a {fit.layout.width}-column "
        f"terminal: {fit.brand!r} {fit.task!r} {fit.status!r}"
    )
    for segment in fit.segments:
        assert f"{ui.DOT} {segment.plain}" in fit.brand


@pytest.mark.parametrize("size", PROMPT_VIEWPORTS)
def test_the_header_drops_segments_from_the_tail_in_the_anatomy_order(
    size: tuple[int, int],
) -> None:
    """What the header drops is a SUFFIX of the offered order, not a hole."""
    model = _header(size, "running")
    offered = [segment.key for segment in model.segments()]
    fit = fit_header(model)
    kept = [segment.key for segment in fit.segments]
    assert kept == offered[: len(kept)], (
        f"kept {kept} but the anatomy offered {offered}: {fit.brand!r}"
    )


def test_startup_state_is_full_only_on_true_first_launch() -> None:
    """Later launches use the compact actionable state without the brand hero."""
    first = EmptyState(
        first_launch=True,
        version="1.0",
        repo="/tmp/example",
        log_root="/tmp/logs",
        model="m",
        width=100,
    ).lines()
    later = EmptyState(
        first_launch=False,
        version="1.0",
        repo="/tmp/example",
        log_root="/tmp/logs",
        model="m",
        width=100,
    ).lines()
    first_text = _console_text(*first)
    later_text = _console_text(*later)
    assert (
        "repo" in first_text
        and "logs" in first_text
        and "verified, not vibed" in first_text
    )
    assert "NEO ready" in later_text
    assert "Ask, change, run, or debug this repo." in later_text
    assert "verified, not vibed" not in later_text


async def test_later_launch_suppresses_memory_and_session_receipts(
    tmp_path, clean_tui_hooks, monkeypatch
) -> None:
    """Later launches stay compact instead of replaying memory or session context."""
    import cli.interactive as iv
    import cli.session as session

    logs = tmp_path / "quiet-logs"
    iv.record_session(logs, "old-task", "old issue", str(tmp_path), "success")
    monkeypatch.setattr(
        session,
        "load_latest_session",
        lambda _root, _repo: {"summary": "PRIVATE SESSION CONTEXT"},
    )
    monkeypatch.setattr(
        session,
        "session_memory_brief",
        lambda _repo, _root: ["PRIVATE MEMORY RECEIPT"],
    )
    repo = tmp_path / "quiet-repo"
    repo.mkdir()
    app = NeoApp(
        repo=repo,
        log_root=logs,
        state={"repo": str(repo), "file_config": {}},
        file_config={},
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        feed = app.query_one("#neo-body", RichLog)
        plain = "\n".join(
            "".join(segment.text for segment in line._segments) for line in feed.lines
        )
        assert "NEO ready" in plain
        assert "PRIVATE SESSION CONTEXT" not in plain
        assert "PRIVATE MEMORY RECEIPT" not in plain


def test_result_card_preserves_verifier_gate() -> None:
    """Only clean verifier evidence can produce verified completion styling."""
    unverified = _console_text(
        ResultCard(
            task_id="task-unverified",
            mode="ask",
            facts={"status": "success"},
        ).render()
    )
    verified = _console_text(
        ResultCard(
            task_id="task-verified",
            mode="verified_fix",
            facts={
                "status": "completed_verified",
                "verification_evidence": [
                    {
                        "target_passed": True,
                        "regression_passed": True,
                        "flaky": False,
                    }
                ],
                "latest_verification": {
                    "target_passed": True,
                    "regression_passed": True,
                    "flaky": False,
                },
            },
        ).render()
    )
    assert "UNVERIFIED" in unverified
    assert "SUCCESS" not in unverified
    assert "VERIFIED" in verified
    assert "SUCCESS" in verified


def test_state_components_have_stable_public_boundaries() -> None:
    """Each requested shell concern has a small independently testable boundary."""
    assert issubclass(ShellHeader, object)
    assert issubclass(PlanRail, object)
    assert issubclass(EventFeed, object)
    assert issubclass(ResultCard, object)
    assert issubclass(ContextPanel, object)
    assert issubclass(ModalFrame, object)
    assert issubclass(CommandPaletteFrame, ModalFrame)
    assert issubclass(ShellFooter, object)
    assert issubclass(EmptyState, object)
    assert issubclass(LoadingState, object)
    assert issubclass(ErrorState, object)
    assert "retry" in ErrorState("tool failed", "retry").render()


@pytest.mark.parametrize(
    "size",
    [(80, 24), (100, 30), (120, 36), (200, 50), (60, 24), (50, 160)],
)
async def test_shell_anatomy_keeps_event_feed_and_composer_visible(
    tmp_path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """Every required viewport keeps the composer, task, and central event surface."""
    app = _app(tmp_path, f"repo-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = app.begin_live_run("layout-task")
        run.consume(
            {
                "kind": "tool_call",
                "data": {"tool": "edit", "args": {"path": "src/app.py"}},
            }
        )
        app._render_side(run)
        app._render_context(run)
        composer = app.query_one("#neo-input", Input)
        composer.value = "resize-preserve"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        layout = resolve_shell_layout(*size)
        assert app.query_one("#neo-side").styles.display == (
            "block" if layout.plan_rail_visible else "none"
        )
        assert app.query_one("#neo-context").styles.display == (
            "block" if layout.context_rail_visible else "none"
        )
        assert "layout-task" in str(app.query_one("#neo-task", Static).visual)
        assert app.query_one("#neo-body", EventFeed).is_attached
        assert composer.is_attached and composer.value == "resize-preserve"
        assert composer.region.height > 0
        wrapper = app.query_one("#neo-inputwrap")
        footer = app.query_one("#neo-hints")
        assert wrapper.region.height == layout.composer_height
        assert footer.region.y > wrapper.region.y
        assert footer.region.y + footer.region.height <= size[1]
        await _stop_run(app)


async def test_context_panel_is_projected_from_journal_events(
    tmp_path, clean_tui_hooks
) -> None:
    """Files, diagnostics, and verification in the context rail come from the journal."""
    app = _app(tmp_path, "context-repo")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        run = app.begin_live_run("context-task")
        for event in (
            {
                "kind": "tool_call",
                "data": {"tool": "edit", "args": {"path": "src/app.py"}},
            },
            {
                "kind": "diagnostics",
                "data": {
                    "items": [
                        {
                            "path": "src/app.py",
                            "severity": "error",
                            "code": "E041",
                            "message": "name is not defined",
                        }
                    ]
                },
            },
            {
                "kind": "verify",
                "data": {
                    "target_passed": True,
                    "regression_passed": True,
                    "flaky": False,
                },
            },
        ):
            run.consume(event)
        app._render_context(run)
        await pilot.pause()
        assert "src/app.py" in str(app.query_one("#neo-context-files", Static).visual)
        diagnostics = str(app.query_one("#neo-context-diagnostics", Static).visual)
        # One diagnostic is ONE row in a 27-column rail. The old renderer
        # wrapped it across three (`error · E999 ·` / `· invalid syntax`),
        # which is a fact split in half with a dangling separator, and it is
        # also why three rows of two-column wrap could push the evidence
        # block off the rail entirely. The link and the code lead so they
        # are never the part that gets cut, and the cut is MARKED.
        assert "src/app.py:1:1" in diagnostics
        assert "E041" in diagnostics
        assert "…" in diagnostics, "a bounded diagnostic must say it was bounded"
        usage = str(app.query_one("#neo-context-usage", Static).visual)
        assert "verify verified" in usage
        await _stop_run(app)


async def test_resize_preserves_run_modal_and_composer_state(
    tmp_path, clean_tui_hooks
) -> None:
    """Live resize keeps the active task, modal draft, composer text, and modal in bounds."""
    app = _app(tmp_path, "resize-repo")
    sizes = [(80, 24), (100, 30), (120, 36), (200, 50), (60, 24), (50, 160), (120, 36)]
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.begin_live_run("resize-task")
        base_screen = app.screen_stack[0]
        composer = base_screen.query_one("#neo-input", Input)
        composer.value = "composer survives every resize"
        composer.cursor_position = len(composer.value)
        app.push_screen(_PromptScreen("resize-safe modal", [Text("modal body")]))
        await pilot.pause()
        app.screen.query_one("#prompt-input", Input).value = "modal draft"
        for width, height in sizes:
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            layout = resolve_shell_layout(width, height)
            assert isinstance(app.screen, _PromptScreen)
            assert (
                str(app.screen.query_one("#prompt-title", Static).visual)
                == "resize-safe modal"
            )
            assert app.screen.query_one("#prompt-input", Input).value == "modal draft"
            assert (
                base_screen.query_one("#neo-input", Input).value
                == "composer survives every resize"
            )
            assert base_screen.query_one("#neo-input", Input).cursor_position == len(
                "composer survives every resize"
            )
            assert "resize-task" in str(
                base_screen.query_one("#neo-task", Static).visual
            )
            assert base_screen.query_one("#neo-side").styles.display == (
                "block" if layout.plan_rail_visible else "none"
            )
            assert base_screen.query_one("#neo-context").styles.display == (
                "block" if layout.context_rail_visible else "none"
            )
            box = app.screen.query_one("#prompt-box")
            assert box.region.width <= layout.modal_width
            assert box.region.x >= 0
            assert box.region.x + box.region.width <= width
        await pilot.press("escape")
        await pilot.pause()
        await _stop_run(app)


# ---------------------------------------------------------------------------
# ROUND 2 — the rail row budget.
#
# Every test above this line asserted a WIDGET: its content, its display
# flag, its value. None of them asked whether the row was on screen. The
# rendered-frame tests below are the ones that catch the defect class this
# round was opened for: content that is correct, complete, and invisible.
# ---------------------------------------------------------------------------

_LIVE_EVENTS = [
    {
        "kind": "task_start",
        "data": {"mode": "daily", "issue_text": "change the parser"},
    },
    {"kind": "model_request", "data": {"turn": 1, "step": "agent-1"}},
    {
        "kind": "tool_call",
        "data": {"tool": "edit", "args": {"path": "src/app.py"}, "turn": 1},
    },
    {
        "kind": "tool_call",
        "data": {"tool": "edit", "args": {"path": "src/parser.py"}, "turn": 1},
    },
    {
        "kind": "tool_call",
        "data": {"tool": "read", "args": {"path": "src/lexer.py"}, "turn": 1},
    },
    {
        "kind": "diagnostics",
        "data": {
            "items": [
                {
                    "path": "src/app.py",
                    "severity": "error",
                    "code": "E041",
                    "message": "name is not defined",
                },
                {
                    "path": "src/parser.py",
                    "severity": "warning",
                    "code": "W001",
                    "message": "unused import",
                },
            ]
        },
    },
    {
        "kind": "verify",
        "data": {"target_passed": True, "regression_passed": True, "flaky": False},
    },
    {"kind": "model_response", "data": {"usage": {"tokens": 1200, "cost": 0.0031}}},
]


def test_a_block_that_does_not_fit_states_the_omission() -> None:
    """A bound block says how much it left out, and a zero-row block vanishes."""
    body = [f"row {index}" for index in range(9)]
    assert bounded_lines(body, 0) == ""
    assert bounded_lines(body, 1) == "+9 more"
    assert bounded_lines(body, 3).splitlines() == ["row 0", "row 1", "+7 more"]
    assert bounded_lines(body, 9) == "\n".join(body)
    assert bounded_lines(body, 40) == "\n".join(body)
    assert bounded_lines([], 5) == ""


def test_blocks_are_admitted_in_priority_order_and_the_omission_is_stated() -> None:
    """The evidence block is admitted before the file list, and the total fits."""
    blocks = {
        "status": ["mode build", "state running", "cost $0.0031", "error boom"],
        "checkpoints": ["checkpoints 2", "· 41 tok-1", "· 88 tok-2"],
        "plan": [f"step {index} pending" for index in range(12)],
    }
    fitted = fit_region_blocks(11, blocks, PlanRail.BLOCK_PRIORITY)
    assert "cost $0.0031" in fitted["status"], "the meters must not be what gives way"
    assert "error boom" in fitted["status"]
    assert "checkpoints 2" in fitted["checkpoints"]
    assert "+" in fitted["plan"], "a plan list that did not fit must say so"
    total = sum(len(value.splitlines()) for value in fitted.values())
    assert total <= 11, f"the rail was promised 11 rows and was given {total}"


def test_a_starved_block_is_dropped_rather_than_invented_a_row() -> None:
    """The allocator never spends a row it was not given, and never reclaims.

    A rail with three rows and a four-row status block keeps two facts and
    says `+2 more`; the plan list behind it gets nothing and is dropped. The
    tempting alternative — handing the plan the row reserved for its
    separator — was measured to over-promise by exactly one row, so the
    allocator stays monotone: a dropped block is a policy decision, and the
    policy is `BLOCK_PRIORITY`.
    """
    blocks = {
        "status": ["mode build", "cost $0.0031", "error boom", "journal 1 unreadable"],
        "plan": ["step 0"],
    }
    fitted = fit_region_blocks(3, blocks, PlanRail.BLOCK_PRIORITY)
    assert fitted["status"].splitlines() == ["mode build", "+3 more"]
    assert fitted["plan"] == ""


def test_a_rail_with_no_room_at_all_drops_everything_without_inventing_rows() -> None:
    """A starved rail is empty, and it is empty for a reason."""
    blocks = {"status": ["mode build", "cost $0.0031"], "plan": ["step 0", "step 1"]}
    fitted = fit_region_blocks(0, blocks, PlanRail.BLOCK_PRIORITY)
    assert fitted == {"status": "", "plan": ""}


def test_an_unprioritised_block_is_never_published() -> None:
    """A block with no place in the priority order is an unbounded block."""
    fitted = fit_region_blocks(
        20, {"status": ["cost $0.00"], "surprise": ["should never render"]}, ("status",)
    )
    assert fitted["surprise"] == ""


def test_a_rail_is_never_promised_rows_the_composer_needs() -> None:
    """`rail_rows` is the shell's own arithmetic, and it is bounded below."""
    # header 1 + run line 1 + announcement 1 + composer 3 + footer 1
    assert rail_rows(36) == 29
    assert rail_rows(4) == 0
    assert rail_rows(0) == 0
    assert rail_content_width(30) == 27
    assert rail_content_width(1) >= 1


def test_a_bounded_block_costs_one_row_per_line() -> None:
    """The budget is a measurement, not an estimate, because nothing wraps."""
    block = Text("FILES\n[UV] src/parser.py modified +3 -1\n[UV] src/lexer.py +0 -9")
    bounded = bound_block_text(block, 12)
    assert len(bounded.plain.splitlines()) == 3
    assert all(len(line) <= 12 for line in bounded.plain.splitlines()), (
        "a bounded line must fit the width it was given"
    )
    assert bound_block_text(Text("x" * 40), 10).plain == "xxxxxxxxx…"


@pytest.mark.parametrize("size", PROMPT_VIEWPORTS)
async def test_the_rendered_header_keeps_the_task_and_status_apart(
    tmp_path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """The rendered row shows the task and the status as two separate words.

    The defect: `task audit-task-1234running`, at every width, with nothing
    marking where one fact ended. This reads the compositor, not the widget.
    """
    app = _app(tmp_path, f"header-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.begin_live_run("audit-task-1234")
        app._render_header("running")
        app._render_side(app._run)
        await pilot.pause()
        row = _frame(app)[0]
        assert "task-1234" in row, row
        assert "running" in row, row
        between = row.split("task-1234", 1)[1]
        assert between.strip(), f"the status chip is welded to the task chip: {row!r}"
        assert between.split()[0] == "running", row
        assert len(row) <= size[0]
        await _stop_run(app)


@pytest.mark.parametrize(
    "size", [(120, 36), (120, 30), (120, 26), (200, 50), (160, 44)]
)
async def test_the_rendered_context_rail_always_shows_the_evidence(
    tmp_path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """The verification state and the cost are ON SCREEN wherever the rail is.

    The defect: at 120x36 and 120x30 the whole `EVIDENCE + USAGE` block sat
    below the rail's bottom edge, so the product's central promise — the
    evidence for the result — was not rendered at all.
    """
    app = _app(tmp_path, f"evidence-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = app.begin_live_run("audit-task-1234")
        for event in _LIVE_EVENTS:
            run.consume(event)
        app._render_context(run)
        await pilot.pause()
        base = app.screen_stack[0]
        layout = resolve_shell_layout(*size)
        if not layout.context_rail_visible:
            await _stop_run(app)
            return
        panel = app.screen_stack[0].query_one("#neo-context")
        assert panel.region.height > 0
        evidence = _row(app, "#neo-context-usage")
        assert "verify verified" in evidence, (
            f"the evidence block is not on screen at {size}:\n{_frame(app)}"
        )
        assert "$0.0031" in evidence, (
            f"the cost is not on screen at {size}:\n{_frame(app)}"
        )
        assert app._context_allocation.get("usage", 0) >= 3
        # The receipt is rows, and it must agree with what is on screen.
        for key, region_rows in (
            ("usage", base.query_one("#neo-context-usage").region.height),
            ("files", base.query_one("#neo-context-files").region.height),
            ("legend", base.query_one("#neo-context-legend").region.height),
        ):
            assert app._context_allocation.get(key) == region_rows, (
                f"the {key} receipt says {app._context_allocation.get(key)} rows and "
                f"the region is {region_rows} at {size}"
            )
        await _stop_run(app)


@pytest.mark.parametrize(
    "size", [(120, 36), (120, 30), (120, 26), (200, 50), (100, 30)]
)
async def test_the_rendered_plan_rail_always_shows_its_meters(
    tmp_path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """Mode, state, and cost are ON SCREEN wherever the plan rail is.

    The defect: `cost`, `tools`, the unreadable-event warning, and `error`
    were below the fold at every height where the rail appeared, because the
    rail published a fixed 30-row column into whatever height it had.
    """
    app = _app(tmp_path, f"meters-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = app.begin_live_run("audit-task-1234")
        for event in _LIVE_EVENTS:
            run.consume(event)
        app._render_side(run)
        await pilot.pause()
        base = app.screen_stack[0]
        layout = resolve_shell_layout(*size)
        if not layout.plan_rail_visible:
            await _stop_run(app)
            return
        status = _row(app, "#neo-side-status")
        assert "mode" in status, (
            f"the rail's meters are not on screen at {size}:\n{_frame(app)}"
        )
        assert "cost" in status, f"the cost is not on screen at {size}:\n{_frame(app)}"
        assert "calls" in status
        for key, selector in (("status", "#neo-side-status"), ("plan", "#neo-todo")):
            assert (
                app._rail_allocation.get(key) == base.query_one(selector).region.height
            ), (
                f"the {key} receipt says {app._rail_allocation.get(key)} rows and the "
                f"region is {base.query_one(selector).region.height} at {size}"
            )
        await _stop_run(app)


@pytest.mark.parametrize("size", [(120, 36), (120, 30), (200, 50), (120, 26)])
async def test_no_rail_region_is_cut_without_saying_so(
    tmp_path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """A region's height is the number of rows it published — or it says why.

    This is the generalisation of the defect: a rail region whose content
    was longer than its `max-height` lost its tail with no marker, so the
    shell could not tell a complete list from a truncated one.
    """
    app = _app(tmp_path, f"cut-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = app.begin_live_run("audit-task-1234")
        for event in _LIVE_EVENTS:
            run.consume(event)
        app._render_side(run)
        app._render_context(run)
        await pilot.pause()
        base = app.screen_stack[0]
        layout = resolve_shell_layout(*size)
        pairs = [
            ("#neo-side", layout.plan_rail_visible),
            ("#neo-context", layout.context_rail_visible),
        ]
        regions = {
            "#neo-side": (
                "#neo-side-header",
                "#neo-todo",
                "#neo-plan-checkpoints",
                "#neo-side-label",
                "#neo-side-status",
            ),
            "#neo-context": (
                "#neo-context-header",
                "#neo-context-usage",
                "#neo-context-files",
                "#neo-context-diagnostics",
                "#neo-context-relevant",
                "#neo-context-sources",
                "#neo-context-legend",
            ),
        }
        for rail, visible in pairs:
            rail_region = base.query_one(rail).region
            if not visible or rail_region.height <= 0:
                continue
            bottom = rail_region.y
            for selector in regions[rail]:
                node = base.query_one(selector)
                published = _published_lines(str(node.visual))
                if published == 0:
                    # An empty block is HIDDEN, not left as an empty box: an
                    # empty `height: auto` Static still costs a row and a
                    # margin, and two of them spent three of a 19-row rail.
                    assert node.styles.display == "none", (
                        f"{selector} at {size} is empty but still rendered"
                    )
                else:
                    assert node.region.height == published, (
                        f"{selector} at {size} occupies {node.region.height} rows and "
                        f"published {published}: {node.visual!r}"
                    )
                bottom = max(bottom, node.region.y + node.region.height)
            # The real invariant is the BOTTOM EDGE, and it is margin-aware:
            # summing region heights misses the one-row gap between blocks, and
            # that gap is exactly how the state-code legend lost its last row
            # to the bottom edge at 120x36.
            assert bottom <= rail_region.y + rail_region.height, (
                f"the rail {rail} at {size} ends at {rail_region.y + rail_region.height} "
                f"and its last block ends at {bottom}: "
                f"{ {key: base.query_one(key).region.height for key in regions[rail]} }"
            )
        await _stop_run(app)


@pytest.mark.parametrize("size", [(120, 36), (200, 50), (100, 30)])
async def test_the_rail_states_each_fact_once(
    tmp_path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """No label appears in both rail blocks, in two vocabularies.

    The round-2 audit rendered `verify PASS` directly above
    `verify verified` and `elapsed —` above `time 0s` in the same column.
    Two words for one fact is how a reader ends up unsure which one the
    product means.
    """
    from cli.tui import _RAIL_ROW_LABELS_OWNED_ELSEWHERE, _row_label

    app = _app(tmp_path, f"vocab-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = app.begin_live_run("audit-task-1234")
        for event in _LIVE_EVENTS:
            run.consume(event)
        app._render_side(run)
        await pilot.pause()
        base = app.screen_stack[0]
        projection = [
            _row_label(line)
            for line in str(base.query_one("#neo-todo").visual).splitlines()
        ]
        meters = [
            _row_label(line)
            for line in str(base.query_one("#neo-side-status").visual).splitlines()
        ]
        shared = {label for label in projection if label and label in meters}
        assert not shared, (
            f"the rail states {sorted(shared)} twice: {projection} / {meters}"
        )
        for label in _RAIL_ROW_LABELS_OWNED_ELSEWHERE:
            assert label not in projection, (
                f"{label} belongs to the meters block and is still in the projection"
            )
        await _stop_run(app)


@pytest.mark.parametrize("size", [(80, 12), (60, 16), (50, 20), (40, 12), (100, 14)])
async def test_the_composer_stays_on_screen_at_a_short_viewport(
    tmp_path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """The composer never disappears, however short the terminal gets.

    The prompt makes this a hard requirement; the round-2 audit measured the
    shell at 80x12, 100x14, 50x20 and 40x12, where the header, the run line,
    the announcement region, and the stream paint all compete for the same
    handful of rows.
    """
    app = _app(tmp_path, f"short-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = app.begin_live_run("audit-task-1234")
        app._render_run(run)
        app._render_side(run)
        composer = app.query_one("#neo-input", Input)
        composer.value = "still here"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        base = app.screen_stack[0]
        region = base.query_one("#neo-inputwrap").region
        assert region.height >= 1, f"the composer wrapper vanished at {size}"
        assert region.y + region.height <= size[1], (
            f"the composer is off the bottom of the screen at {size}"
        )
        footer = base.query_one("#neo-hints").region
        assert footer.y + footer.height <= size[1]
        assert base.query_one("#neo-input", Input).value == "still here"
        await _stop_run(app)
