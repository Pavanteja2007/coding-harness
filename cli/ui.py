"""Vex CLI presentation layer — rich Console + the Vex theme, shared by
every command (Boundary 6).

Cross-platform notes (Task A):
- ONE console instance (`console()`); rich auto-detects Windows terminal
  capability (ANSI on Windows Terminal / VT-enabled conhost; plain-text
  degradation when piped or on legacy consoles) — verified rather than
  assumed: `vex status` etc. render identically when stdout is piped.
- `--no-color` / NO_COLOR force plain output globally (rich's
  NO_COLOR support + our flag hook).

Theme (crimson-on-black re-theme, 2026-09-22) — the locked palette
from VEX_DESIGN_SYSTEM.md. Two weights of crimson, mirroring the
landing page's fill/text split:
  #4A0A14  accent-primary — the FILL weight (cursor/selection blocks,
           badge fills; always under #F5F5F5 text). ~1.5:1 as text —
           never used for glyphs or thin borders on dark grounds.
  #E8114A  the logo's own crimson, extracted from the live wordmark
           ramp (the doc's logo clause) — the TEXT weight (~4.6:1 on
           #000000, measured — see the ramp note below): prompts,
           emphasis, active states.
Roles map to the doc tokens:
  vex.accent   #E8114A bold  logo crimson — prompt symbol, emphasis
  vex.accent2  #E8114A        same crimson, regular weight — ids/labels
  vex.running  #E8114A        active/in-progress states (active = accent,
                              per the doc; never orange)
  vex.glow     #FF7A93        accent-glow — the rose tone for the
                              thinking spinner and diff file headers
  vex.ok       #34D399  success — actual success states only
  vex.error    #F87171  errors/failures
  vex.warn     #FBBF24  warnings — real emphasis only (approval gates)
  vex.muted    #8A8A8A  text-secondary — all muted/secondary text
  vex.diff.add #34D399 / del #F87171 / meta #FF7A93 / hunk #8A8A8A
The wordmark ramp (_VEX_RAMP) is the locked logo — crimson→rose→
white-hot. Its per-column interpolation stops are the ONLY sanctioned
non-token colors (documented exemption); the current ramp's blends
are pinks, never orange.
"""

from __future__ import annotations

import io
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from rich.console import Console
from rich.status import Status
from rich.theme import Theme

if TYPE_CHECKING:  # annotation-only import (the render helpers return Texts)
    from rich.text import Text

#: The locked design tokens (VEX_DESIGN_SYSTEM.md) — the single source
#: both surfaces (rich REPL + textual TUI) import. The CLI is a text
#: medium: bg/border/fill tokens are used where CSS would (backgrounds,
#: borders, cursor/selection fills) via the TUI stylesheet + theme
#: variables; text glyphs always use the legible weights.
BG_BASE = "#000000"
BG_PANEL = "#0A0A0A"
BG_PANEL_HOVER = "#161616"
BORDER_SUBTLE = "#2A2A2A"
ACCENT_PRIMARY = "#4A0A14"
ACCENT_TEXT = "#E8114A"  # the logo's own crimson (wordmark ramp stop 2)
ACCENT_GLOW = "#FF7A93"
TEXT_PRIMARY = "#F5F5F5"
TEXT_SECONDARY = "#8A8A8A"
SUCCESS = "#34D399"
ERROR = "#F87171"
WARNING = "#FBBF24"

VEX_THEME = Theme(
    {
        "vex.accent": f"{ACCENT_TEXT} bold",
        "vex.accent2": ACCENT_TEXT,
        "vex.running": ACCENT_TEXT,
        "vex.glow": ACCENT_GLOW,
        "vex.ok": SUCCESS,
        "vex.error": ERROR,
        "vex.warn": WARNING,
        "vex.muted": TEXT_SECONDARY,
        "vex.diff.add": SUCCESS,
        "vex.diff.del": ERROR,
        "vex.diff.meta": ACCENT_GLOW,
        "vex.diff.hunk": TEXT_SECONDARY,
    }
)

# Global kill-switch consulted by console() (set by --no-color / NO_COLOR).
_no_color: Optional[bool] = None


def _enc_ok(ch: str) -> bool:
    """True when stdout's encoding can render `ch` (Windows legacy
    consoles are cp1252: →, ›, ✔ crash the rich writer — verified live).
    Used to pick pretty vs ASCII-fallback glyphs at import time."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        ch.encode(enc, errors="strict")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _glyph(pretty: str, ascii_fallback: str) -> str:
    return pretty if _enc_ok(pretty) else ascii_fallback


#: Cross-platform glyphs (Task A: pretty on UTF-8 terminals incl. Windows
#: Terminal; ASCII on legacy cp1252 consoles — crashes otherwise).
GLYPHS = {
    "arrow": _glyph("→", "->"),
    "prompt": _glyph("›", ">"),
    "bullet": _glyph("●", "*"),
    "ok": _glyph("✔", "OK"),
    "fail": _glyph("✘", "x"),
    "wait": _glyph("⏱", "T"),
    "sad": _glyph("•", "-"),
    "ember": _glyph("◆", "*"),
}

#: Spinner set safe for the current console's encoding. rich's default
#: spinners ("dots" etc.) are braille — UnicodeEncodeError on cp1252
#: consoles when ANSI is otherwise forced (found by probe, 2026-09-13;
#: same bug class as the glyph fallbacks above).
SPINNER = "dots" if _enc_ok("\u280b") else "line"

#: Thinking-frame set (distinct ORBIT glyph — model-thinking phases
#: look different from the linear spinner at a glance; ASCII fallback).
THINK_FRAMES = "◐◓◑◒" if _enc_ok("◐") else "|/-\\"

#: Technical one-liners shown while the model thinks (Qwen-Code-style
#: "still working" flavor; all tech, all ASCII — safe on any console).
JOKES = (
    "it's not a bug, it's an undocumented feature... no wait, it's a bug",
    "compiling excuses... none found... generating patch instead",
    "99 little bugs in the code, take one down, patch it around",
    "works on my machine -- but the sandbox is my machine",
    "reading the docs you didn't... someone had to",
    "recursion works: to understand it, see 'recursion works'",
    "the tests pass; the tests always pass; trust the tests",
    "stack overflowed once just reading this function",
    "there are two hard problems: naming, and off-by-one",
    "git blame says it was you. git also says it was 3am",
    "sudo make me a sandwich -- denied: read-only sandbox",
    "the deadline was yesterday; the build was 4 minutes ago",
    "cache invalidation solved: we just don't cache",
    "null pointers: the billion-dollar mistake, right on schedule",
    "hold on, interpolating between intent and implementation",
    "debugging: being the detective in a crime you didn't commit",
    "chmod +x the fix... no wait, that's how we got here",
    "the verifier will judge; the verifier judges everyone",
)


def joke_at(i: int) -> str:
    """Deterministic joke pick for index i (cycles the JOKES tuple;
    never raises, always ASCII)."""
    return JOKES[i % len(JOKES)]


def set_no_color(flag: bool) -> None:
    """Force-disable color for this process (CLI --no-color flag)."""
    global _no_color
    _no_color = flag
    # the shared console may already exist (eager import-time
    # construction) — rebuild it with the flag applied.
    console.cache_clear()


@lru_cache(maxsize=1)
def console() -> Console:
    """The shared Vex console (theme applied; color auto-degrades when
    the terminal can't render ANSI — rich handles Windows detection)."""
    return Console(
        theme=VEX_THEME,
        highlight=False,  # no surprise re-highlight of numbers
        soft_wrap=False,
        no_color=_no_color or None,
        # stderr stays separate: helpers below route errors to stderr
    )


# Eager construction: rich resolves the color system ONCE at Console
# creation (from the then-current sys.stdout.isatty()). If the first
# console() call ever happened while textual's app capture had
# replaced sys.stdout (TUI tests / the app itself), the lru_cached
# console would freeze a WRONG color system (seen live: color_system
# "windows" stuck for the whole process, ANSI codes leaking into
# piped output afterwards). Constructing at import time — before any
# test/app swaps the streams — pins the detection to the real stdout.
console()


def err_console() -> Console:
    """Themed console bound to stderr (errors/warnings)."""
    return Console(
        theme=VEX_THEME,
        highlight=False,
        no_color=_no_color or None,
        stderr=True,
    )


def supports_ansi() -> bool:
    """True when the shared console will actually emit ANSI (Task A
    verification hook — used by the degradation test, not the hot path)."""
    c = console()
    buf = io.StringIO()
    probe = Console(
        file=buf, theme=VEX_THEME, force_terminal=c.is_terminal, no_color=c.no_color
    )
    probe.print("[vex.accent]x[/]")
    return "\x1b[" in buf.getvalue()


# ---------------------------------------------------------------------------
# Task C — live status indicators
# ---------------------------------------------------------------------------


def status(text: str, *args, **kw) -> Status:
    """A themed rich Status spinner bound to the shared console.

    Usage (auto-stops, even on exception):
        with status("fixing (model thinking)...") as st:
            ...
            st.update("verifying in sandbox...")
    """
    kw.setdefault("spinner", SPINNER)
    return console().status(f"[vex.running]{text}[/]", *args, **kw)


def fmt_cost(usd: float) -> str:
    """Compact human cost: $0.0000 / $0.0123 / $1.24."""
    if usd >= 1:
        return f"${usd:.2f}"
    if usd >= 0.01:
        return f"${usd:.4f}"
    return f"${usd:.6f}"


# ---------------------------------------------------------------------------
# Task D — diff rendering (syntax-highlighted, +/- colored)
# ---------------------------------------------------------------------------


def print_diff(text: str) -> None:
    """Render a unified diff with rich: language-aware syntax
    highlighting of the code content (pygments via `rich.Syntax` — the
    same stack the TUI's inline preview uses, one shared renderer),
    hunk headers muted, file headers in gold.

    Assumes `text` is unified-diff output (as produced by the harness's
    unified_diff / git diff). Non-diff text prints as-is (muted). When
    pygments can't identify a file's language the lines still carry
    their +/-/@@ diff coloring (never a crash, never a blank block).
    """
    con = console()
    try:
        con.print(diff_text(text), highlight=False)
    except Exception:
        # Degraded path: pure +/-/hunk role coloring, no lexing (a
        # view must never take a fix's reporting down).
        for line in (text or "").splitlines():
            if line.startswith("+++") or line.startswith("---"):
                con.print(line, style="vex.diff.meta")
            elif line.startswith("@@"):
                con.print(line, style="vex.diff.hunk")
            elif line.startswith("+"):
                con.print(line, style="vex.diff.add")
            elif line.startswith("-"):
                con.print(line, style="vex.diff.del")
            else:
                con.print(line)


# ---------------------------------------------------------------------------
# Task B (interaction-polish round) — syntax-highlighted diffs
# ---------------------------------------------------------------------------

#: Diff-line roles: the character at the line start decides. The styles
#: are CONCRETE token hexes (not `vex.diff.*` theme-role names) because
#: these Text objects go straight into textual's RichLog, which has no
#: access to the rich VEX_THEME — the roles live in `print_diff`'s
#: degraded path (which prints via the themed shared console).
#: Backgrounds are the dark ends of the success/error tokens blended
#: toward bg-base — a readable add/del band under the pygments colors.
_DIFF_META_STYLE = ACCENT_GLOW
_DIFF_HUNK_STYLE = TEXT_SECONDARY
_DIFF_OTHER_STYLE = TEXT_SECONDARY
_DIFF_ADD_BG = "#0A1510"
_DIFF_DEL_BG = "#1C0A0F"
_DIFF_ADD_FG = SUCCESS
_DIFF_DEL_FG = ERROR


def _diff_lexer(name: str):
    """The pygments lexer for a file NAME (best effort, never raises):
    by filename first (`Makefile`/`Dockerfile` are real pygments
    names), then by extension, then by content-agnostic text. Returns
    None when nothing matches — the caller falls back to plain text."""
    if not name:
        return None
    try:
        from pygments.lexers import guess_lexer_for_filename

        lx = guess_lexer_for_filename(name, "")
        # pygments returns a TextLexer when it gives up; that adds nothing.
        if lx and lx.name not in ("Text only", "Text"):
            return lx
    except Exception:
        pass
    try:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext:
            from pygments.lexers import get_lexer_by_name

            mapping = {
                "py": "python",
                "js": "javascript",
                "ts": "typescript",
                "tsx": "tsx",
                "jsx": "jsx",
                "go": "go",
                "rs": "rust",
                "rb": "ruby",
                "java": "java",
                "c": "c",
                "h": "c",
                "cpp": "cpp",
                "sh": "bash",
                "bash": "bash",
                "json": "json",
                "toml": "toml",
                "yaml": "yaml",
                "yml": "yaml",
                "md": "markdown",
                "html": "html",
                "css": "css",
                "sql": "sql",
            }
            if ext in mapping:
                return get_lexer_by_name(mapping[ext])
    except Exception:
        pass
    return None


class _DiffHighlighter:
    """Stateful per-line diff renderer: tracks the current file (from
    `+++ b/<name>` headers) and lexes each content line with that
    file's language, so a Python diff shows Python tokens, a JSON diff
    shows JSON tokens, etc.

    Total by contract: every failure (unknown language, lexer crash)
    degrades to the flat +/-/@@ coloring — the diff's own information
    (what changed) never depends on the syntax layer succeeding.
    """

    def __init__(self) -> None:
        self._cache: dict = {}
        self._current = None

    def _lexer_for(self, name: str):
        key = name or ""
        if key not in self._cache:
            self._cache[key] = _diff_lexer(name)
        return self._cache[key]

    def line(self, line: str):
        """One unified-diff line as a styled rich Text."""
        from rich.text import Text

        if line.startswith("+++") or line.startswith("---"):
            # File headers also SELECT the language for the hunk that
            # follows (the +++ side is the new content — what's on +/ctx
            # lines, and the closer to what - lines were before the
            # rename, so it is the better lexer cue).
            if line.startswith("+++"):
                self._current = self._lexer_for(_diff_filename(line[6:]))
            return Text(line, style=_DIFF_META_STYLE)
        if line.startswith("@@"):
            return Text(line, style=_DIFF_HUNK_STYLE)
        if line.startswith("+"):
            return self._content("+", line[1:], _DIFF_ADD_FG, _DIFF_ADD_BG)
        if line.startswith("-"):
            return self._content("-", line[1:], _DIFF_DEL_FG, _DIFF_DEL_BG)
        if line.startswith(" "):
            return self._content(" ", line[1:], None, None)
        # binary/truncation markers, "\ No newline", git index noise:
        # secondary info, never a random success/error color.
        return Text(line, style=_DIFF_OTHER_STYLE)

    def _content(self, marker: str, code: str, fg, bg):
        """A content line with optional fg/bg for the diff role; the
        code portion gets pygments token colors over that base."""
        from rich.style import Style
        from rich.text import Text

        base = Style.parse(
            " ".join(s for s in (f"{fg}" if fg else "", f"on {bg}" if bg else "") if s)
            or "none"
        )
        out = Text()
        out.append(marker, style=base)
        lexer = self._current
        if lexer is not None and code.strip():
            try:
                from rich.syntax import Syntax

                syn = Syntax(
                    code,
                    lexer=lexer,
                    theme="ansi_dark",
                    background_color=bg or BG_BASE,
                    word_wrap=False,
                )
                inner = syn.highlight(code)
                # pygments emits a trailing newline for every snippet;
                # the joiner adds line breaks, so drop it (a Text slice
                # keeps the token spans intact).
                if inner.plain.endswith("\n"):
                    inner = inner[: len(inner.plain) - 1]
                # overlay token colors on top of the whole-code base
                # span (Task B): keep ONE base span covering the code so
                # the +/- role still reads, then stylize tokens on top —
                # both survive into textual's RichLog.
                out.append(code, style=base)
                off = len(marker)
                for span in inner.spans:
                    if span.style is None:
                        continue
                    try:
                        out.stylize(
                            span.style
                            if not isinstance(span.style, str)
                            else Style.parse(span.style),
                            off + span.start,
                            off + span.end,
                        )
                    except Exception:
                        pass
                return out
            except Exception:
                pass  # lexer/theme blew up: fall through to plain diff role
        out.append(code, style=base)
        return out


def _diff_filename(after_header: str) -> str:
    """The path from a `+++ b/<path>` tail (strips tab-timestamps git
    appends; tolerates the missing `b/` prefix in hand-made diffs)."""
    name = (after_header or "").strip().split("\t")[0].strip()
    if name.startswith("b/"):
        name = name[2:]
    return name


def diff_text(diff: str, *, width: int = 0) -> "Text":
    """A whole unified diff as ONE rich Text — language-aware syntax
    highlighting over the +/-/@@ diff roles (Task B of the interaction
    polish round). Shared by every surface: the TUI's inline preview,
    the REPL's /diff, `vex fix`'s final diff.

    Assumes unified-diff shape; any other text comes back mostly plain
    (the classifier only ever ADDS styling). Never raises.
    """
    out = None
    for line in diff_render_lines((diff or "").splitlines()):
        if out is None:
            from rich.text import Text

            out = line
        else:
            from rich.text import Text

            out.append_text(Text("\n"))
            out.append_text(line)
    if out is None:
        from rich.text import Text

        out = Text("")
    return out


def diff_render_lines(diff: Any) -> list:
    """A unified diff (string, or the list of diff LINE STRINGS, or the
    (text, kind) tuples cli.tracelog.live_diff returns) as a list of
    per-line rich Texts, each language-aware syntax highlighted over its
    +/-/@@ role.

    Shared renderer for the TUI inline preview, the approval modal diff
    body, and the REPL / flag-command diff (Task B). The (text, kind)
    form ignores `kind` and re-derives it from the line prefix so the
    SAME classifier colors every surface identically; binary/
    truncation markers fall through to plain. Never raises.
    """
    hl = _DiffHighlighter()
    if isinstance(diff, str):
        raw: list = diff.splitlines()
    else:
        raw = list(diff or [])
    out: list = []
    for item in raw:
        line = item[0] if isinstance(item, tuple) else str(item)
        out.append(hl.line(line))
    return out


# ---------------------------------------------------------------------------
# Task F (interaction-polish round) — completion notification
# ---------------------------------------------------------------------------


def bell(message: str = "") -> None:
    """Signal that a task finished — the terminal bell (`\\a`) plus, on
    Windows, a non-blocking `MessageBeep` (stdlib ctypes; covers the
    terminals where the in-band bell is swallowed). Fire-and-forget by
    design: a UI nicety must never raise into a finished run's
    reporting. TTY-ONLY (a \\a byte in a PIPE is literal garbage — a
    piped `vex fix` must stay byte-clean). Set `VEX_NOTIFY=0` to
    silence (tests / CI / ssh)."""
    import os
    import sys

    if os.environ.get("VEX_NOTIFY", "").strip().lower() in ("0", "false", "no"):
        return
    try:
        if not (sys.stderr.isatty() or sys.stdout.isatty()):
            return  # never ring into a pipe or a log
    except Exception:
        return
    try:
        sys.stderr.write("\a")
        sys.stderr.flush()
    except Exception:
        pass
    if os.name == "nt":  # 0x40 = MB_ICONASTERISK
        try:
            import ctypes

            ctypes.windll.user32.MessageBeep(0x40)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Branding — gradient wordmark, splash, compact header (Tasks A/B/C)
# ---------------------------------------------------------------------------

#: ANSI-shadow wordmark (box-drawing double-line letterforms — the
#: figlet style OpenCode/Claude Code-class CLIs use). '╔' & co. crash
#: legacy cp1252 consoles, so wordmark_lines() probe-picks this vs the
#: '#' block fallback below. Rows are ljust'd to a uniform width so
#: the gradient spans identical spans per row.
_WORDMARK_ANSI = [
    r"██╗   ██╗███████╗██╗  ██╗".ljust(26),
    r"██║   ██║██╔════╝╚██╗██╔╝".ljust(26),
    r"██║   ██║█████╗   ╚███╔╝ ".ljust(26),
    r"╚██╗ ██╔╝██╔══╝   ██╔██╗ ".ljust(26),
    r" ╚████╔╝ ███████╗██╔╝ ██╗".ljust(26),
    r"  ╚═══╝ ╚══════╝╚═╝  ╚═╝".ljust(26),
]
#: '#' block fallback (the original 26-col block letterforms, plain
#: ASCII): for consoles whose encoding can render neither
#: box-drawing nor shade glyphs (exotic 7-bit consoles).
_WORDMARK_BLOCK = [
    "#     #   ######   #     #",
    "#     #   #         #   # ",
    "#     #   #          ###  ",
    " #   #    #####      ###  ",
    "  # #     #         #   # ",
    "   #      ######   #     #",
]

#: The brand gradient: deep crimson -> accent -> rose -> white-hot.
#: Dark-to-light across the wordmark is the "modern CLI" look; each
#: row interpolates horizontally (per-column color stops). The TEXT
#: weight (#E8114A, stop 2) is the crimson one step brighter than
#: #DC143C in the same hue: #DC143C measures ~4.2:1 on #000000
#: (below the 4.5:1 bar), #E8114A measures ~4.6:1 — brightness
#: adjusted, hue kept. Per-column blends are the only sanctioned
#: non-token colors (pinks on this ramp, never orange).
_VEX_RAMP = ("#7A0C26", "#E8114A", "#FF7A93", "#FFF0F3")

TAGLINE = "the AI harness that fixes bugs — verified, not vibed"

#: Dot separator (cp1252-safe, unlike the box-drawing glyphs).
DOT = "·"


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _interp(c1, c2, t: float):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2, strict=False))


def gradient_text(s: str, ramp=_VEX_RAMP):
    """A rich Text with a horizontal color gradient across `s`.

    Assumes a non-empty string; ramp holds >=2 hex colors. Used for
    the wordmark rows (per-column stops), works on any console rich
    can render (per-segment truecolor/256/ansi degrade is rich's).
    """
    from rich.text import Text

    hexes = [_hex_to_rgb(x) for x in ramp]
    if len(s) <= 1:
        return Text(s, style=ramp[0])
    out = Text()
    segs = len(hexes) - 1
    for i, ch in enumerate(s):
        t = i / (len(s) - 1) * segs
        si = min(int(t), segs - 1)
        r, g, b = _interp(hexes[si], hexes[si + 1], t - si)
        out.append(ch, style=f"#{r:02x}{g:02x}{b:02x}")
    return out


def wordmark_lines() -> list:
    """The VEX wordmark rows, encoding-safe for this console."""
    if _enc_ok("╔"):
        return list(_WORDMARK_ANSI)
    return [r for r in _WORDMARK_BLOCK]


def print_splash(
    repo: "os.PathLike",
    log_root: "os.PathLike",
    model: str = "router (adaptive)",
    version: str = "",
) -> None:
    """The empty-session welcome screen (Task A + C).

    Shown ONCE — first launch into an empty session (no recorded
    sessions under the log root), matching OpenCode's empty-state
    pattern. Regular sessions get the compact header instead.
    Assumes repo/log_root are display strings (Paths are str()'d).
    """
    from rich.rule import Rule
    from rich.text import Text

    con = console()
    con.print()
    rows = wordmark_lines()
    if _enc_ok("╔"):
        # gradient wordmark (the modern look); fallback rows render flat
        for row in rows:
            con.print(gradient_text(row), highlight=False)
    else:
        for row in rows:
            con.print(f"[vex.accent]{row}[/]", highlight=False)
    con.print()
    con.print(
        Text("  the AI harness that fixes bugs ", style=TEXT_SECONDARY)
        + Text("·", style=ACCENT_TEXT)
        + Text(" verified, not vibed", style=TEXT_PRIMARY)
    )
    con.print()
    con.print(Rule(style=BORDER_SUBTLE))
    con.print()
    # aligned info rows: dim label column, clean value column
    con.print(f"  [vex.muted]repo [/] [bold {TEXT_PRIMARY}]{Path(repo)}[/]")
    con.print(f"  [vex.muted]logs [/] [{TEXT_PRIMARY}]{Path(log_root)}[/]")
    con.print(f"  [vex.muted]model[/] [vex.accent2]{model}[/]")
    if version:
        con.print(f"  [vex.muted]vex  [/] [{TEXT_PRIMARY}]{version}[/]")
    con.print()
    con.print(
        f"  [vex.muted]describe what's wrong in plain language[/] "
        f"[{TEXT_SECONDARY}]{DOT}[/] [vex.accent]/help[/] "
        f"[{TEXT_SECONDARY}]for commands[/]"
    )
    con.print()


def print_compact_header(
    repo: "os.PathLike",
    model: str = "router (adaptive)",
    version: str = "",
    status_text: str = "",
) -> None:
    """The per-session one-line header (Task C, Claude Code pattern).

    Shown on every regular session start (not the splash): version,
    current model, repo, and an optional status word. No giant art.
    """
    con = console()
    ember = GLYPHS["ember"]
    bits = f"[vex.accent]{ember} vex[/]"
    if version:
        bits += f" [vex.muted]{version}[/]"
    bits += f"  [vex.muted]{DOT}[/] [vex.muted]model[/] [vex.accent2]{model}[/]"
    bits += f"  [vex.muted]{DOT}[/] [vex.muted]{Path(repo)}[/]"
    if status_text:
        bits += f"  [vex.muted]{DOT}[/] [vex.running]{status_text}[/]"
    con.print(bits)
