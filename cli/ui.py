"""Neo CLI presentation layer — rich Console + the Neo theme, shared by
every command (Boundary 6).

Cross-platform notes (Task A):
- ONE console instance (`console()`); rich auto-detects Windows terminal
  capability (ANSI on Windows Terminal / VT-enabled conhost; plain-text
  degradation when piped or on legacy consoles) — verified rather than
  assumed: `neo status` etc. render identically when stdout is piped.
- `--no-color` / NO_COLOR force plain output globally (rich's
  NO_COLOR support + our flag hook).

Theme (crimson-on-black re-theme, 2026-09-22) — the locked palette
from NEO_DESIGN_SYSTEM.md. Two weights of crimson, mirroring the
landing page's fill/text split:
  #4A0A14  accent-primary — the FILL weight (cursor/selection blocks,
           badge fills; always under #F5F5F5 text). ~1.5:1 as text —
           never used for glyphs or thin borders on dark grounds.
  #E8114A  the logo's own crimson, extracted from the live wordmark
           ramp (the doc's logo clause) — the TEXT weight (~4.6:1 on
           #000000, measured — see the ramp note below): prompts,
           emphasis, active states.
Roles map to the doc tokens:
  neo.accent   #E8114A bold  logo crimson — prompt symbol, emphasis
  neo.accent2  #E8114A        same crimson, regular weight — ids/labels
  neo.running  #E8114A        active/in-progress states (active = accent,
                              per the doc; never orange)
  neo.glow     #FF7A93        accent-glow — the rose tone for the
                              thinking spinner and diff file headers
  neo.ok       #34D399  success — actual success states only
  neo.error    #F87171  errors/failures
  neo.warn     #FBBF24  warnings — real emphasis only (approval gates)
  neo.muted    #8A8A8A  text-secondary — all muted/secondary text
  neo.diff.add #34D399 / del #F87171 / meta #FF7A93 / hunk #8A8A8A
The wordmark ramp (_NEO_RAMP) is the locked logo — crimson→rose→
white-hot. Its per-column interpolation stops are the ONLY sanctioned
non-token colors (documented exemption); the current ramp's blends
are pinks, never orange.
"""

from __future__ import annotations

import io
import itertools
import os
import re
import sys
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional

from rich.console import Console
from rich.status import Status

from cli.theme import (
    ColorDepth,
    TerminalTokens,
    channel_report,
    is_dumb_terminal,
    is_legacy_encoding,
    resolve_color_depth,
    resolve_theme,
    rich_theme,
    state_label,
    state_marker,
    state_markers,
    token_coverage,
    unmapped_tokens,
)
from cli.theme import textual_variables as _textual_variables
from cli.theme import theme_names as _theme_names
from cli.theme import theme_preview as _theme_preview
from cli.theme import theme_summary as _theme_summary
from shared.security import redact_text

if TYPE_CHECKING:  # annotation-only import (the render helpers return Texts)
    from rich.text import Text

_DEFAULT_TOKENS = resolve_theme(depth=ColorDepth.TRUECOLOR)
_ENV_THEME_CONTROLLED = (
    any(
        key in os.environ
        for key in (
            "NO_COLOR",
            "NEO_NO_COLOR",
            "NEO_COLOR_DEPTH",
            "FORCE_COLOR",
            "NEO_THEME",
            "NEO_THEME_OVERRIDES",
            "TERM",
            "COLORTERM",
        )
    )
    or is_dumb_terminal()
)
_active_tokens: TerminalTokens = (
    resolve_theme(env=os.environ, is_tty=True)
    if _ENV_THEME_CONTROLLED
    else _DEFAULT_TOKENS
)

BG_BASE = _active_tokens["bg_base"]
BG_PANEL = _active_tokens["bg_panel"]
BG_PANEL_HOVER = _active_tokens["bg_panel_hover"]
BORDER_SUBTLE = _active_tokens["border_subtle"]
ACCENT_PRIMARY = _active_tokens["accent_primary"]
ACCENT_TEXT = _active_tokens["accent_text"]
ACCENT_GLOW = _active_tokens["accent_glow"]
TEXT_PRIMARY = _active_tokens["text_primary"]
TEXT_SECONDARY = _active_tokens["text_secondary"]
SUCCESS = _active_tokens["success"]
ERROR = _active_tokens["error"]
WARNING = _active_tokens["warning"]

NEO_THEME = rich_theme(_DEFAULT_TOKENS)

_no_color: Optional[bool] = (
    os.environ.get("NO_COLOR") is not None or os.environ.get("NEO_NO_COLOR") == "1"
)


def active_tokens() -> TerminalTokens:
    """Return the process-wide resolved terminal token set."""
    return _active_tokens


def token_value(name: str, default: Any = None) -> Any:
    """Return one semantic token value from the active theme."""
    return _active_tokens.get(name, default)


def set_theme(
    name: Optional[str] = None,
    overrides: Any = None,
    *,
    config: Optional[dict] = None,
    depth: Optional[ColorDepth | str] = None,
) -> TerminalTokens:
    """Select a terminal theme and rebuild the shared Rich console."""
    resolved = resolve_theme(
        name=name,
        overrides=overrides,
        config=config,
        depth=depth,
        is_tty=True,
    )
    return set_active_tokens(resolved)


def set_active_tokens(tokens: TerminalTokens) -> TerminalTokens:
    """Install an already-resolved token set as the process theme."""
    global _active_tokens
    global BG_BASE, BG_PANEL, BG_PANEL_HOVER, BORDER_SUBTLE
    global ACCENT_PRIMARY, ACCENT_TEXT, ACCENT_GLOW
    global TEXT_PRIMARY, TEXT_SECONDARY, SUCCESS, ERROR, WARNING
    _active_tokens = tokens
    BG_BASE = _active_tokens["bg_base"]
    BG_PANEL = _active_tokens["bg_panel"]
    BG_PANEL_HOVER = _active_tokens["bg_panel_hover"]
    BORDER_SUBTLE = _active_tokens["border_subtle"]
    ACCENT_PRIMARY = _active_tokens["accent_primary"]
    ACCENT_TEXT = _active_tokens["accent_text"]
    ACCENT_GLOW = _active_tokens["accent_glow"]
    TEXT_PRIMARY = _active_tokens["text_primary"]
    TEXT_SECONDARY = _active_tokens["text_secondary"]
    SUCCESS = _active_tokens["success"]
    ERROR = _active_tokens["error"]
    WARNING = _active_tokens["warning"]
    console.cache_clear()
    return _active_tokens


def color_depth(*, is_tty: Optional[bool] = None) -> ColorDepth:
    """Return the resolved color depth for the active environment."""
    return resolve_color_depth(is_tty=is_tty)


def legacy_encoding(stream: Any = None) -> bool:
    """Return whether the selected stream uses a legacy encoding."""
    return is_legacy_encoding(stream)


def color_enabled(*, is_tty: Optional[bool] = None) -> bool:
    """Return whether the active environment permits terminal color."""
    return color_depth(is_tty=is_tty) is not ColorDepth.NONE


def available_themes() -> tuple[str, ...]:
    """Return the built-in terminal theme names."""
    return _theme_names()


def preview_theme(
    name: Optional[str] = None,
    overrides: Any = None,
    *,
    depth: Optional[ColorDepth | str] = None,
) -> str:
    """Render a theme preview without changing process state."""
    return _theme_preview(
        resolve_theme(name=name, overrides=overrides, depth=depth, is_tty=True)
    )


def theme_report(
    name: Optional[str] = None,
    overrides: Any = None,
    *,
    depth: Optional[ColorDepth | str] = None,
) -> dict:
    """Return a JSON-friendly report for the selected theme."""
    return _theme_summary(
        resolve_theme(name=name, overrides=overrides, depth=depth, is_tty=True)
    )


def textual_theme_variables(tokens: Optional[TerminalTokens] = None) -> dict[str, str]:
    """Return Textual CSS variables for the active or supplied tokens."""
    return _textual_variables(tokens or active_tokens())


def current_rich_theme() -> Any:
    """Return the Rich theme for the active terminal tokens."""
    return rich_theme(active_tokens())


def state_glyph(name: str) -> str:
    """Return a state's encoding-safe marker for the current stream.

    Hue is the first channel for a state and never the only one: this is
    what a renderer prints when it needs the state to be readable at 16
    colors, under NO_COLOR, or on a legacy console.
    """
    return state_marker(name)


def state_text(name: str) -> str:
    """Return a state's text alternative, the last channel that always works."""
    return state_label(name)


def state_channel_report(tokens: Optional[TerminalTokens] = None) -> dict:
    """Report which channels carry state, and every hue collision, for the
    active (or supplied) token set."""
    return channel_report(tokens or active_tokens())


def token_roles() -> dict:
    """Report which renderer role draws each semantic token."""
    return token_coverage()


def tokens_without_a_role() -> dict:
    """Return tokens no renderer draws, with the reason for each."""
    return unmapped_tokens()


def state_markers_table() -> dict:
    """Return every state's encoding-safe marker keyed by state name."""
    return state_markers()


def _enc_ok(ch: str) -> bool:
    """Return whether the active stream can render a character."""
    if is_dumb_terminal():
        return False
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
RULE_CHAR = _glyph("─", "-")


def rule_char() -> str:
    """Return the rule glyph safe for the current stream and terminal mode."""
    return _glyph("─", "-")


def rule(title: str = "", style: str = "") -> None:
    """Print an encoding-safe horizontal rule through the shared console."""
    console().rule(title, characters=rule_char(), style=style or None)


#: Spinner set safe for the current console's encoding. rich's default
#: spinners ("dots" etc.) are braille — UnicodeEncodeError on cp1252
#: consoles when ANSI is otherwise forced (found by probe, 2026-09-13;
#: same bug class as the glyph fallbacks above).
SPINNER = "dots" if _enc_ok("\u280b") else "line"


def _motion_disabled_by_env() -> bool:
    """Return whether an environment policy disables nonessential motion."""
    return any(
        str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}
        for name in ("NEO_REDUCED_MOTION", "REDUCED_MOTION", "NO_MOTION")
    )


def spinner_name() -> str:
    """Return a spinner safe for the current stream and motion policy."""
    if not motion_enabled():
        return "line"
    return "dots" if _enc_ok("\u280b") else "line"


#: Thinking-frame set (distinct ORBIT glyph — model-thinking phases
#: look different from the linear spinner at a glance; ASCII fallback).
THINK_FRAMES = "◐◓◑◒" if _enc_ok("◐") else "|/-\\"


def motion_enabled() -> bool:
    """Return whether decorative terminal motion is currently enabled."""
    return active_tokens().motion and not _motion_disabled_by_env()


def thinking_frames() -> str:
    """Return thinking frames safe for the current terminal mode."""
    if not motion_enabled():
        return "*"
    return "◐◓◑◒" if _enc_ok("◐") else "|/-\\"


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
    """Force-disable color for this process (CLI flag or NO_COLOR)."""
    global _no_color
    _no_color = bool(
        flag
        or os.environ.get("NO_COLOR") is not None
        or os.environ.get("NEO_NO_COLOR") == "1"
    )
    console.cache_clear()


def _console_color_system() -> Optional[str]:
    """Return Rich's color-system name for the active token depth."""
    return {
        ColorDepth.TRUECOLOR: "auto",
        ColorDepth.ANSI256: "256",
        ColorDepth.ANSI16: "windows",
        ColorDepth.NONE: None,
    }[active_tokens().depth]


@lru_cache(maxsize=1)
def console() -> Console:
    """Return the shared Neo console using the active semantic theme."""
    return Console(
        theme=rich_theme(active_tokens()),
        color_system=_console_color_system(),
        highlight=False,
        soft_wrap=False,
        no_color=_no_color
        or is_dumb_terminal()
        or active_tokens().depth is ColorDepth.NONE
        or None,
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
    """Return a themed console bound to stderr."""
    return Console(
        theme=rich_theme(active_tokens()),
        color_system=_console_color_system(),
        highlight=False,
        no_color=_no_color
        or is_dumb_terminal()
        or active_tokens().depth is ColorDepth.NONE
        or None,
        stderr=True,
    )


def supports_ansi() -> bool:
    """Return whether the shared console emits ANSI color sequences."""
    if _no_color or active_tokens().depth is ColorDepth.NONE or is_dumb_terminal():
        return False
    c = console()
    buf = io.StringIO()
    probe = Console(
        file=buf,
        theme=rich_theme(active_tokens()),
        color_system=_console_color_system(),
        force_terminal=c.is_terminal,
        no_color=c.no_color,
    )
    probe.print("[neo.accent]x[/]")
    return "\x1b[" in buf.getvalue()


# ---------------------------------------------------------------------------
# THE sanitiser — one entry point, every render path
# ---------------------------------------------------------------------------
#
# The order is the whole fix, and it is enforced by a source-level test
# (`cli/test_sanitize_pipeline.py::test_the_pipeline_strips_before_it_redacts`)
# so a future refactor cannot silently invert it:
#
#     coerce -> strip escapes -> drop invisible chars -> redact -> decode-guard
#                                                                       -> render
#
# WHY strip must come FIRST. ANSI escapes SPLIT a secret into visually
# contiguous bytes. `key=\x1b[35msk\x1b[0m-FAKE` is not a token the redactor
# can match, and the strip that runs afterwards REASSEMBLES a complete,
# visible credential out of it. Redacting the pre-strip bytes protects a
# string the user never sees.
#
# WHY invisible characters must go too. A zero-width space inside `sk-...`
# defeats the shape match and renders as nothing, so removing it is what makes
# "the bytes the redactor scanned" equal "the bytes the terminal draws".
#
# WHY it FAILS CLOSED. A redaction layer that degrades to the raw value on
# error is not a redaction layer. Any failure — a raising redactor, a redactor
# that returns None, a value that cannot be coerced — withholds the detail and
# says so. The withheld receipt records the FACT and never the value.

_ANSI_ESCAPE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]|\x9b[0-?]*[ -/]*[@-~])"
)
# Tab (0x09) and newline (0x0a) are real structure and are preserved.
# Backspace (0x08) is removed: it moves the cursor BACK, so the terminal
# REASSEMBLES the bytes around it — removing it is what makes the reassembled
# token visible to the redactor.
_CONTROL_WITHOUT_NEWLINES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Carriage return is different from backspace and is handled differently, for
# a reason the two comments above do not share: `\r` moves the cursor to
# column 0, so it HIDES what precedes it rather than joining it. Removing it
# outright would glue `key=REDACTED` onto `sk-...` and destroy the token
# boundary the redactor needs — the secret would then survive in full. A space
# preserves the boundary for the redactor and removes the overwrite for the
# terminal, which is the only substitution that is safe for both. A bare `\r`
# inside a line is always an overwrite control; `\r\n` is a line ending and is
# collapsed to `\n` first, so no line acquires a trailing space.
_CARRIAGE_RETURN = re.compile(r"\r\n?")
_BACKSPACE = re.compile("\b")

#: Zero-width, bidi-override and other format characters. They occupy no
#: column and reorder what surrounds them, so a redactor scanning them sees a
#: token the terminal does not draw. Enumerated by CODEPOINT on purpose: these
#: characters are invisible in source, so a literal one is impossible to audit
#: and impossible to review in a diff.
INVISIBLE_CODEPOINTS: tuple = (
    0x00AD,  # SOFT HYPHEN
    0x180E,  # MONGOLIAN VOWEL SEPARATOR
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x200E,  # LEFT-TO-RIGHT MARK
    0x200F,  # RIGHT-TO-LEFT MARK
    0x202A,  # LEFT-TO-RIGHT EMBEDDING
    0x202B,  # RIGHT-TO-LEFT EMBEDDING
    0x202C,  # POP DIRECTIONAL FORMATTING
    0x202D,  # LEFT-TO-RIGHT OVERRIDE
    0x202E,  # RIGHT-TO-LEFT OVERRIDE
    0x2060,  # WORD JOINER
    0x2061,  # FUNCTION APPLICATION
    0x2062,  # INVISIBLE TIMES
    0x2063,  # INVISIBLE SEPARATOR
    0x2064,  # INVISIBLE PLUS
    0x2066,  # LEFT-TO-RIGHT ISOLATE
    0x2067,  # RIGHT-TO-LEFT ISOLATE
    0x2068,  # FIRST STRONG ISOLATE
    0x2069,  # POP DIRECTIONAL ISOLATE
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
    0xFFF9,  # INTERLINEAR ANNOTATION ANCHOR
    0xFFFA,  # INTERLINEAR ANNOTATION SEPARATOR
    0xFFFB,  # INTERLINEAR ANNOTATION TERMINATOR
)
_INVISIBLE_CHARS = re.compile(
    "[%s]" % "".join(chr(code) for code in INVISIBLE_CODEPOINTS)
)

#: Cheap prefilter for the decode-aware guard. The guard only runs when the
#: value actually carries an encoding marker, so ordinary prose and ordinary
#: diffs pay a single regex search and nothing else.
_ENCODING_MARKER = re.compile(r"%|&#[0-9]{1,6};|[A-Za-z0-9+/]{16,}={0,2}")

#: A candidate encoded run: long enough that ordinary identifiers and
#: word-wrapped prose do not match.
_ENCODED_RUN = re.compile(r"[0-9A-Za-z+/%=._~-]{12,}")

_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{%d,}={0,2}" % 16)
_HEX_RUN = re.compile(r"(?:[0-9A-Fa-f]{2}){%d,}" % 8)

#: Bounds. A redactor that is quadratic on a hostile input has already taken
#: this repository down once (`"y" * 40000` wedged the journal); the guard is
#: on a render path, so every dimension of it is capped.
ENCODED_MAX_RUN = 4096
ENCODED_MAX_PROBES = 24
ENCODED_MAX_PROBE_BYTES = 4096
ENCODED_MAX_DECODE_ROUNDS = 2

#: The bounded, in-process observability ledger. Records the FACT of a
#: withholding, never the value: a receipt that carried the value would be the
#: leak it is reporting.
SANITIZE_WITHHOLDING_LIMIT = 64
_SANITIZE_WITHHOLDINGS: deque = deque(maxlen=SANITIZE_WITHHOLDING_LIMIT)
_WITHHOLD_COUNTER = itertools.count(1)


class RedactionUnavailable(RuntimeError):
    """Raised when the redactor could not be applied to a value.

    A redaction layer that cannot redact must WITHHOLD. Callers never let this
    escape: :func:`sanitize_text` converts it into a withheld receipt.
    """


#: The marker `shared.security` itself emits when IT fails closed
#: (`shared.security._withheld_text`). The authority is allowed to withhold —
#: it must, a redactor that cannot redact may not pass the value through — and
#: this layer has to be able to tell that outcome apart from a successful
#: redaction, for two reasons that are both measured rather than theoretical:
#:
#: 1. **The withholding must be observable.** Before this constant existed,
#:    an authority-side withholding produced a perfectly safe string that
#:    `sanitize_report()` reported as ``withheld: 0`` — the user saw
#:    "(detail withheld: redaction failed: …)" while the ledger said nothing
#:    had been withheld. A silently withheld detail is an invisible failure, and
#:    an invisible withholding is how a broken redactor becomes a permanent
#:    condition nobody notices.
#: 2. **The liveness probe must not read a failure as a success.** A probe
#:    that only checks "did the probe string survive" scores an authority
#:    that failed closed as FUNCTIONAL, because the failure marker does not
#:    contain the probe. That is the wrong answer in the safe direction once
#:    and the wrong answer in the reporting direction always.
AUTHORITY_WITHHELD_PREFIX = "(detail withheld:"

#: The reasons THIS layer withholds under. Declared, closed, and load-bearing:
#: the marker text `(detail withheld: <reason>)` is what distinguishes a
#: withholding made here from one the authority made, because both sides use
#: the same prefix. Without this set, `sanitize_text` misreads its OWN
#: `(detail withheld: encoded credential)` as the authority's marker on a second
#: pass and re-labels it `(detail withheld: redactor unavailable)` — measured,
#: and the symptom is a diff row whose withholding reason changes every time it
#: is sanitised again, which breaks idempotency and makes the ledger lie.
#:
#: A reason nobody declared cannot be recorded, which is the point: the ledger's
#: `by_reason` keys are a closed vocabulary a reader can count.
WITHHELD_REASONS: tuple = (
    "encoded credential",
    "not displayable",
    "redactor unavailable",
)


def _is_authority_withholding(text: str) -> bool:
    """Whether ``text`` is the AUTHORITY's withheld marker rather than ours.

    The two share a prefix, so the prefix alone cannot tell them apart and
    guessing would make a re-sanitised value change its own meaning. The
    discriminator is the reason vocabulary: a marker whose reason is one of
    :data:`WITHHELD_REASONS` was produced here, anything else came back from
    ``shared.security``.
    """
    if not text.startswith(AUTHORITY_WITHHELD_PREFIX):
        return False
    # The marker is `(detail withheld: <reason>)`, so the trailing paren is
    # part of the envelope rather than of the reason. Only OUR reasons are
    # compared, and none of them ends in a paren, so stripping it cannot make
    # an authority reason look like one of ours.
    reason = text[len(AUTHORITY_WITHHELD_PREFIX) :].strip().rstrip(")").strip()
    return bool(reason) and reason not in WITHHELD_REASONS


#: A LIVENESS PROBE for the redactor, not a second redactor. `cli` must not
#: re-implement `shared.security`'s patterns — two pattern sets are two
#: answers to "is this a secret", and the second one would rot. What `cli` CAN
#: do is ask the authority whether it is still doing its job: a redactor that
#: has become a pass-through (the shape this exact wrapper used to degrade to)
#: leaves a known-shaped input untouched, and that is observable in O(1).
REDACTOR_PROBE = "sk-VEXPROBE00000000000000000000"
REDACTOR_PROBE_INTERVAL = 1
_redactor_ok: Optional[bool] = None
_probe_counter = itertools.count(1)


def redactor_is_functional() -> bool:
    """Whether `shared.security.redact_text` is still redacting.

    The probe runs at most once per :data:`REDACTOR_PROBE_INTERVAL` calls and
    costs the same whether the value is one line or nine kilobytes, which is
    why it is a probe and not a second scan of the caller's own text.

    Measured on this host: 0.03 ms per probe against 9.4-12.2 ms for a
    200-line diff through `redact_text` itself, so the guarantee this buys is
    not the guarantee that costs the render.

    An authority that returned its own withheld marker has NOT redacted, and
    this answers ``False`` — see :data:`AUTHORITY_WITHHELD_PREFIX`.

    **A declared limit.** This probe cannot detect a redactor that redacts
    correctly and then has its own output UNDONE: the probe is redacted, the
    restoration puts the probe string back, and the answer reads functional
    while the caller's value is disclosed. The shape is pinned as a named
    boundary test in ``cli/test_display_contract.py`` rather than left
    implicit, because the alternative — re-scanning every rendered value with
    a SECOND pattern set inside ``cli`` — was measured at +122 % on a 9 KB
    diff and is the wrong shape anyway: two pattern sets are two answers to
    "is this a secret" and the second one rots silently.
    """
    global _redactor_ok
    if next(_probe_counter) % REDACTOR_PROBE_INTERVAL:
        return bool(_redactor_ok)
    try:
        result = redact_text(REDACTOR_PROBE)
    except Exception:
        _redactor_ok = False
        return False
    if _is_authority_withholding(str(result or "")):
        _redactor_ok = False
        return False
    _redactor_ok = bool(
        isinstance(result, str) and result and REDACTOR_PROBE not in result
    )
    return _redactor_ok


def sanitize_report() -> dict:
    """Report withheld sanitisations — counts and reasons, never values.

    The always-available observability channel. A withholding is otherwise
    invisible, and an invisible withholding is how a broken redactor becomes a
    permanent condition nobody notices.
    """
    rows = list(_SANITIZE_WITHHOLDINGS)
    by_reason: dict = {}
    for row in rows:
        by_reason[str(row.get("reason") or "unknown")] = (
            by_reason.get(str(row.get("reason") or "unknown"), 0) + 1
        )
    return {
        "withheld": len(rows),
        "window": SANITIZE_WITHHOLDING_LIMIT,
        "by_reason": by_reason,
        "events": list(rows),
    }


def _note_withholding(reason: str, length: int, task_id: str = "") -> None:
    """Record that a detail was withheld. Never records the detail itself."""
    reason = str(reason or "unknown")
    row = {
        "seq": next(_WITHHOLD_COUNTER),
        "reason": reason,
        "chars_withheld": int(length),
        "task_id": str(task_id or ""),
    }
    _SANITIZE_WITHHOLDINGS.append(row)
    if task_id:
        try:
            from shared.tracing import emit

            emit("cli.ui", "display_detail_withheld", task_id=str(task_id), **row)
        except Exception:
            pass


def _withheld(reason: str, *, task_id: str = "", length: int = 0) -> str:
    """The withheld marker: states the class of the failure, carries no value."""
    _note_withholding(reason, length, task_id)
    return f"(detail withheld: {reason})"


def coerce_text(value: Any) -> str:
    """Return ``value`` as text, or raise rather than guess.

    Coercion is part of the sanitiser because an un-coercible value must fail
    closed like any other step: a fallback that produced ``""`` for a
    measurement nobody took would be a lie, and one that produced the raw
    object would be a disclosure.
    """
    if isinstance(value, str):
        return value
    try:
        return str(value or "")
    except Exception as exc:  # pragma: no cover - hostile __str__
        raise RedactionUnavailable(f"value is not displayable: {exc!r}") from exc


def strip_escapes(text: Any) -> str:
    """Return ``text`` with terminal escapes and invisible characters removed.

    NO redaction. This is for the one job that genuinely needs the raw
    visible bytes — building the text a secret will be searched FOR, so the
    comparison is between what the redactor saw and what the terminal shows.
    Calling :func:`sanitize_text` there redacts the very credential being
    looked for and the check passes vacuously (VEX-TERM-UX-09 finding 6).

    Never raises.
    """
    try:
        out = coerce_text(text)
    except Exception:
        return ""
    try:
        out = _ANSI_ESCAPE.sub("", out)
        out = _CARRIAGE_RETURN.sub(lambda m: "\n" if m.group(0) == "\r\n" else " ", out)
        out = _BACKSPACE.sub("", out)
        out = _CONTROL_WITHOUT_NEWLINES.sub("", out)
        return _INVISIBLE_CHARS.sub("", out)
    except Exception:
        return ""


def _coerce_or_withhold(value: Any, *, task_id: str = "") -> Optional[str]:
    """Return display text for ``value``, or None when it cannot be coerced."""
    try:
        return coerce_text(value)
    except RedactionUnavailable:
        return _withheld("not displayable", task_id=task_id)


def redact_or_fail(text: Any) -> str:
    """Redact ``text`` or raise :class:`RedactionUnavailable`. Never falls back.

    A permissive signature is not evidence of support: a redactor that returns
    ``None`` has not redacted anything, and one that raises has not either. Both
    are refusals, and a refusal withholds.
    """
    try:
        out = redact_text(text)
    except Exception as exc:
        raise RedactionUnavailable(f"redactor raised: {exc!r}") from exc
    if out is None or not isinstance(out, str):
        raise RedactionUnavailable(f"redactor returned {type(out).__name__}, not text")
    return out


def _decoded_candidates(run: str) -> Iterator[str]:
    """Yield the plausible decodings of one encoded run. Bounded and total.

    Percent-decoding is ITERATIVE: `sk%252DFAKE` unquotes once to
    `sk%2DFAKE`, which still contains no secret shape, so a single pass would
    wave through a doubly-encoded credential. Two rounds is enough to cover
    the double-encoded form; the cap is a bound, not a claim that three is
    impossible.
    """
    if not run or len(run) > ENCODED_MAX_RUN:
        return
    try:
        from urllib.parse import unquote, unquote_plus

        frontier = {run}
        for _ in range(ENCODED_MAX_DECODE_ROUNDS):
            nxt: set = set()
            for current in frontier:
                for fn in (unquote, unquote_plus):
                    decoded = fn(current)
                    if decoded and decoded not in frontier:
                        nxt.add(decoded)
            if not nxt:
                break
            for decoded in nxt:
                yield decoded[:ENCODED_MAX_PROBE_BYTES]
            frontier = nxt
    except Exception:
        pass
    if _BASE64_RUN.fullmatch(run) and len(run) % 4 == 0:
        try:
            import base64

            decoded = base64.b64decode(run, validate=True)
            if decoded:
                yield decoded[:ENCODED_MAX_PROBE_BYTES].decode("utf-8", errors="ignore")
        except Exception:
            pass
    if _HEX_RUN.fullmatch(run) and len(run) % 2 == 0:
        try:
            decoded = bytes.fromhex(run).decode("utf-8", errors="ignore")
            if decoded:
                yield decoded[:ENCODED_MAX_PROBE_BYTES]
        except Exception:
            pass


def _encoded_secret_reason(text: str) -> Optional[str]:
    """Return why ``text`` DECODES into credential material, or None.

    Percent-encoding, base64 and hex are transports, not secrecy: the rendered
    string shows ``sk%2DFAKE...`` or a base64 blob, and whatever consumes it
    downstream recovers a live key. Redaction cannot match a shape the text
    does not contain, so the shape has to be recovered first. The run is then
    withheld rather than rewritten, because a display sanitiser must not
    silently mutate the bytes it was asked to show.
    """
    if not text or not _ENCODING_MARKER.search(text):
        return None
    probes = 0
    for match in _ENCODED_RUN.finditer(text):
        if probes >= ENCODED_MAX_PROBES:
            break
        probes += 1
        run = match.group(0)
        for decoded in _decoded_candidates(run):
            try:
                from shared.security import contains_secret

                if contains_secret(decoded):
                    return "encoded credential"
            except Exception:
                continue
    return None


def sanitize_text(value: Any = "", *, task_id: str = "") -> str:
    """Return display-safe text: escapes stripped, secrets redacted.

    THE single entry point every render path in ``cli/`` goes through. Never
    raises and never returns the un-redacted value: on any failure it returns
    ``(detail withheld: <reason>)`` and records the fact of the withholding in
    :func:`sanitize_report`.
    """
    text = _coerce_or_withhold(value, task_id=task_id)
    if text is None:
        return ""

    # 1. escapes and overwrite controls — BEFORE any inspection of the bytes
    text = strip_escapes(text)

    # 2. redaction, on the bytes the terminal will actually draw
    redacted: Optional[str] = None
    try:
        redacted = redact_or_fail(text)
    except RedactionUnavailable:
        redacted = None
    # The AUTHORITY may withhold on its own terms, and it must: a redactor that
    # cannot redact may not pass the value through. It used to arrive here as a
    # safe string that this layer passed straight to the terminal, which is
    # correct for the VALUE and wrong for the RECORD — `sanitize_report()`
    # reported `withheld: 0` while the screen showed a withholding. It is now
    # recorded here under this layer's own reason vocabulary, and the
    # authority's own detail text is deliberately NOT propagated: that detail
    # is DATA produced by the failing component, and this layer does not
    # re-publish what it just refused to trust.
    if redacted is not None and _is_authority_withholding(redacted):
        return _withheld("redactor unavailable", task_id=task_id, length=len(text))
    if redacted is None or not redactor_is_functional():
        return _withheld("redactor unavailable", task_id=task_id, length=len(text))

    # 3. decode-aware guard, per line so one encoded row cannot blank a diff.
    #    Cost, measured on this host over a 200-line / 9 KB diff:
    #    `_ENCODING_MARKER.search` 0.71 ms, the whole per-line pass 0.83 ms,
    #    against 9.4-12.2 ms already spent inside `redact_text` for the same
    #    input — i.e. the guard is ~6% of a render that redaction already
    #    dominates. Not worth a cache; recorded so a future round can decide
    #    with a number rather than a guess.
    if _ENCODING_MARKER.search(redacted):
        out: list = []
        for line in redacted.split("\n"):
            reason = _encoded_secret_reason(line)
            out.append(
                _withheld(reason, task_id=task_id, length=len(line)) if reason else line
            )
        return "\n".join(out)
    return redacted


def strip_ansi(value: Any) -> str:
    """Return display-safe text without secrets, terminal controls, or unsafe controls.

    Historical name for :func:`sanitize_text`; every pre-existing call site
    resolves to that one implementation.
    """
    return sanitize_text(value)


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
    kw.setdefault("spinner", spinner_name())
    return console().status(f"[neo.running]{text}[/]", *args, **kw)


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
                con.print(line, style="neo.diff.meta")
            elif line.startswith("@@"):
                con.print(line, style="neo.diff.hunk")
            elif line.startswith("+"):
                con.print(line, style="neo.diff.add")
            elif line.startswith("-"):
                con.print(line, style="neo.diff.del")
            else:
                con.print(line)


# ---------------------------------------------------------------------------
# Task B (interaction-polish round) — syntax-highlighted diffs
# ---------------------------------------------------------------------------

#: Diff-line roles: the character at the line start decides. The styles
#: are CONCRETE token hexes (not `neo.diff.*` theme-role names) because
#: these Text objects go straight into textual's RichLog, which has no
#: access to the rich NEO_THEME — the roles live in `print_diff`'s
#: degraded path (which prints via the themed shared console).
#: Backgrounds are the dark ends of the success/error tokens blended
#: toward bg-base — a readable add/del band under the pygments colors.
_DIFF_META_STYLE = ACCENT_GLOW
_DIFF_HUNK_STYLE = TEXT_SECONDARY
_DIFF_OTHER_STYLE = TEXT_SECONDARY
_DIFF_ADD_BG = _DEFAULT_TOKENS["diff_add_bg"]
_DIFF_DEL_BG = _DEFAULT_TOKENS["diff_delete_bg"]
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

        tokens = active_tokens()
        if line.startswith("+++") or line.startswith("---"):
            # File headers also SELECT the language for the hunk that
            # follows (the +++ side is the new content — what's on +/ctx
            # lines, and the closer to what - lines were before the
            # rename, so it is the better lexer cue).
            if line.startswith("+++"):
                self._current = self._lexer_for(_diff_filename(line[6:]))
            return Text(line, style=tokens["diff_meta"])
        if line.startswith("@@"):
            return Text(line, style=tokens["diff_hunk"])
        if line.startswith("+"):
            return self._content(
                "+", line[1:], tokens["diff_add"], tokens["diff_add_bg"]
            )
        if line.startswith("-"):
            return self._content(
                "-", line[1:], tokens["diff_delete"], tokens["diff_delete_bg"]
            )
        if line.startswith(" "):
            return self._content(" ", line[1:], None, None)
        # binary/truncation markers, "\ No newline", git index noise:
        # secondary info, never a random success/error color.
        return Text(line, style=tokens["text_secondary"])

    def _content(self, marker: str, code: str, fg, bg):
        """A content line with optional fg/bg for the diff role; the
        code portion gets pygments token colors over that base."""
        from rich.style import Style
        from rich.text import Text

        tokens = active_tokens()
        base = Style.parse(
            " ".join(s for s in (f"{fg}" if fg else "", f"on {bg}" if bg else "") if s)
            or "none"
        )
        out = Text()
        out.append(marker, style=base)
        lexer = self._current if tokens.color_enabled else None
        if lexer is not None and code.strip():
            try:
                from rich.syntax import Syntax

                syn = Syntax(
                    code,
                    lexer=lexer,
                    theme="ansi_dark",
                    background_color=bg or tokens["bg_base"],
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
    the REPL's /diff, `neo fix`'s final diff.

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
        out.append(hl.line(strip_ansi(line)))
    return out


# ---------------------------------------------------------------------------
# Task F (interaction-polish round) — completion notification
# ---------------------------------------------------------------------------


def bell(message: str = "") -> bool:
    """Signal that a task finished — the terminal bell (`\\a`) plus, on
    Windows, a non-blocking `MessageBeep` (stdlib ctypes; covers the
    terminals where the in-band bell is swallowed). Fire-and-forget by
    design: a UI nicety must never raise into a finished run's
    reporting. TTY-ONLY (a \\a byte in a PIPE is literal garbage — a
    piped `neo fix` must stay byte-clean). Set `NEO_NOTIFY=0` to
    silence (tests / CI / ssh).

    VEX-CEILING-10: the return value is whether a bell was actually
    emitted, so `cli.notify` can write an honest receipt instead of
    claiming a notification that was suppressed. The signature is
    unchanged — a failure is escalated by `cli.notify` calling this more
    than once, so a patch of the historical one-argument shape keeps
    working.
    """
    import os
    import sys

    if os.environ.get("NEO_NOTIFY", "").strip().lower() in ("0", "false", "no"):
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        return False
    if os.name == "nt":  # 0x40 = MB_ICONASTERISK
        try:
            import ctypes

            ctypes.windll.user32.MessageBeep(0x40)
        except (Exception, KeyboardInterrupt):
            pass
    return True


# ---------------------------------------------------------------------------
# Branding — gradient wordmark, splash, compact header (Tasks A/B/C)
# ---------------------------------------------------------------------------

#: ANSI-shadow wordmark (box-drawing double-line letterforms — the
#: figlet style OpenCode/Claude Code-class CLIs use). '╔' & co. crash
#: legacy cp1252 consoles, so wordmark_lines() probe-picks this vs the
#: '#' block fallback below. Rows are ljust'd to a uniform width so
#: the gradient spans identical spans per row.
#:
#: Geometry is unchanged by the VEX->NEO rename: N occupies the 9-column
#: cell the leading letter always has, E and O the 8-column cells, for the
#: same 25-character content ljust'd to a 26-column span — so the
#: per-column gradient ramp and every width budget downstream are
#: byte-for-byte comparable with the previous mark.
_WORDMARK_ANSI = [
    r"██╗   ██╗███████╗███████╗".ljust(26),
    r"██║  ███╗██╔════╝██╔════╝".ljust(26),
    r"██║ ███║ █████╗  ██║     ".ljust(26),
    r"██║███║  ██╔══╝  ██║     ".ljust(26),
    r"██║███║  ██║     ██║     ".ljust(26),
    r"██╚╝██║  ███████╗███████╗".ljust(26),
]
#: '#' block fallback (plain ASCII, 7/6/7 letter cells): for consoles
#: whose encoding can render neither box-drawing nor shade glyphs (exotic
#: 7-bit consoles).
_WORDMARK_BLOCK = [
    "#     #   ######   #######",
    "##    #   #        #     #",
    "# #   #   #        #     #",
    "#  #  #   #####    #     #",
    "#   # #   #        #     #",
    "#    ##   ######   #######",
]

#: The brand gradient: deep crimson -> accent -> rose -> white-hot.
#: Dark-to-light across the wordmark is the "modern CLI" look; each
#: row interpolates horizontally (per-column color stops). The TEXT
#: weight (#E8114A, stop 2) is the crimson one step brighter than
#: #DC143C in the same hue: #DC143C measures ~4.2:1 on #000000
#: (below the 4.5:1 bar), #E8114A measures ~4.6:1 — brightness
#: adjusted, hue kept. Per-column blends are the only sanctioned
#: non-token colors (pinks on this ramp, never orange).
_NEO_RAMP = ("#7A0C26", "#E8114A", "#FF7A93", "#FFF0F3")

TAGLINE = "the AI harness that fixes bugs — verified, not vibed"

#: Dot separator (cp1252-safe, unlike the box-drawing glyphs).
DOT = "·"


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _interp(c1, c2, t: float):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2, strict=False))


def gradient_text(s: str, ramp=_NEO_RAMP):
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
    """The NEO wordmark rows, encoding-safe for this console."""
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
            con.print(f"[neo.accent]{row}[/]", highlight=False)
    con.print()
    con.print(
        Text("  the AI harness that fixes bugs ", style=TEXT_SECONDARY)
        + Text("·", style=ACCENT_TEXT)
        + Text(" verified, not vibed", style=TEXT_PRIMARY)
    )
    con.print()
    rule("", BORDER_SUBTLE)
    con.print()
    # aligned info rows: dim label column, clean value column
    con.print(f"  [neo.muted]repo [/] [bold {TEXT_PRIMARY}]{Path(repo)}[/]")
    con.print(f"  [neo.muted]logs [/] [{TEXT_PRIMARY}]{Path(log_root)}[/]")
    con.print(f"  [neo.muted]model[/] [neo.accent2]{model}[/]")
    if version:
        con.print(f"  [neo.muted]neo  [/] [{TEXT_PRIMARY}]{version}[/]")
    con.print()
    con.print(
        f"  [neo.muted]describe what's wrong in plain language[/] "
        f"[{TEXT_SECONDARY}]{DOT}[/] [neo.accent]/help[/] "
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
    bits = f"[neo.accent]{ember} neo[/]"
    if version:
        bits += f" [neo.muted]{version}[/]"
    bits += f"  [neo.muted]{DOT}[/] [neo.muted]model[/] [neo.accent2]{model}[/]"
    bits += f"  [neo.muted]{DOT}[/] [neo.muted]{Path(repo)}[/]"
    if status_text:
        bits += f"  [neo.muted]{DOT}[/] [neo.running]{status_text}[/]"
    con.print(bits)
