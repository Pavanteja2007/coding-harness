"""Token, theme, fallback, and terminal-safety regression tests."""

from __future__ import annotations

import io
import pathlib
import re
import subprocess
import sys
import tokenize

import pytest

from cli import theme, ui


@pytest.fixture(autouse=True)
def restore_active_theme():
    """Restore process-global presentation state after each test."""
    original = ui.active_tokens()
    yield
    ui.set_active_tokens(original)


def test_token_catalog_covers_surface_and_state_roles():
    tokens = theme.resolve_theme(depth=theme.ColorDepth.TRUECOLOR)
    required = {
        "bg_base",
        "bg_panel",
        "bg_panel_hover",
        "border_subtle",
        "accent_text",
        "accent_glow",
        "text_primary",
        "text_secondary",
        "success",
        "warning",
        "error",
        "info",
        "pending",
        "text_disabled",
        "selection_bg",
        "focus",
        "hover",
        "pressed",
        "streaming",
        "approval",
    }
    assert required <= set(tokens)
    assert tokens["bg_base"] == "#000000"
    assert tokens["bg_panel"] == "#0A0A0A"
    assert tokens["accent_text"] == "#E8114A"
    assert tokens["accent_glow"] == "#FF7A93"
    assert tokens["text_secondary"] == "#8A8A8A"
    assert all(value.startswith("#") for value in tokens.values())


def test_token_aliases_keep_semantic_names_stable():
    tokens = theme.resolve_theme(depth=theme.ColorDepth.TRUECOLOR)
    assert tokens["background"] == tokens["bg_base"]
    assert tokens["panel"] == tokens["bg_panel"]
    assert tokens["accent"] == tokens["accent_text"]
    assert tokens["muted"] == tokens["text_secondary"]
    assert tokens["selected"] == tokens["selection_bg"]
    assert tokens["diff.add"] == tokens["diff_add"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {"COLORTERM": "truecolor", "TERM": "xterm-256color"},
            theme.ColorDepth.TRUECOLOR,
        ),
        ({"TERM": "xterm-256color"}, theme.ColorDepth.ANSI256),
        ({"TERM": "xterm"}, theme.ColorDepth.ANSI16),
        ({"TERM": "dumb"}, theme.ColorDepth.NONE),
        ({"NO_COLOR": "1", "TERM": "xterm-256color"}, theme.ColorDepth.NONE),
        ({"NEO_COLOR_DEPTH": "16", "TERM": "xterm-256color"}, theme.ColorDepth.ANSI16),
        ({"FORCE_COLOR": "2", "TERM": "xterm"}, theme.ColorDepth.ANSI256),
    ],
)
def test_color_depth_detection(env, expected):
    assert theme.resolve_color_depth(env, is_tty=True) is expected


def test_forced_and_non_tty_depths_are_explicit():
    assert (
        theme.resolve_color_depth({"TERM": "xterm"}, is_tty=False)
        is theme.ColorDepth.NONE
    )
    assert (
        theme.resolve_color_depth(
            {"TERM": "xterm-256color", "COLORTERM": "truecolor"},
            is_tty=False,
        )
        is theme.ColorDepth.NONE
    )
    assert (
        theme.resolve_color_depth({"TERM": "xterm"}, is_tty=True)
        is theme.ColorDepth.ANSI16
    )


def test_truecolor_256_and_16_profiles_render_without_literal_leakage(monkeypatch):
    """Each colour depth renders its own escape family, with no literal leak.

    The RENDER half runs in a fresh interpreter. That is not ceremony: it is
    the fix for a measured order-dependence.

    rich.style.Style.render is:
        attrs = self._ansi or self._make_ansi_codes(color_system)
    `_ansi` is memoized ON THE STYLE INSTANCE, and `Style.parse` is
    lru_cache'd, so one Style object is shared process-wide and whichever
    colour system renders it FIRST wins for the rest of the process.
    `theme.rich_theme()` builds its Theme from `Style.parse(...)`, i.e. from
    those shared instances.

    Measured, with everything else held equal: after tests/test_cli_tui.py
    drove a Textual app (16-colour / windows consoles), a Console built with
    color_system="truecolor" still emitted "1;31" instead of "1;38;2;...".
    The inputs were provably fine -- identical get_ansi_codes
    ('38','2','232','17','74'), identical downgrade(TRUECOLOR), an
    un-poisoned Color, and `Style.render` entered with
    color_system=ColorSystem.TRUECOLOR -- and clearing the Color.* and
    Style.parse lru_caches did NOT fix it, which is what localised the fault
    to Rich's per-Style `_ansi` memo rather than to our theme.

    A subprocess cannot inherit any of that, so the render assertion is
    order-independent by construction. The token assertions below stay
    in-process: they are our actual product contract and must not need a
    subprocess to be trustworthy.
    """
    for leaked in ("NO_COLOR", "NEO_NO_COLOR"):
        monkeypatch.delenv(leaked, raising=False)

    cases = (
        (theme.ColorDepth.TRUECOLOR, "38;2;"),
        (theme.ColorDepth.ANSI256, "38;5;"),
        (theme.ColorDepth.ANSI16, "31m"),
    )
    for depth, _marker in cases:
        tokens = theme.resolve_theme(env={}, depth=depth)
        assert tokens.color_enabled, f"{depth.name} must keep colour enabled"
        accent = tokens.style("accent")
        if depth is theme.ColorDepth.TRUECOLOR:
            # Only truecolor can carry the exact brand crimson; the reduced
            # depths deliberately downshift the palette (ANSI256 accent is
            # #FF005F), so pinning the exact hex there would be asserting the
            # downshift is a bug.
            assert accent.lower() == "#e8114a", (
                f"truecolor accent degraded to {accent!r}"
            )
        else:
            assert accent.startswith("#") and len(accent) in (4, 7), (
                f"{depth.name} accent must stay a real colour, got {accent!r}"
            )

    program = (
        "import io, sys\n"
        "from rich.console import Console\n"
        "from cli import theme\n"
        "depth = theme.ColorDepth[sys.argv[1]]\n"
        "tokens = theme.resolve_theme(env={}, depth=depth)\n"
        "out = io.StringIO()\n"
        "console = Console(file=out, force_terminal=True,\n"
        "                  color_system={'TRUECOLOR': 'truecolor', 'ANSI256': '256',"
        " 'ANSI16': 'windows'}[depth.name],\n"
        "                  theme=theme.rich_theme(tokens))\n"
        "console.print('[neo.accent]active[/]')\n"
        "sys.stdout.write(out.getvalue())\n"
    )
    for depth, marker in cases:
        completed = subprocess.run(
            [sys.executable, "-c", program, depth.name],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(pathlib.Path(__file__).resolve().parents[1]),
        )
        assert completed.returncode == 0, (
            f"{depth.name} render subprocess failed: {completed.stderr.strip()}"
        )
        rendered = completed.stdout
        assert "active" in rendered, rendered
        assert marker in rendered, f"{depth.name} render {rendered!r} lacks {marker!r}"
        assert "#e8114a" not in rendered.lower(), rendered


def test_no_color_and_dumb_preview_are_plain_and_lossless():
    tokens = theme.resolve_theme(depth=theme.ColorDepth.NONE)
    preview = theme.theme_preview(tokens)
    assert "Neo terminal theme: default" in preview
    assert "bg_base" in preview
    assert "[#000000]" not in preview
    assert "[ ]" in preview
    assert tokens.color_enabled is False
    assert tokens.style("accent_text", bold=True) == "bold"


def test_builtin_profiles_and_user_overrides():
    assert theme.theme_names() == ("default", "high-contrast", "reduced-motion")
    contrast = theme.resolve_theme("high-contrast", depth=theme.ColorDepth.TRUECOLOR)
    assert contrast["text_primary"] == "#FFFFFF"
    assert contrast["border_subtle"] == "#FFFFFF"
    reduced = theme.resolve_theme("reduced-motion", depth=theme.ColorDepth.TRUECOLOR)
    assert reduced.motion is False
    custom = theme.resolve_theme(
        "default",
        {"accent_text": "#123456", "not_a_token": "#FFFFFF", "motion": False},
        depth=theme.ColorDepth.TRUECOLOR,
    )
    assert custom["accent_text"] == "#123456"
    assert custom.motion is False
    assert custom.source == "override"
    with pytest.raises(ValueError):
        theme.resolve_theme("default", {"accent_text": "red"}, strict=True)


def test_theme_preview_and_report_are_serializable():
    report = theme.theme_summary(theme.resolve_theme("high-contrast", depth="256"))
    assert report["theme"] == "high-contrast"
    assert report["depth"] == "256"
    assert report["tokens"]["accent_text"].startswith("#")
    preview = ui.preview_theme("reduced-motion", depth="16")
    assert "motion: off" in preview
    assert "Neo terminal theme" in preview


def test_textual_variables_are_semantic_and_complete():
    variables = ui.textual_theme_variables(
        theme.resolve_theme("default", depth=theme.ColorDepth.TRUECOLOR)
    )
    for name in (
        "neo-background",
        "neo-panel",
        "neo-border",
        "neo-accent",
        "neo-highlight",
        "neo-success",
        "neo-warning",
        "neo-error",
        "neo-info",
        "neo-pending",
        "neo-focus",
        "neo-hover",
        "neo-pressed",
        "neo-streaming",
        "neo-approval",
    ):
        assert name in variables


def test_legacy_encoding_and_dumb_glyph_selection(monkeypatch):
    class LegacyStream:
        encoding = "cp1252"

    monkeypatch.setattr(ui.sys, "stdout", LegacyStream())
    assert ui.is_legacy_encoding() is True
    assert ui._enc_ok("→") is False
    assert ui._glyph("→", "->") == "->"
    monkeypatch.setenv("TERM", "dumb")
    assert ui.is_dumb_terminal() is True
    assert ui._glyph("→", "->") == "->"


def test_unverified_completion_never_uses_success_semantics():
    from cli import runview

    rows = runview.card_lines(
        {"task_id": "u1", "status": "completed_unverified", "mode": "ask"},
        mode="ask",
    )
    assert "COMPLETED · UNVERIFIED" in rows[0]
    assert "[neo.ok]" not in rows[0]
    assert "[neo.warn]" in rows[0]
    assert runview.status_is_verified("completed_unverified") is False
    assert runview.status_is_completed("completed_unverified") is True


def test_dumb_terminal_disables_tui_entry(monkeypatch):
    import cli.tui as tui

    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("NEO_TUI", raising=False)
    assert tui.can_run_tui() is False


def test_custom_theme_can_be_selected_without_literal_ui_state():
    original = ui.active_tokens()
    selected = ui.set_theme("high-contrast", depth=theme.ColorDepth.TRUECOLOR)
    assert selected.theme == "high-contrast"
    assert ui.ACCENT_TEXT == "#FF315C"
    assert (
        ui.current_rich_theme().styles["neo.accent"].color.get_truecolor().hex.lower()
        == "#ff315c"
    )
    ui.set_active_tokens(original)


def test_unmapped_role_never_reaches_textual_as_raw_markup(monkeypatch):
    """A no-color theme can only express SOME roles; the rest must become
    Textual's `none` style. A raw `[neo.x]` tag is not a Textual style, so
    it orphans the next `[/]` and kills the app with a MarkupError."""
    from textual.content import Content

    import cli.tui as tui

    monkeypatch.setattr(tui, "_ROLE_MAP", {"neo.accent": "bold"})
    mapped = tui._m("[neo.accent]ok[/] [neo.muted]idle[/]")
    assert "neo." not in mapped
    assert "[none]idle[/]" in mapped
    Content.from_markup(mapped)


def test_real_header_and_status_markup_parse_under_a_partial_role_map(monkeypatch):
    """The exact strings the shell header and status widget push into Textual
    must parse even when the theme can express only one role."""
    from textual.content import Content

    import cli.tui as tui
    from cli.tui_components import HeaderModel, resolve_shell_layout

    monkeypatch.setattr(tui, "_ROLE_MAP", {"neo.accent": "bold"})
    header = HeaderModel(
        version="0.2.0",
        model="router (adaptive)",
        repo="coding-harness",
        mode="auto",
        task_id="agent-09",
        layout=resolve_shell_layout(120, 36),
    )
    Content.from_markup(tui._m(header.brand_markup()))
    Content.from_markup(tui._m(header.task_markup()))
    Content.from_markup(tui._m("[neo.muted]idle[/]"))


# ---------------------------------------------------------------------------
# The token system must be SINGLE-SOURCE and the non-color channel must be
# real.  The three gates below are what stop this from decaying back into
# scattered literal colors and color-only state.
# ---------------------------------------------------------------------------


def test_every_token_has_a_renderer_role_or_a_declared_reason():
    """A token no renderer can draw is a gap, not a preference.

    `TOKEN_NAMES` is the catalog; `RICH_ROLE_TOKENS` and
    `TEXTUAL_VARIABLE_TOKENS` are the two authoritative renderer tables.
    Adding a token without a role and without an entry in `UNMAPPED_TOKENS`
    is how a semantic token silently becomes unreachable, so this fails.
    """
    assert theme.unmapped_tokens() == {}
    coverage = theme.token_coverage()
    assert set(coverage) == set(theme.TOKEN_NAMES)
    for name, entry in coverage.items():
        assert entry["rich"] is not None or entry["textual"] is not None, name


def test_renderer_variables_are_generated_from_the_token_tables():
    """The tables are authoritative: a role cannot name a color it did not
    declare, and a declared role is always actually emitted."""
    tokens = theme.resolve_theme(depth=theme.ColorDepth.TRUECOLOR)
    built = theme.rich_theme(tokens)
    for role, (token_name, style_kwargs) in theme.RICH_ROLE_TOKENS.items():
        assert role.startswith("neo."), role
        assert token_name in theme.TOKEN_NAMES, (role, token_name)
        background = style_kwargs.get("background")
        if isinstance(background, str):
            assert background in theme.TOKEN_NAMES, (role, background)
        assert role in built.styles, role
        # The emitted role must be exactly the style the table declares —
        # a role that silently resolved to a different color is the drift
        # this table exists to prevent.  Rich normalizes hex case, so the
        # comparison is case-insensitive; nothing else may differ.
        assert (
            str(built.styles[role]).lower()
            == tokens.style(token_name, **style_kwargs).lower()
        ), role
    variables = theme.textual_variables(tokens)
    assert set(variables) == set(theme.TEXTUAL_VARIABLE_TOKENS)
    for variable, spec in theme.TEXTUAL_VARIABLE_TOKENS.items():
        source = spec[0] if isinstance(spec, tuple) else spec
        assert source in theme.TOKEN_NAMES, (variable, source)
        assert variables[variable].startswith("#"), variable


def test_state_markers_cover_every_state_the_terminal_prompt_names():
    """Hue is the first channel for a state, never the only one.

    Every state the visual contract enumerates — selection, focus, hover,
    pressed, streaming, approval, active, and the six outcome states — plus
    the two verification states, must carry a marker and a text label.  The
    label is what a renderer prints when there is no color at all, so it is
    part of the token contract, not decoration.
    """
    required = {
        "active",
        "selection",
        "focus",
        "hover",
        "pressed",
        "streaming",
        "approval",
        "success",
        "warning",
        "error",
        "info",
        "pending",
        "disabled",
        "verified",
        "unverified",
    }
    assert required <= set(theme.state_names())
    tokens = theme.resolve_theme(depth=theme.ColorDepth.TRUECOLOR)
    for name in theme.state_names():
        assert theme.state_marker(name), name
        assert theme.state_label(name), name
        assert theme.state_hue(name) in theme.TOKEN_NAMES, name
        assert tokens[theme.state_hue(name)].startswith("#"), name


def test_state_markers_degrade_to_ascii_and_never_to_nothing():
    """A legacy console must not crash on a marker, and an ASCII-only stream
    must still get something a reader can interpret."""

    class Encoded:
        def __init__(self, encoding: str) -> None:
            self.encoding = encoding

        def isatty(self) -> bool:
            return True

    utf8 = Encoded("utf-8")
    ascii_only = Encoded("ascii")
    for name in theme.state_names():
        pretty = theme.state_marker(name, stream=utf8)
        legacy = theme.state_marker(name, stream=ascii_only)
        assert pretty, name
        assert legacy, name
        # The ASCII form must be encodable on the stream it was chosen for.
        legacy.encode("ascii")
        # And the text label is always available, even for a state family the
        # caller invented (which yields no marker at all rather than a lie).
        assert theme.state_label(name) or not theme.state_marker(name)
    assert theme.state_marker("no-such-state", stream=utf8) == ""
    assert theme.state_label("no-such-state") == ""


def test_no_two_states_are_identical_without_hue():
    """The load-bearing accessibility assertion.

    At 16 colors the palette provably collapses (`accent_text` and `error`
    both resolve to #FF0000), and under NO_COLOR there is no hue at all.  So
    the (marker, label) pair must be unique per state at EVERY depth — if it
    were not, a state would be readable only as a color, which the terminal
    accessibility gate forbids.
    """
    for depth in theme.ColorDepth:
        pairs = {}
        for name in theme.state_names():
            channel = (theme.state_marker(name), theme.state_label(name))
            assert channel not in pairs, (depth.value, name, pairs.get(channel))
            pairs[channel] = name
        assert len(pairs) == len(theme.state_names())


def test_channel_report_names_the_hue_collisions_instead_of_hiding_them():
    """A collapse is a statement, not a silent acceptance."""
    sixteen = theme.channel_report(theme.resolve_theme(env={}, depth="16"))
    assert sixteen["hue"] is True
    assert sixteen["marker"] is True and sixteen["text"] is True
    collisions = {tuple(entry["states"]): entry for entry in sixteen["hue_collisions"]}
    assert ("active", "error") in collisions, sorted(collisions)
    active_error = collisions[("active", "error")]
    assert active_error["resolved"] == "#FF0000"
    assert "marker:◆" in active_error["separated_by"]
    assert "text:active" in active_error["separated_by"]
    # 16-color also folds four states onto white; a receipt that only
    # mentioned the crimson pair would be under-reporting.
    assert ("focus", "hover", "pending", "streaming") in collisions, sorted(collisions)
    assert sixteen["unmapped_tokens"] == {}

    truecolor = theme.channel_report(theme.resolve_theme(env={}, depth="truecolor"))
    assert ("active", "error") not in {
        tuple(entry["states"]) for entry in truecolor["hue_collisions"]
    }

    plain = theme.channel_report(theme.resolve_theme(env={}, depth="none"))
    assert plain["hue"] is False
    assert plain["hue_collisions"] == []
    # The count is still reported, and it is the TRUECOLOR count: the plain
    # depth keeps the full-palette values, so "how bad would hue be here" is
    # the truecolor answer. A report that dropped the number could not be
    # distinguished from a report that had nothing to report.
    assert plain["hue_collision_count"] == truecolor["hue_collision_count"] >= 1


def test_resolved_depth_probes_the_stream_when_the_caller_asserts_neither(monkeypatch):
    """Environment variables are a CEILING, not an answer.

    Measured before the fix: a pipe carrying `TERM=xterm-256color` resolved
    to 256-color, so `tokens.color_enabled` was True while the console
    emitted no escape at all.  Any renderer branching on that claim was
    branching on something the stream had already refused.
    """

    class Pipe(io.StringIO):
        def isatty(self):
            return False

    class Tty(io.StringIO):
        def isatty(self):
            return True

    env = {"TERM": "xterm-256color"}
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("NEO_NO_COLOR", raising=False)
    monkeypatch.delenv("NEO_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    assert theme.resolve_color_depth(env, stream=Pipe()) is theme.ColorDepth.NONE
    assert theme.resolve_color_depth(env, stream=Tty()) is theme.ColorDepth.ANSI256
    # An explicit is_tty keeps the historical env-only answer, which is what
    # the interactive surfaces rely on.
    assert theme.resolve_color_depth(env, is_tty=True) is theme.ColorDepth.ANSI256
    # With no stream asserted at all, the real sys.stdout decides. Under
    # pytest that is a capture, so the honest answer is "no color".
    assert theme.resolve_color_depth(env) is theme.ColorDepth.NONE
    assert theme.resolve_theme(env=env).depth is theme.ColorDepth.NONE
    assert theme.resolve_theme(env=env).color_enabled is False


def test_an_unprobeable_stream_is_unknown_not_a_refusal():
    """ "I could not ask" must not become "definitely not a terminal".

    A stream double with no `isatty` cannot answer the question, and a caller
    that treated that as a refusal would strip color from a stream that can
    render it.
    """

    class Opaque:
        pass

    assert theme.stream_is_tty(Opaque()) is None
    assert theme.stream_is_tty(io.StringIO()) is False

    class Boom:
        def isatty(self):
            raise OSError("no terminal here")

    assert theme.stream_is_tty(Boom()) is None


def test_color_enabled_never_claims_color_the_stream_cannot_carry():
    """`ui.color_enabled()` is the public answer; it must be a measured one."""
    env = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}

    class Pipe(io.StringIO):
        def isatty(self):
            return False

    assert theme.resolve_color_depth(env, stream=Pipe()) is theme.ColorDepth.NONE
    # The explicit-TTY arm still permits color: an interactive surface that
    # knows it is on a terminal must not be told otherwise.
    assert theme.resolve_color_depth(env, is_tty=True) is theme.ColorDepth.TRUECOLOR


@pytest.mark.parametrize("depth", ["truecolor", "256", "16", "none"])
def test_theme_preview_states_the_degradation_at_every_depth(depth):
    """The theme preview/fallback test.

    The preview is the token system's own demonstration surface, so it has
    to be readable at the depth it is previewing: it names the depth, keeps
    the state channel visible, reports the collision count, and states the
    token coverage — including at `none`, where it must say plainly that no
    color is emitted rather than pretending the swatches mean something.
    """
    tokens = theme.resolve_theme(env={}, depth=depth)
    preview = theme.theme_preview(tokens)
    assert f"depth: {depth}" in preview
    assert "state channels (marker + text survive every depth):" in preview
    assert "token coverage: 29/29" in preview
    assert "channels: hue" in preview
    for name in theme.state_names():
        assert f"  {name:<11} " in preview, name
    report = theme.channel_report(tokens)
    assert f"collisions {report['hue_collision_count']}" in preview
    if depth == "none":
        assert "[#000000]" not in preview
        assert "[ ]" in preview
        assert "no color is emitted at this depth" in preview
    else:
        assert "[#000000]" in preview


def test_theme_report_includes_the_state_and_coverage_receipts():
    report = ui.theme_report("high-contrast", depth="256")
    assert report["channels"]["depth"] == "256"
    assert report["channels"]["unmapped_tokens"] == {}
    assert set(report["states"]) == set(theme.state_names())
    assert set(report["coverage"]) == set(theme.TOKEN_NAMES)
    # The report must stay JSON-serializable: it is what `/theme` and the
    # support bundle hand to a machine.
    import json

    json.dumps(report)


#: Modules that draw terminal chrome.  A literal color in one of these is a
#: second, competing source of truth, which is exactly what the single token
#: system exists to prevent.
CHROME_MODULES = (
    "cli/ui.py",
    "cli/tui.py",
    "cli/tui_components.py",
    "cli/runview.py",
    "cli/tracelog.py",
    "cli/streamview.py",
    "cli/headless.py",
    "cli/notify.py",
    "cli/background.py",
    "cli/command_exec.py",
    "cli/interactive.py",
    "cli/onboard.py",
    "cli/doctor.py",
    "cli/fileview.py",
    "cli/commands.py",
    "cli/main.py",
    "cli/session.py",
    "cli/errors.py",
    "cli/deps.py",
)

#: Documented, deliberate exceptions.  Each one is a value the design system
#: names as exempt; a new entry needs a reason in this table, which is what
#: makes adding one a decision rather than a convenience.
LITERAL_COLOR_EXEMPTIONS = {
    ("cli/ui.py", 1317): (
        "_NEO_RAMP is the LOCKED logo gradient; NEO_DESIGN_SYSTEM.md exempts "
        "its per-column interpolation stops from the token contract"
    ),
}

_HEX_LITERAL = re.compile(r"#[0-9A-Fa-f]{6}\b")
_NAMED_LITERAL = re.compile(
    r"(?<![\w-])(grey\d{1,2}|gray\d{1,2}|orange\d{0,2}|darkorange|indianred"
    r"|sienna|peru|tan|beige|wheat|salmon|coral|gold|khaki|plum|orchid|violet"
    r"|purple|navy|teal|maroon|olive|aqua|fuchsia|lime|brown|azure|lavender"
    r"|firebrick|green3|red3|blue3|magenta3|cyan3|yellow3)(?![\w-])"
)
_SKIP_TOKENS = {
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.COMMENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}


def _code_tokens(source: str):
    """Yield (lineno, text) for every token that is not a comment or docstring.

    Comments and docstrings are excluded deliberately: both are places the
    design system NAMES its own colors, and a gate that flagged the prose
    explaining the palette would be a gate nobody keeps.
    """
    tokens = [
        token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type != tokenize.ENCODING
    ]
    docstrings: set = set()
    line: list = []
    for token in tokens:
        if token.type in {tokenize.NL, tokenize.NEWLINE}:
            if len(line) == 1 and line[0].type == tokenize.STRING:
                docstrings.add(line[0].start)
            line = []
        elif token.type not in _SKIP_TOKENS:
            line.append(token)
    if len(line) == 1 and line[0].type == tokenize.STRING:
        docstrings.add(line[0].start)
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.start in docstrings:
            continue
        yield token.start[0], token.string


def test_no_literal_color_outside_the_token_authority():
    """Semantic styles, not scattered literal colors — enforced, not asked.

    `cli/theme.py` holds the palette and is therefore the authority; this
    scans every module that draws chrome and fails on a hex literal or a
    Rich color NAME in executable text.  A renderer that reaches for
    `"#34D399"` instead of `tokens["success"]` is a second source of truth,
    and it is the specific failure this gate exists to catch.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    findings: list[str] = []
    for relative in CHROME_MODULES:
        source = (root / relative).read_text(encoding="utf-8")
        for lineno, text in _code_tokens(source):
            for hit in (*_HEX_LITERAL.finditer(text), *_NAMED_LITERAL.finditer(text)):
                if (relative, lineno) in LITERAL_COLOR_EXEMPTIONS:
                    continue
                findings.append(f"{relative}:{lineno} {hit.group(0)!r}")
    listing = "\n".join(findings)
    assert not findings, (
        "literal color(s) in a chrome module; use the semantic token instead:\n"
        + listing
    )


def test_every_literal_color_exemption_is_still_used():
    """An exemption that no longer matches anything is a stale escape hatch."""
    root = pathlib.Path(__file__).resolve().parents[1]
    for relative, lineno in LITERAL_COLOR_EXEMPTIONS:
        lines = (root / relative).read_text(encoding="utf-8").splitlines()
        assert 0 < lineno <= len(lines), (relative, lineno)
        line = lines[lineno - 1]
        assert _HEX_LITERAL.search(line), (
            f"{relative}:{lineno} no longer contains a hex literal; drop the exemption"
        )


def test_the_token_module_is_the_only_palette():
    """The palette lives in one file, so a reader can find the whole system."""
    root = pathlib.Path(__file__).resolve().parents[1]
    others = []
    for module in root.glob("cli/*.py"):
        if module.name == "theme.py":
            continue
        text = module.read_text(encoding="utf-8")
        for marker in ("_DEFAULT_COLORS", "_HIGH_CONTRAST_COLORS", "_FALLBACK_256"):
            if marker in text:
                others.append(f"{module.name}:{marker}")
    assert not others, others
