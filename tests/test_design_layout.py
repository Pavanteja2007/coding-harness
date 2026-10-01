"""The layout authority, the anti-clutter rule, and the responsive shell.

Every test here is named after the BEHAVIOUR it checks, and each one reads
the authority (`cli/design.py`) or the REAL mounted `VexApp` through
Textual's `Pilot` — never a hand-written expectation of what a constant
should be. The two exceptions say so in their own docstrings, because a test
that asserts a number without saying where the number came from is how a
layout constant quietly becomes a second authority.

The regression this file is really guarding is DRIFT: a second place that
decides what a viewport means. A rule that lives in six widgets is not a
rule, so `test_no_layout_constant_lives_outside_design` reads every module
under `cli/` with `ast` and fails if one of them declares a layout
constant. A comment cannot satisfy it and a reformat cannot empty it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, RichLog, Static

from cli import design
from cli import toggles as toggles_mod
from cli.tui import VexApp
from cli.tui_components import ContextPanel, EventFeed, PlanRail

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "cli"

#: The six viewports the prompt names, plus the two it implies by describing
#: a terminal rather than a size.
VIEWPORTS = [(60, 24), (80, 24), (100, 30), (120, 36), (200, 50), (50, 160)]

#: Every layout mode, at every viewport. A policy that is only right in the
#: shipped mode is a policy that has not been tested.
SIDEBAR_MODES = list(design.SIDEBAR_MODES)

#: The widget ids `compose()` mounted BEFORE this round. The constraint is
#: "do not remove any of the 11 existing widgets"; the list is written out
#: rather than derived from the current code, because a list derived from
#: the current code cannot detect a removal.
PRE_EXISTING_WIDGET_IDS = (
    "vex-header",
    "vex-body",
    "vex-side",
    "vex-context",
    "vex-runline",
    "vex-announce",
    "vex-stream",
    "vex-inputwrap",
    "vex-input",
    "vex-hints",
)

#: Names a module-level layout constant is allowed to carry. The gate also
#: runs a PATTERN scan (below) so a new constant with a different name cannot
#: slip through this list.
LAYOUT_NAME_MARKERS = (
    "BREAKPOINT",
    "MIN_COLUMNS",
    "MIN_WIDTH",
    "MIN_HEIGHT",
    "GUTTER",
    "SPACING",
    "TYPE_SCALE",
    "DENSIT",
    "CHROME",
    "BLOCK_SEPARATOR",
    "HINT_LIMIT",
    "RAIL_WIDTH",
)


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


def _app(tmp_path: Path, name: str = "repo", **kwargs: Any) -> VexApp:
    """A real VexApp against a throwaway repository and artifact root."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    return VexApp(
        repo=repo,
        log_root=tmp_path / f"{name}-logs",
        state={"repo": str(repo), "mode": "build", "file_config": {}},
        file_config={},
        version="9.9.9",
        **kwargs,
    )


def _frame(app: VexApp) -> list[str]:
    """The RENDERED screen, one string per terminal row.

    The compositor is the only place a layout claim can be checked for real:
    `widget.visual` proves what a widget CONTAINS, not whether the row was
    on screen.
    """
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def _widget_plain(app: VexApp, selector: str) -> str:
    """The text a mounted widget is currently publishing."""
    try:
        node = app.query_one(selector, Static)
    except Exception:
        return ""
    return str(node.visual)


async def _live_run(app: VexApp, task_id: str = "audit-task-1234") -> Any:
    """A run carrying enough events for every rail block to have content."""
    run = app.begin_live_run(task_id)
    for event in (
        {"kind": "task_start", "data": {"mode": "daily", "issue_text": "change the parser"}},
        {"kind": "model_request", "data": {"turn": 1, "step": "agent-1"}},
        {"kind": "tool_call", "data": {"tool": "edit", "args": {"path": "src/app.py"}, "turn": 1}},
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
            "data": {"target_passed": True, "regression_passed": True, "flaky": False},
        },
        {"kind": "model_response", "data": {"usage": {"tokens": 1200, "cost": 0.0031}}},
    ):
        run.consume(event)
    return run


async def _stop_run(app: VexApp) -> None:
    if app._run_stop is not None:
        app._run_stop.set()
    if app._tail_thread is not None:
        app._tail_thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 1. cli/design.py is the only source of a layout constant
# ---------------------------------------------------------------------------


def _module_level_assignments(path: Path) -> dict[str, int]:
    """Every module-level name a Python file ASSIGNS, with its line number.

    `ast`, not a regex: a name inside a function, a string, or a comment
    cannot be mistaken for a declaration, and a reformat cannot empty it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found: dict[str, int] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                found.setdefault(target.id, node.lineno)
    return found


def test_no_layout_constant_lives_outside_design() -> None:
    """Only `cli/design.py` may DECLARE a layout constant.

    Three scans, because one vocabulary is not enough. The declared-name scan
    catches a module that shadows a constant `design.py` owns. The pattern
    scan catches a NEW constant nobody had listed yet, which is the case a
    declared-name list cannot see. The exemption table is read FROM the
    authority, and every entry in it must carry a reason, so an exemption
    cannot be added silently.
    """
    declared_here = {
        name
        for name in _module_level_assignments(CLI_DIR / "design.py")
        if not (name.startswith("__") and name.endswith("__"))
    }
    findings: list[str] = []
    for path in sorted(CLI_DIR.glob("*.py")):
        if path.name == "design.py":
            continue
        for name, line in _module_level_assignments(path).items():
            if f"{path.name}:{name}" in design.LAYOUT_SCOPE_EXEMPT:
                continue
            if name in declared_here or any(marker in name for marker in LAYOUT_NAME_MARKERS):
                findings.append(f"{path.name}:{line} declares {name}")
    assert not findings, "a layout constant outside cli/design.py:\n" + "\n".join(findings)
    for key, reason in design.LAYOUT_SCOPE_EXEMPT.items():
        assert str(reason).strip(), f"{key} is exempt with no stated reason"
    assert design.LAYOUT_AUTHORITY_SCOPE, "an authority with no stated scope is not one"


def test_the_shell_css_uses_only_the_declared_vertical_spacing() -> None:
    """Every VERTICAL gap in the shell's CSS is 0 or one spacing unit.

    "One spacing unit" is a claim, and a claim is only worth what can fail.
    Horizontal padding inside a text region is a pre-existing decision this
    round does not own, so the gate covers the vertical axis — which is the
    axis a spacing unit governs, and the axis a `margin-bottom: 2` would
    silently double.

    Comments are stripped first, because a comment is where a design system
    NAMES its own numbers and a gate that flagged that prose would be a gate
    nobody keeps.
    """
    import re

    css = re.sub(r"/\*.*?\*/", "", VexApp.CSS, flags=re.S)
    declared = {str(design.spacing(0)), str(design.spacing(1))}
    offenders: list[str] = []
    for prop, value in re.findall(
        r"(?<![\w-])(margin-top|margin-bottom|margin|padding-top|padding-bottom|padding)\s*:\s*([^;{}]+);",
        css,
    ):
        parts = [part for part in value.split() if part]
        # Only the vertical component is this gate's business: a two-value
        # shorthand's first number is top/bottom, its second is left/right.
        vertical = [parts[0]] if parts else []
        for number in vertical:
            if number not in declared:
                offenders.append(f"{prop}: {value.strip()}")
    assert not offenders, "vertical spacing outside the declared unit:\n" + "\n".join(
        sorted(set(offenders))
    )


def test_the_anti_clutter_threshold_is_one_number() -> None:
    """The sidebar's rule and the toggles' rule are the same number.

    `cli.toggles` declares `MIN_SECTION_ENTRIES` for its own surfaces and
    `cli.design` declares the rule for the layout. Two homes for one rule is
    only safe if the two cannot drift, and the way to make that true is to
    assert it rather than to hope.
    """
    assert design.ANTI_CLUTTER_MIN_ENTRIES == toggles_mod.MIN_SECTION_ENTRIES
    assert design.ANTI_CLUTTER_MIN_ENTRIES == 3, (
        "a section with two or fewer entries is not rendered, so the "
        "threshold is three"
    )


# ---------------------------------------------------------------------------
# 2. Every region is present and within bounds at every viewport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", VIEWPORTS)
@pytest.mark.parametrize("mode", SIDEBAR_MODES)
async def test_every_declared_region_is_present_and_within_bounds(
    tmp_path: Path, clean_tui_hooks, size: tuple[int, int], mode: str
) -> None:
    """Every declared region resolves in the REAL app and sits on screen.

    The authority's own arithmetic is checked too, so a spec that claims a
    rectangle the compositor does not produce fails here rather than passing
    on a self-consistent but wrong number.
    """
    app = _app(tmp_path, f"regions-{size[0]}x{size[1]}-{mode}")
    app._sidebar_mode = design.normalize_sidebar_mode(mode)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = await _live_run(app)
        app._render_side(run)
        app._render_context(run)
        await pilot.pause()
        spec = design.resolve_layout(*size, sidebar=mode)
        assert spec.out_of_bounds() == (), "the authority placed a region off-screen"
        for name, widget_id in design.REGION_IDS.items():
            try:
                node = app.query_one(f"#{widget_id}")
            except Exception as exc:  # pragma: no cover - the failure message
                pytest.fail(f"region {name!r} ({widget_id}) is not mounted: {exc}")
            declared = spec.region(name)
            if not declared.visible:
                continue
            assert node.region.width > 0 or declared.width == 0, (
                f"{name} is declared {declared.width} wide and mounted at "
                f"{node.region.width}"
            )
            assert node.region.width <= size[0], f"{name} is wider than the terminal"
            assert node.region.height <= size[1], f"{name} is taller than the terminal"
        await _stop_run(app)


@pytest.mark.parametrize("size", VIEWPORTS)
async def test_the_pre_existing_widgets_are_all_still_mounted(
    tmp_path: Path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """None of the widgets that existed before this round was removed."""
    app = _app(tmp_path, f"widgets-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for widget_id in PRE_EXISTING_WIDGET_IDS:
            assert app.query_one(f"#{widget_id}"), widget_id
        assert app.query_one("#vex-body", EventFeed).is_attached
        assert app.query_one("#vex-side", PlanRail).is_attached
        assert app.query_one("#vex-context", ContextPanel).is_attached
        assert app.query_one("#vex-input", Input).is_attached
        assert app.query_one("#vex-body", RichLog).is_attached


# ---------------------------------------------------------------------------
# 3. The tri-state sidebar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "expected"),
    [(121, True), (120, False), (100, False), (200, True), (60, False)],
)
def test_the_sidebar_is_visible_at_121_and_hidden_at_120_under_auto(
    width: int, expected: bool
) -> None:
    """`auto` is the 120-column responsive policy, measured at the boundary."""
    spec = design.resolve_layout(width, 40, sidebar="auto")
    assert spec.sidebar_shown is expected
    assert design.sidebar_visible(width, 40, "auto") is expected


def test_the_sidebar_mode_is_tri_state_and_hide_always_hides() -> None:
    """Three declared modes; `hide` never shows the column at any size."""
    assert design.SIDEBAR_MODES == ("auto", "show", "hide")
    for width in (60, 80, 100, 120, 121, 200):
        assert design.sidebar_visible(width, 40, "hide") is False, width


@pytest.mark.parametrize("size", [(100, 30), (120, 36), (200, 50)])
async def test_the_sidebar_keybind_cycles_the_mode_and_persists_it(
    tmp_path: Path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """One keypress advances auto -> show -> hide, and the choice survives.

    The persisted store is the toggle registry's (Prompt 04's); this asserts
    the MOUNT reads it back, because a toggle that the shell does not read
    is a toggle the user cannot see work.
    """
    app = _app(tmp_path, f"toggle-{size[0]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        start = app._sidebar_mode
        seen = [start]
        for _ in range(len(design.SIDEBAR_MODES)):
            app.action_toggle_sidebar()
            await pilot.pause()
            seen.append(app._sidebar_mode)
        assert seen[0] == seen[-1], "the cycle does not come back round: %r" % (seen,)
        assert set(seen) == set(design.SIDEBAR_MODES), seen
        # The mode the shell is in is the mode the resolved layout uses.
        assert app._layout.sidebar_mode == app._sidebar_mode
        await _stop_run(app)

    again = _app(tmp_path, f"toggle-{size[0]}")
    async with again.run_test(size=size) as pilot:
        await pilot.pause()
        assert again._sidebar_mode == app._sidebar_mode, (
            "the chosen mode did not survive a restart"
        )
        await _stop_run(again)


def test_the_sidebar_mode_vocabulary_is_the_toggle_registrys() -> None:
    """The toggle's user words and the layout's modes are ONE vocabulary.

    This used to pin two vocabularies - the toggle module said
    ``auto``/``shown``/``hidden`` and the layout said ``auto``/``show``/
    ``hide`` - and assert a translation in both directions. The two
    vocabularies are the structural half of the tri-state toggle stall
    (VEX-PF-01 §2): a value written on one side was not in the list the
    other side indexed, so the advance stalled after one press. They are now
    the same three words, and the pin asserts that rather than describing a
    translation that no longer has anything to translate.

    ``shown``/``hidden`` still load, because a hand-edited store may
    contain them, but they are ALIASES now: accepted on the way in,
    canonicalised to ``show``/``hide``, and never written.
    """
    assert toggles_mod.TRISTATE_VALUES == ("auto", "show", "hide")
    assert tuple(toggles_mod.TOGGLE_VALUES["sidebar"]) == tuple(
        design.SIDEBAR_MODES
    ), "the toggle registry and the layout authority must speak ONE vocabulary"
    for word in toggles_mod.TRISTATE_VALUES:
        assert word in design.SIDEBAR_MODES
        assert design.normalize_sidebar_mode(word) == word, (
            f"{word!r} round-trips through the layout normaliser unchanged"
        )
    assert design.normalize_sidebar_mode("shown") == "show"
    assert design.normalize_sidebar_mode("hidden") == "hide"
    # The old spellings are ALIASES, and nothing writes them.
    assert "shown" not in toggles_mod.TRISTATE_VALUES
    assert "hidden" not in toggles_mod.TRISTATE_VALUES
    assert toggles_mod.TOGGLE_VALUE_ALIASES.get("shown") == "show"
    assert toggles_mod.TOGGLE_VALUE_ALIASES.get("hidden") == "hide"
    # An unusable value must never hide a region.
    assert design.normalize_sidebar_mode("nonsense") == design.DEFAULT_SIDEBAR_MODE
    assert design.normalize_sidebar_mode(None) == design.DEFAULT_SIDEBAR_MODE


@pytest.mark.parametrize("size", [(100, 30), (120, 36), (200, 50)])
def test_the_sidebar_width_is_42_and_the_content_width_is_the_declared_formula(
    size: tuple[int, int]
) -> None:
    """Width 42, and the transcript gets exactly the declared remainder.

    The prompt's formula is `width - (sidebar ? 42 : 0) - 4`. The context
    rail is subtracted too, because the transcript does not overlap it and a
    content width that ignored it would promise columns that are not on
    screen. Both forms are asserted, so the difference is a stated decision
    rather than an accident of the arithmetic.
    """
    spec = design.resolve_layout(*size, sidebar="show")
    assert spec.sidebar_cols == 42
    assert spec.content_cols == (
        size[0] - (42 if spec.sidebar_shown else 0) - spec.context_cols - 4
    )
    hidden = design.resolve_layout(*size, sidebar="hide")
    assert hidden.sidebar_cols == 0
    assert hidden.content_cols == size[0] - 0 - hidden.context_cols - 4
    # The prompt's literal formula, on the pure function.
    assert design.content_width(size[0], True) == size[0] - 42 - 4
    assert design.content_width(size[0], False) == size[0] - 4


# ---------------------------------------------------------------------------
# 4. The anti-clutter rule
# ---------------------------------------------------------------------------


def test_a_section_with_two_entries_is_not_rendered_and_three_is() -> None:
    """The rule itself, as a pure predicate, in both directions."""
    assert design.section_is_rendered(0) is False
    assert design.section_is_rendered(1) is False
    assert design.section_is_rendered(2) is False
    assert design.section_is_rendered(3) is True
    assert design.section_is_rendered(["a", "b", "c"]) is True
    # A section admitted with a `+N more` marker is still a rendered
    # section: the rule counts entries, not surviving rows.
    assert design.section_is_rendered(3) is True
    assert design.section_is_collapsible(2) is False
    assert design.section_is_collapsible(3) is True


@pytest.mark.parametrize("size", [(120, 36), (200, 50)])
async def test_the_sidebar_renders_a_two_entry_section_as_nothing_and_a_three_entry_one(
    tmp_path: Path, clean_tui_hooks, size: tuple[int, int], monkeypatch
) -> None:
    """In the REAL app: two entries publish no rows, three publish rows.

    Driven through the sidebar's own fact table, so the test exercises the
    real path rather than a widget poked by hand.
    """
    app = _app(tmp_path, f"clutter-{size[0]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = await _live_run(app)
        app._render_side(run)
        await pilot.pause()

        for entries, expected_rows in ((2, 0), (3, 3)):
            monkeypatch.setattr(
                app, "_sidebar_facts", lambda run, n=entries: {"mcp": ["s"] * n}
            )
            app._render_sidebar_sections(run)
            await pilot.pause()
            published = app._sidebar_allocation.get("mcp", 0)
            assert (published > 0) is (expected_rows > 0), (
                f"{entries} entries published {published} rows"
            )
            node = app.query_one("#vex-sidebar-mcp", Static)
            if expected_rows == 0:
                assert node.styles.display == "none"
                assert not str(node.visual).strip()
        await _stop_run(app)


def test_the_rail_drops_a_block_whose_section_has_two_entries() -> None:
    """The rule reaches the rails, and it reaches the ENTRY count.

    A block that is bounded with `+N more` is still rendered, so a rail
    cannot be emptied by the rule just for being long.
    """
    from cli.tui_components import fit_region_blocks

    blocks = {"status": ["mode", "state", "cost"], "checkpoints": ["checkpoints 2", "a", "b"]}
    thin = fit_region_blocks(
        8,
        blocks,
        ("status", "checkpoints"),
        entry_counts={"status": 3, "checkpoints": 1},
    )
    assert thin["checkpoints"] == "", "a one-entry section must publish nothing"
    assert thin["status"] != ""
    bounded = fit_region_blocks(
        2,
        {"status": ["mode", "state", "cost"]},
        ("status",),
        entry_counts={"status": 3},
    )
    assert "+" in bounded["status"], "a bounded three-entry section is still rendered"
    # The blocks the design declares exempt are exempt for a STATED reason.
    exempt = fit_region_blocks(
        4,
        {"usage": ["verify verified", "cost $0.00"], "files": ["src/app.py"]},
        ("usage", "files"),
        entry_counts={"usage": 2, "files": 1},
    )
    assert exempt["usage"] != "", "the evidence block is declared exempt"
    assert exempt["files"] != "", "the changed-file block is declared exempt"
    for block, reason in design.ANTI_CLUTTER_EXEMPT.items():
        assert str(reason).strip(), f"{block} is exempt with no stated reason"


# ---------------------------------------------------------------------------
# 5. Collapsible sections, persisted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [(120, 36), (200, 50)])
async def test_collapsing_a_section_persists_and_shows_a_triangle(
    tmp_path: Path, clean_tui_hooks, size: tuple[int, int], monkeypatch
) -> None:
    """A collapsed section keeps its heading and loses its entries, and
    the state survives a restart."""
    app = _app(tmp_path, f"collapse-{size[0]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = await _live_run(app)
        monkeypatch.setattr(
            app, "_sidebar_facts", lambda run: {"mcp": ["alpha", "beta", "gamma"]}
        )
        app._render_sidebar_sections(run)
        await pilot.pause()
        node = app.query_one("#vex-sidebar-mcp", Static)
        assert node.styles.display == "block"
        assert design.TRIANGLE_EXPANDED in str(node.visual)
        assert "alpha" in str(node.visual)

        assert app.toggle_section("mcp") is True
        await pilot.pause()
        assert design.TRIANGLE_COLLAPSED in str(node.visual)
        assert "alpha" not in str(node.visual), "a collapsed section still shows its entries"
        assert node.region.height == 1, "a collapsed section must cost exactly its heading"
        assert app.toggle_section("mcp") is False
        await _stop_run(app)

    again = _app(tmp_path, f"collapse-{size[0]}")
    async with again.run_test(size=size) as pilot:
        await pilot.pause()
        assert again._prefs.is_collapsed("mcp") is False, "the un-collapse did not persist"
        assert again.toggle_section("mcp") is True
        await pilot.pause()

    third = _app(tmp_path, f"collapse-{size[0]}")
    async with third.run_test(size=size) as pilot:
        await pilot.pause()
        assert third._prefs.is_collapsed("mcp") is True, (
            "the collapse did not survive a restart"
        )
        await _stop_run(third)


def test_a_section_that_is_not_rendered_cannot_be_collapsed(tmp_path: Path) -> None:
    """A triangle on a section that is not there is a control for nothing."""
    app = _app(tmp_path, "no-collapse")
    assert app.toggle_section("not-a-section") is False
    assert app.toggle_section("") is False


def test_an_unreadable_preference_file_falls_back_and_says_so(tmp_path: Path) -> None:
    """A corrupt preference file is a layout preference, not a crash."""
    path = design.preferences_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    prefs = design.load_preferences(tmp_path)
    assert prefs.density == design.DEFAULT_DENSITY
    assert prefs.sidebar_mode == design.DEFAULT_SIDEBAR_MODE
    assert prefs.source == "default"
    # Every stored value is validated, so a hand-edited file cannot inject
    # a mode, a density, or a section the product does not have.
    path.write_text(
        json.dumps(
            {
                "sidebar_mode": "nonsense",
                "density": "enormous",
                "collapsed": ["mcp", "not-a-section"],
            }
        ),
        encoding="utf-8",
    )
    prefs = design.load_preferences(tmp_path)
    assert prefs.sidebar_mode == design.DEFAULT_SIDEBAR_MODE
    assert prefs.density == design.DEFAULT_DENSITY
    assert prefs.collapsed == frozenset({"mcp"})


# ---------------------------------------------------------------------------
# 6. The statusline
# ---------------------------------------------------------------------------


def _true_sections() -> dict[str, str]:
    """Every statusline section, with a value that is TRUE."""
    return {
        "queue": "3 queued (ctrl+g)",
        "subagents": "2 subagents (ctrl+b)",
        "background": "1 background (ctrl+o)",
        "density": "density compact (ctrl+o)",
        "sidebar": "sidebar auto (ctrl+5)",
    }


@pytest.mark.parametrize("width", [40, 60, 80, 100, 120, 200])
def test_the_statusline_drops_sections_by_priority_as_the_terminal_narrows(
    width: int,
) -> None:
    """Narrowing drops from the TAIL, and never a kept section's own text.

    The invariant is two-sided on purpose: a statusline that dropped the
    WRONG end would still satisfy a one-sided test, and dropping the queue
    count is exactly the kind of quiet failure this rule exists to catch.
    """
    fit = design.fit_statusline(_true_sections(), width)
    order = [item.key for item in design.STATUSLINE_SECTIONS]
    positions = [order.index(key) for key in fit.sections]
    assert positions == sorted(positions), f"{fit.sections} is out of priority order"
    assert all(
        key not in fit.text for key in fit.dropped
    ), "a dropped section is still in the rendered row"
    for key in fit.sections:
        assert _true_sections()[key] in fit.text
    if fit.dropped:
        assert len(fit.text) <= width
    # Wider is never less informative than narrower, once normalised.
    narrower = design.fit_statusline(_true_sections(), max(1, width - 40))
    assert len(narrower.sections) <= len(fit.sections)


def test_the_statusline_hint_limit_is_explicit_and_bounded() -> None:
    """The limit is a declared number, and it is a real ceiling."""
    assert design.STATUSLINE_HINT_LIMIT >= 1
    fit = design.fit_statusline(_true_sections(), 200, hint_limit=2)
    assert len(fit.sections) == 2
    assert fit.limited is True
    assert fit.sections == ("queue", "subagents"), "the limit drops from the tail"


def test_an_empty_hint_renders_nothing_at_all() -> None:
    """A hint must be true: nothing queued means nothing is shown.

    `StatusSection.render` is the decision, and it is the caller's count
    that decides — the fitter never invents a number.
    """
    by_key = {item.key: item for item in design.STATUSLINE_SECTIONS}
    assert by_key["queue"].render(0) == ""
    assert by_key["queue"].render(None) == ""
    assert by_key["queue"].render(3) == "3 queued (ctrl+g)"
    fit = design.fit_statusline({"queue": "", "subagents": ""}, 200)
    assert fit.text == ""
    assert fit.sections == ()


@pytest.mark.parametrize("size", [(60, 24), (120, 36), (200, 50)])
async def test_the_statusline_is_absent_until_it_has_something_true(
    tmp_path: Path, clean_tui_hooks, size: tuple[int, int]
) -> None:
    """An idle shell spends NO row on the statusline."""
    app = _app(tmp_path, f"statusline-{size[0]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        node = app.query_one("#vex-statusline", Static)
        assert node.styles.display == "none"
        assert node.region.height == 0
        # The composer still starts where the layout said it would: a
        # statusline that costs nothing must not move anything else.
        assert app.query_one("#vex-inputwrap").region.y + app.query_one(
            "#vex-inputwrap"
        ).region.height <= size[1]

        app._queue.extend(["one", "two", "three"])
        app._render_statusline()
        await pilot.pause()
        assert app._statusline_sections, "three queued items are a fact"
        published = _widget_plain(app, "#vex-statusline")
        assert "3 queued" in published, published
        assert len(published) <= size[0], "the statusline overflowed the terminal"
        await _stop_run(app)


# ---------------------------------------------------------------------------
# 7. The type scale, the spacing unit, and density
# ---------------------------------------------------------------------------


def test_the_type_scale_is_four_roles_realised_as_weight_and_budget() -> None:
    """A terminal has no font size, so the scale is weight + column budget.

    The test says so because a `font-size` nobody can render is a number
    that looks like a design decision and is not one.
    """
    assert design.TYPE_SCALE_ORDER == ("display", "title", "body", "micro")
    assert set(design.TYPE_SCALE) == set(design.TYPE_SCALE_ORDER)
    assert [design.TYPE_SCALE[name].pts for name in design.TYPE_SCALE_ORDER] == sorted(
        [design.TYPE_SCALE[name].pts for name in design.TYPE_SCALE_ORDER], reverse=True
    )
    for role in design.TYPE_SCALE.values():
        assert role.weight in ("bold", "normal")
        assert role.hue in ("accent", "primary", "secondary")
        assert role.max_columns >= 1
    # An unknown role degrades to body rather than raising or inventing one.
    assert design.type_role("nonsense") is design.TYPE_SCALE["body"]


def test_there_is_exactly_one_spacing_unit_and_every_gap_uses_it() -> None:
    """`spacing()` is the only way a number becomes rows or columns."""
    assert design.SPACING_UNIT == 1
    assert design.spacing(0) == 0
    assert design.spacing(1) == 1
    assert design.rail_block_gap("comfortable") == 1
    assert design.rail_block_gap("compact") == 0


@pytest.mark.parametrize(
    ("size", "gains_rows"),
    [((120, 26), True), ((120, 36), False)],
    ids=["tight-rail", "roomy-rail"],
)
async def test_density_measurably_changes_the_rows_on_screen(
    tmp_path: Path, clean_tui_hooks, size: tuple[int, int], gains_rows: bool
) -> None:
    """Compact really does put more rows on the screen, and it is a number.

    Measured on the mounted app rather than the arithmetic, and measured at
    BOTH a tight and a roomy rail, because the two halves of that are the
    claim: at 120x26 the lowest-priority context blocks gain rows, and at
    120x36 they do not — there is room for both densities to publish the
    same blocks, and a density that claimed a difference there would be
    claiming a difference it did not make.

    Two effects, both counted from the widgets: the composer loses a row at
    every viewport, and the rail's block gaps close, which is where the
    extra rows actually land. The lowest-priority context blocks are the ones
    that gain them, because they are the blocks that were being squeezed.
    """
    app = _app(tmp_path, f"density-{size[0]}x{size[1]}-{gains_rows}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        run = await _live_run(app)
        app._render_side(run)
        app._render_context(run)
        await pilot.pause()
        base = app.screen_stack[0]
        low_priority = ("relevant", "sources", "legend")

        def reading() -> dict[str, Any]:
            return {
                "composer": base.query_one("#vex-inputwrap").region.height,
                "low_priority_rows": sum(
                    int(app._context_allocation.get(key, 0)) for key in low_priority
                ),
            }

        comfortable = reading()
        assert app.set_density("compact") == "compact"
        app._render_side(run)
        app._render_context(run)
        await pilot.pause()
        compact = reading()

        assert compact["composer"] < comfortable["composer"], (
            f"compact did not shorten the composer: {comfortable} -> {compact}"
        )
        if gains_rows:
            assert compact["low_priority_rows"] > comfortable["low_priority_rows"], (
                f"compact did not free a row for the squeezed blocks: "
                f"{comfortable} -> {compact}"
            )
        else:
            assert compact["low_priority_rows"] == comfortable["low_priority_rows"], (
                "a roomy rail publishes the same blocks at both densities, and "
                "claiming otherwise would be claiming a difference that was "
                "not made"
            )
        assert app._layout.density == "compact"
        # The receipt must still match what is on screen, in the compact arm
        # too: a density that changed the arithmetic without changing the
        # widgets would be a receipt nobody can trust.
        for key, selector in (
            ("usage", "#vex-context-usage"),
            ("files", "#vex-context-files"),
        ):
            assert app._context_allocation.get(key) == base.query_one(selector).region.height
        assert design.density_profile("nonsense").name == design.DEFAULT_DENSITY
        await _stop_run(app)


# ---------------------------------------------------------------------------
# 8. The sidebar's facts, and the getting-started card
# ---------------------------------------------------------------------------


def test_the_sidebar_footer_splits_the_directory_and_carries_the_version() -> None:
    """Parent and leaf, leaf FIRST, plus the version.

    A 42-column rail cannot carry a full absolute path, and the one fact a
    reader needs is which repository they are in.
    """
    footer = design.sidebar_footer("/home/dev/projects/coding-harness", "0.3.0")
    assert footer.leaf == "coding-harness"
    assert footer.parent.endswith("projects")
    lines = footer.lines
    assert lines[0] == "coding-harness"
    assert "0.3.0" in lines[-1]
    assert design.sidebar_footer("", "").rendered is False


async def test_the_getting_started_card_appears_only_without_a_provider(
    tmp_path: Path, clean_tui_hooks, monkeypatch
) -> None:
    """The card is shown when no provider is connected, and only then.

    "Certain", not "not connected": a card shown because the check could not
    run is exactly the clutter this round removes, so an unavailable check
    answers False.
    """
    app = _app(tmp_path, "startup")
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        run = await _live_run(app)
        monkeypatch.setattr(app, "_needs_provider", lambda: False)
        app._render_sidebar_sections(run)
        await pilot.pause()
        assert app._sidebar_allocation.get("startup", 0) == 0
        assert app.query_one("#vex-sidebar-startup", Static).styles.display == "none"

        monkeypatch.setattr(app, "_needs_provider", lambda: True)
        app._render_sidebar_sections(run)
        await pilot.pause()
        assert app._sidebar_allocation.get("startup", 0) >= 3
        assert "no provider" in _widget_plain(app, "#vex-sidebar-startup").lower()
        await _stop_run(app)


async def test_an_untrusted_string_cannot_escape_into_the_markup_parser(
    tmp_path: Path, clean_tui_hooks, monkeypatch
) -> None:
    """A repository name carrying `[` is DATA and must render literally.

    Model output and file contents are untrusted. A string that reaches
    Textual's markup parser carrying a `[` can delete a message, which is
    why the sidebar escapes on the way in and why the frame below is
    asserted to still contain the message.
    """
    app = _app(tmp_path, "markup")
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        run = await _live_run(app)
        hostile = "weird[name] [bold]repo[/] [/]"
        monkeypatch.setattr(
            app,
            "_sidebar_facts",
            lambda run: {"mcp": [hostile, "second", "third"]},
        )
        app._render_sidebar_sections(run)
        await pilot.pause()
        frame = "\n".join(_frame(app))
        assert "second" in frame, "a render failure deleted a message"
        assert "weird[name]" in frame, (
            "the host string was not rendered literally:\n" + frame
        )
        await _stop_run(app)


# ---------------------------------------------------------------------------
# 9. The reserved statusline row, and the chrome arithmetic
# ---------------------------------------------------------------------------


def test_a_statusline_with_no_facts_costs_no_row() -> None:
    """The statusline's row count is an INPUT to the layout, not a cost.

    The composer is bottom-ANCHORED, so showing the statusline does not move
    it: what shrinks is the transcript, which is where the row actually comes
    from. Asserting the composer moves would be asserting a layout that
    would push the composer off the bottom of a short terminal.
    """
    quiet = design.resolve_layout(120, 36, statusline_rows=0)
    loud = design.resolve_layout(120, 36, statusline_rows=1)
    assert quiet.region("statusline").height == 0
    assert loud.region("statusline").height == 1
    assert quiet.region("composer").y == loud.region("composer").y
    assert quiet.region("composer").bottom == loud.region("composer").bottom
    assert quiet.region("transcript").height > loud.region("transcript").height
    assert loud.out_of_bounds() == ()


def test_split_and_vertical_terminals_collapse_both_rails() -> None:
    """A narrow split and a vertical terminal keep the conversation."""
    for size in ((60, 24), (50, 160)):
        for mode in SIDEBAR_MODES:
            spec = design.resolve_layout(*size, sidebar=mode)
            assert spec.sidebar_shown is False, (size, mode)
            assert spec.context_shown is False, (size, mode)
            assert spec.region("transcript").width > 0


def test_the_header_budget_lives_in_the_authority() -> None:
    """The header's column budget is a layout fact, so it is declared there."""
    budget = design.HEADER_BUDGET
    assert budget.chrome_columns == design.HEADER_CHROME_COLUMNS
    assert budget.separator_columns() == len(design.HEADER_SEPARATOR)
    assert budget.separator_columns() > 1, (
        "the separator is budgeted as one column and rendered as three"
    )
    assert design.rail_block_gap(design.DEFAULT_DENSITY) == design.RAIL_BLOCK_SEPARATOR
