"""Vex CLI presentation layer — rich Console + the Vex theme, shared by
every command (Boundary 6).

Cross-platform notes (Task A):
- ONE console instance (`console()`); rich auto-detects Windows terminal
  capability (ANSI on Windows Terminal / VT-enabled conhost; plain-text
  degradation when piped or on legacy consoles) — verified rather than
  assumed: `vex status` etc. render identically when stdout is piped.
- `--no-color` / NO_COLOR env force plain output globally (rich's
  NO_COLOR support + our flag hook).

Theme (Task B) — amber/ember palette, warm-technical, distinct from
Claude Code orange:
  vex.accent   burnt amber   #e8722a  headers, emphasis
  vex.running  soft gold     #e6b84c  in-progress/running states
  vex.ok       green3                success
  vex.error    red3                  errors/failures
  vex.warn     orange1               warnings
  vex.muted    grey58                secondary text
  vex.diff.add/del/meta           diff rendering
"""

from __future__ import annotations

import io
import os
import sys
from functools import lru_cache
from typing import Optional

from rich.console import Console
from rich.status import Status
from rich.theme import Theme

VEX_THEME = Theme(
    {
        "vex.accent": "#e8722a bold",
        "vex.running": "#e6b84c",
        "vex.ok": "green3",
        "vex.error": "red3",
        "vex.warn": "orange1",
        "vex.muted": "grey58",
        "vex.diff.add": "green3",
        "vex.diff.del": "red3",
        "vex.diff.meta": "#e6b84c",
        "vex.diff.hunk": "grey58",
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
}


def set_no_color(flag: bool) -> None:
    """Force-disable color for this process (CLI --no-color flag)."""
    global _no_color
    _no_color = flag


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
    """Render a unified diff with rich: line-per-line +/- coloring,
    hunk headers muted, file headers in gold.

    Assumes `text` is unified-diff output (as produced by the harness's
    unified_diff / git diff). Non-diff text prints as-is (muted).
    """
    con = console()
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
