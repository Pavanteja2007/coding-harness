"""Semantic color and motion tokens for Neo terminal surfaces.

The terminal has one token vocabulary shared by Rich, Textual, and preview
output.  A resolved token set carries its source theme, color depth, motion
policy, and legacy-encoding flag so renderers can degrade without inventing
new colors.  This module is CLI-internal and has no harness or runtime
dependency.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Mapping, Optional


class ColorDepth(str, Enum):
    """Terminal color capabilities understood by Neo."""

    TRUECOLOR = "truecolor"
    ANSI256 = "256"
    ANSI16 = "16"
    NONE = "none"


TOKEN_NAMES = (
    "bg_base",
    "bg_panel",
    "bg_panel_hover",
    "border_subtle",
    "border_strong",
    "accent_primary",
    "accent_text",
    "accent_glow",
    "text_primary",
    "text_secondary",
    "text_disabled",
    "success",
    "warning",
    "error",
    "info",
    "pending",
    "selection_bg",
    "selection_fg",
    "focus",
    "hover",
    "pressed",
    "streaming",
    "approval",
    "diff_add",
    "diff_delete",
    "diff_hunk",
    "diff_meta",
    "diff_add_bg",
    "diff_delete_bg",
)

TOKEN_LABELS = {
    "bg_base": "background",
    "bg_panel": "panel",
    "bg_panel_hover": "panel hover",
    "border_subtle": "border",
    "border_strong": "strong border",
    "accent_primary": "accent fill",
    "accent_text": "accent text",
    "accent_glow": "highlight",
    "text_primary": "primary text",
    "text_secondary": "secondary text",
    "text_disabled": "disabled text",
    "success": "success",
    "warning": "warning",
    "error": "error",
    "info": "info",
    "pending": "pending",
    "selection_bg": "selection fill",
    "selection_fg": "selection text",
    "focus": "focus",
    "hover": "hover",
    "pressed": "pressed",
    "streaming": "streaming",
    "approval": "approval",
    "diff_add": "diff add",
    "diff_delete": "diff delete",
    "diff_hunk": "diff hunk",
    "diff_meta": "diff metadata",
    "diff_add_bg": "diff add surface",
    "diff_delete_bg": "diff delete surface",
}

_ALIASES = {
    "background": "bg_base",
    "panel": "bg_panel",
    "panel_hover": "bg_panel_hover",
    "hover_surface": "bg_panel_hover",
    "border": "border_subtle",
    "strong_border": "border_strong",
    "accent": "accent_text",
    "crimson": "accent_text",
    "active": "accent_text",
    "glow": "accent_glow",
    "rose": "accent_glow",
    "text": "text_primary",
    "primary_text": "text_primary",
    "secondary_text": "text_secondary",
    "muted": "text_secondary",
    "disabled": "text_disabled",
    "ok": "success",
    "warn": "warning",
    "fail": "error",
    "information": "info",
    "selection": "selection_bg",
    "selected": "selection_bg",
    "diff.add": "diff_add",
    "diff.delete": "diff_delete",
    "diff.hunk": "diff_hunk",
    "diff.meta": "diff_meta",
}

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_THEME_ALIASES = {
    "default": "default",
    "neo": "default",
    "normal": "default",
    "high-contrast": "high-contrast",
    "high_contrast": "high-contrast",
    "contrast": "high-contrast",
    "reduced-motion": "reduced-motion",
    "reduced_motion": "reduced-motion",
    "motion": "reduced-motion",
    "calm": "reduced-motion",
}


def _canonical_name(name: Any) -> str:
    """Normalize a public token spelling to its canonical name."""
    value = str(name or "").strip().lower().replace(" ", "_")
    value = value.replace("-", "_")
    value = _ALIASES.get(value, value)
    return value


def _valid_color(value: Any) -> bool:
    """Return whether a value is a safe six-digit terminal color."""
    return isinstance(value, str) and bool(_HEX_RE.fullmatch(value.strip()))


def _with_alpha(value: str, alpha: str) -> str:
    """Append an alpha suffix to a six-digit color when possible."""
    text = str(value or "").strip()
    return f"{text}{alpha}" if _HEX_RE.fullmatch(text) else text


def terminal_encoding(stream: Any = None) -> str:
    """Return the active stream encoding, defaulting to UTF-8."""
    target = stream if stream is not None else sys.stdout
    return str(getattr(target, "encoding", None) or "utf-8").lower()


def is_legacy_encoding(stream: Any = None) -> bool:
    """Return whether a stream uses a legacy non-Unicode encoding."""
    encoding = terminal_encoding(stream)
    return encoding in {"ascii", "cp1252", "cp1250", "cp1251", "latin-1", "iso-8859-1"}


def is_dumb_terminal(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return whether the terminal explicitly requests plain output."""
    values = os.environ if env is None else env
    return str(values.get("TERM", "")).strip().lower() == "dumb"


def _forced_depth(env: Mapping[str, str]) -> Optional[ColorDepth]:
    """Read an explicit color-depth override from an environment mapping."""
    raw = str(env.get("NEO_COLOR_DEPTH", "")).strip().lower()
    if raw:
        aliases = {
            "truecolor": ColorDepth.TRUECOLOR,
            "24bit": ColorDepth.TRUECOLOR,
            "24-bit": ColorDepth.TRUECOLOR,
            "direct": ColorDepth.TRUECOLOR,
            "256": ColorDepth.ANSI256,
            "256color": ColorDepth.ANSI256,
            "8bit": ColorDepth.ANSI256,
            "16": ColorDepth.ANSI16,
            "ansi": ColorDepth.ANSI16,
            "none": ColorDepth.NONE,
            "off": ColorDepth.NONE,
        }
        if raw in aliases:
            return aliases[raw]
    force = str(env.get("FORCE_COLOR", "")).strip().lower()
    if force == "0":
        return ColorDepth.NONE
    if force == "1":
        return ColorDepth.ANSI16
    if force == "2":
        return ColorDepth.ANSI256
    if force == "3":
        return ColorDepth.TRUECOLOR
    return None


def resolve_color_depth(
    env: Optional[Mapping[str, str]] = None,
    *,
    stream: Any = None,
    is_tty: Optional[bool] = None,
) -> ColorDepth:
    """Resolve truecolor, 256-color, 16-color, or plain output capability.

    Environment variables are a CEILING, not an answer.  A resolved depth is
    only believable if the stream that will carry the bytes can render it, so
    when the caller asserts neither ``stream`` nor ``is_tty`` this probes
    ``sys.stdout`` for real.  Passing ``is_tty`` explicitly keeps the
    historical env-only answer, which is what the interactive surfaces want
    (a TUI knows it is on a terminal even when its own stdout is captured).
    """
    values_env = os.environ if env is None else env
    if (
        "NO_COLOR" in values_env
        or str(values_env.get("NEO_NO_COLOR", "")).strip() == "1"
    ):
        return ColorDepth.NONE
    if is_dumb_terminal(values_env):
        return ColorDepth.NONE
    forced = _forced_depth(values_env)
    if forced is not None:
        return forced
    if is_tty is False:
        return ColorDepth.NONE
    # The stream is the GATE and the environment is the CEILING, so the probe
    # runs before hue detection.  It used to run after it, which meant a pipe
    # carrying TERM=xterm-256color never reached the probe at all: the depth
    # was read off the environment, `color_enabled` was True, and the console
    # emitted no escape.  A claim the stream has already refused is not a
    # capability.
    if stream is None and is_tty is None:
        stream = sys.stdout
    if stream is not None:
        try:
            if not bool(stream.isatty()):
                return ColorDepth.NONE
        except Exception:
            return ColorDepth.NONE
    colorterm = str(values_env.get("COLORTERM", "")).strip().lower()
    if colorterm in {"truecolor", "24bit", "24-bit"}:
        return ColorDepth.TRUECOLOR
    term = str(values_env.get("TERM", "")).strip().lower()
    term_program = str(values_env.get("TERM_PROGRAM", "")).strip().lower()
    if any(marker in term for marker in ("truecolor", "direct", "24bit")):
        return ColorDepth.TRUECOLOR
    if "256" in term or any(
        marker in term_program for marker in ("iterm", "wezterm", "vscode")
    ):
        return ColorDepth.ANSI256
    if os.name == "nt" and not term and is_tty is True:
        return ColorDepth.TRUECOLOR
    if term in {"xterm", "screen", "vt100", "vt220", "ansi", "cygwin", "linux"}:
        return ColorDepth.ANSI16
    return ColorDepth.ANSI16


def stream_is_tty(stream: Any = None) -> Optional[bool]:
    """Return whether a stream is an attached terminal, or None if unknowable.

    ``None`` is a real answer, not a failure: a stream with no usable
    ``isatty`` (a test double, an exotic file object) must not be reported
    as "definitely not a terminal", or a caller would disable color on a
    stream that can render it.
    """
    target = sys.stdout if stream is None else stream
    probe = getattr(target, "isatty", None)
    if not callable(probe):
        return None
    try:
        return bool(probe())
    except Exception:
        return None
    return ColorDepth.ANSI16


@dataclass(frozen=True)
class TerminalTokens(Mapping[str, str]):
    """A resolved semantic token set for one terminal capability profile."""

    theme: str
    depth: ColorDepth
    colors: Mapping[str, str]
    motion: bool = True
    legacy_encoding: bool = False
    source: str = "builtin"

    def __getitem__(self, key: str) -> str:
        """Return a token value by canonical or documented alias."""
        name = _canonical_name(key)
        try:
            return self.colors[name]
        except KeyError as exc:
            raise KeyError(f"unknown terminal token: {key}") from exc

    def __iter__(self) -> Iterator[str]:
        """Iterate over canonical token names."""
        return iter(self.colors)

    def __len__(self) -> int:
        """Return the number of canonical tokens."""
        return len(self.colors)

    @property
    def color_enabled(self) -> bool:
        """Return whether resolved values may be emitted as color."""
        return self.depth is not ColorDepth.NONE

    def get(self, key: str, default: Any = None) -> Any:
        """Return a token value or a caller-provided default."""
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-friendly copy of canonical token values."""
        return dict(self.colors)

    def style(
        self,
        token: str,
        *,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        background: str | bool = False,
    ) -> str:
        """Build a Rich/Textual style string without losing text attributes."""
        attributes = []
        if bold:
            attributes.append("bold")
        if italic:
            attributes.append("italic")
        if underline:
            attributes.append("underline")
        if self.depth is ColorDepth.NONE:
            return " ".join(attributes) or "none"
        name = _canonical_name(token)
        if background is True:
            background_name = name if name.startswith("bg_") else "bg_panel"
        else:
            background_name = _canonical_name(background) if background else ""
        style_value = self[name]
        if background_name:
            style_value = f"on {self[background_name]}"
        parts = [*attributes, style_value]
        return " ".join(parts)


@dataclass(frozen=True)
class ThemeDefinition:
    """An immutable built-in theme definition before capability fallback."""

    name: str
    label: str
    description: str
    colors: Mapping[str, str]
    motion: bool = True


_DEFAULT_COLORS = {
    "bg_base": "#000000",
    "bg_panel": "#0A0A0A",
    "bg_panel_hover": "#161616",
    "border_subtle": "#2A2A2A",
    "border_strong": "#8A8A8A",
    "accent_primary": "#4A0A14",
    "accent_text": "#E8114A",
    "accent_glow": "#FF7A93",
    "text_primary": "#F5F5F5",
    "text_secondary": "#8A8A8A",
    "text_disabled": "#5A5A5A",
    "success": "#34D399",
    "warning": "#FBBF24",
    "error": "#F87171",
    "info": "#7DD3FC",
    "pending": "#C4C4C4",
    "selection_bg": "#4A0A14",
    "selection_fg": "#F5F5F5",
    "focus": "#FF7A93",
    "hover": "#D4D4D4",
    "pressed": "#6B1020",
    "streaming": "#FF7A93",
    "approval": "#FBBF24",
    "diff_add": "#34D399",
    "diff_delete": "#F87171",
    "diff_hunk": "#8A8A8A",
    "diff_meta": "#FF7A93",
    "diff_add_bg": "#0A1510",
    "diff_delete_bg": "#1C0A0F",
}

_HIGH_CONTRAST_COLORS = {
    **_DEFAULT_COLORS,
    "bg_panel": "#050505",
    "bg_panel_hover": "#202020",
    "border_subtle": "#FFFFFF",
    "border_strong": "#FFFFFF",
    "accent_primary": "#7A1024",
    "accent_text": "#FF315C",
    "accent_glow": "#FFFFFF",
    "text_primary": "#FFFFFF",
    "text_secondary": "#E0E0E0",
    "text_disabled": "#A0A0A0",
    "success": "#5CFFC0",
    "warning": "#FFE066",
    "error": "#FF6B6B",
    "info": "#8BE9FD",
    "pending": "#FFFFFF",
    "selection_bg": "#7A1024",
    "focus": "#FFFFFF",
    "hover": "#FFFFFF",
    "pressed": "#A31530",
    "streaming": "#FFFFFF",
    "approval": "#FFE066",
    "diff_hunk": "#E0E0E0",
    "diff_add_bg": "#062B20",
    "diff_delete_bg": "#3A0A12",
}

_REDUCED_MOTION_COLORS = dict(_DEFAULT_COLORS)

_FALLBACK_256 = {
    **_DEFAULT_COLORS,
    "accent_primary": "#870025",
    "accent_text": "#FF005F",
    "accent_glow": "#FF87AF",
    "text_primary": "#FFFFFF",
    "text_secondary": "#BFBFBF",
    "text_disabled": "#808080",
    "border_strong": "#BFBFBF",
    "info": "#5FD7FF",
    "pending": "#E0E0E0",
    "selection_bg": "#870025",
    "focus": "#FF87AF",
    "hover": "#FFFFFF",
    "pressed": "#AF0037",
    "streaming": "#FF87AF",
}

_FALLBACK_16 = {
    "bg_base": "#000000",
    "bg_panel": "#000000",
    "bg_panel_hover": "#1C1C1C",
    "border_subtle": "#808080",
    "border_strong": "#FFFFFF",
    "accent_primary": "#800000",
    "accent_text": "#FF0000",
    "accent_glow": "#FFFFFF",
    "text_primary": "#FFFFFF",
    "text_secondary": "#C0C0C0",
    "text_disabled": "#808080",
    "success": "#00FF00",
    "warning": "#FFFF00",
    "error": "#FF0000",
    "info": "#00FFFF",
    "pending": "#FFFFFF",
    "selection_bg": "#800000",
    "selection_fg": "#FFFFFF",
    "focus": "#FFFFFF",
    "hover": "#FFFFFF",
    "pressed": "#800000",
    "streaming": "#FFFFFF",
    "approval": "#FFFF00",
    "diff_add": "#00FF00",
    "diff_delete": "#FF0000",
    "diff_hunk": "#C0C0C0",
    "diff_meta": "#FFFFFF",
    "diff_add_bg": "#000000",
    "diff_delete_bg": "#000000",
}

_BUILTIN_THEMES = {
    "default": ThemeDefinition(
        "default",
        "Neo",
        "Pitch-black surfaces with restrained crimson activity states.",
        _DEFAULT_COLORS,
    ),
    "high-contrast": ThemeDefinition(
        "high-contrast",
        "Neo high contrast",
        "Brighter neutral text and borders for low-vision and bright environments.",
        _HIGH_CONTRAST_COLORS,
    ),
    "reduced-motion": ThemeDefinition(
        "reduced-motion",
        "Neo reduced motion",
        "The default palette with nonessential animation disabled.",
        _REDUCED_MOTION_COLORS,
        motion=False,
    ),
}


def theme_names() -> tuple[str, ...]:
    """Return the names of all built-in terminal themes."""
    return tuple(_BUILTIN_THEMES)


# ---------------------------------------------------------------------------
# The non-color channel.
#
# Hue is the FIRST channel for a state, never the only one.  At 16 colors
# the palette provably collapses: `accent_text` and `error` both resolve to
# #FF0000, and `focus`, `hover`, `streaming` and `border_strong` all resolve
# to #FFFFFF.  Under NO_COLOR there is no hue at all.  So every state also
# carries a marker and a text label, and `channel_report()` states plainly
# which channels are load-bearing at the resolved depth.  A renderer that
# asks the token system what state something is therefore never has to
# answer it with a color.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateMarker:
    """One state's non-color channel.

    ``glyph`` is the Unicode marker, ``ascii_mark`` its legacy-encoding
    fallback, and ``label`` the always-available text alternative.  ``hue``
    names the token that carries this state's color, and may be empty for a
    state that is deliberately colorless.
    """

    name: str
    glyph: str
    ascii_mark: str
    label: str
    hue: str


STATE_MARKERS: tuple[StateMarker, ...] = (
    StateMarker("active", "◆", "*", "active", "accent_text"),
    StateMarker("streaming", "…", "...", "streaming", "streaming"),
    StateMarker("focus", "▸", ">", "focus", "focus"),
    StateMarker("selection", "▌", "|", "selected", "selection_bg"),
    StateMarker("hover", "·", ".", "hover", "hover"),
    StateMarker("pressed", "▾", "v", "pressed", "pressed"),
    StateMarker("approval", "⚠", "!", "approval", "approval"),
    StateMarker("success", "✔", "OK", "success", "success"),
    StateMarker("error", "✖", "x", "error", "error"),
    StateMarker("warning", "▲", "!", "warning", "warning"),
    StateMarker("info", "i", "i", "info", "info"),
    StateMarker("pending", "○", "o", "pending", "pending"),
    StateMarker("disabled", "-", "-", "disabled", "text_disabled"),
    # Verification is its own state family, not a success tint.  A verified
    # result and an unverified one must be distinguishable when there is no
    # color at all, so they carry different markers and different labels.
    StateMarker("verified", "✔", "+", "verified", "success"),
    StateMarker("unverified", "◇", "?", "unverified", "warning"),
)

_STATE_MARKERS_BY_NAME = {marker.name: marker for marker in STATE_MARKERS}

#: Rich role -> (token, TerminalTokens.style keyword arguments).  This table
#: is the ONLY place a Rich role is declared; `rich_theme()` generates the
#: theme from it, so a role cannot drift from the token it names.
RICH_ROLE_TOKENS: dict[str, tuple[str, Mapping[str, Any]]] = {
    "neo.accent": ("accent_text", {"bold": True}),
    "neo.accent2": ("accent_text", {}),
    "neo.running": ("accent_text", {}),
    "neo.glow": ("accent_glow", {}),
    "neo.ok": ("success", {}),
    "neo.error": ("error", {}),
    "neo.warn": ("warning", {}),
    "neo.info": ("info", {}),
    "neo.pending": ("pending", {}),
    "neo.disabled": ("text_disabled", {}),
    "neo.selection": ("selection_fg", {"background": "selection_bg"}),
    "neo.focus": ("focus", {}),
    "neo.hover": ("hover", {}),
    "neo.pressed": ("pressed", {}),
    "neo.streaming": ("streaming", {}),
    "neo.approval": ("approval", {"bold": True}),
    "neo.text": ("text_primary", {}),
    "neo.muted": ("text_secondary", {}),
    "neo.border": ("border_subtle", {}),
    "neo.background": ("bg_base", {"background": True}),
    "neo.panel": ("bg_panel", {"background": True}),
    "neo.panel.hover": ("bg_panel_hover", {"background": True}),
    "neo.border.strong": ("border_strong", {}),
    "neo.diff.add": ("diff_add", {}),
    "neo.diff.del": ("diff_delete", {}),
    "neo.diff.meta": ("diff_meta", {}),
    "neo.diff.hunk": ("diff_hunk", {}),
    "neo.diff.add_bg": ("diff_add_bg", {"background": True}),
    "neo.diff.del_bg": ("diff_delete_bg", {"background": True}),
}

#: Textual CSS variable -> token name, or a ``(token, alpha)`` pair for a value
#: derived from a token.  `textual_variables()` generates from this table.
TEXTUAL_VARIABLE_TOKENS: dict[str, Any] = {
    "neo-background": "bg_base",
    "neo-panel": "bg_panel",
    "neo-panel-hover": "bg_panel_hover",
    "neo-border": "border_subtle",
    "neo-border-strong": "border_strong",
    "neo-accent": "accent_text",
    "neo-accent-fill": "accent_primary",
    "neo-accent-pressed": "pressed",
    "neo-highlight": "accent_glow",
    "neo-text": "text_primary",
    "neo-secondary": "text_secondary",
    "neo-disabled": "text_disabled",
    "neo-success": "success",
    "neo-warning": "warning",
    "neo-error": "error",
    "neo-info": "info",
    "neo-pending": "pending",
    "neo-selection": ("selection_bg", "7F"),
    "neo-selection-text": "selection_fg",
    "neo-focus": "focus",
    "neo-hover": "hover",
    "neo-pressed": "pressed",
    "neo-streaming": "streaming",
    "neo-approval": "approval",
    "neo-diff-add": "diff_add",
    "neo-diff-delete": "diff_delete",
    "neo-diff-hunk": "diff_hunk",
    "neo-diff-meta": "diff_meta",
    "neo-diff-add-bg": "diff_add_bg",
    "neo-diff-delete-bg": "diff_delete_bg",
}

#: Tokens no renderer variable is generated for, each with the reason.  A
#: token added to TOKEN_NAMES without a role and without an entry here is a
#: token nothing can draw, so `token_coverage()` reports it and the token
#: test fails.  Only COLOR tokens belong here; a non-color facet of the theme
#: (motion, depth) is carried on `TerminalTokens` and needs no exemption.
UNMAPPED_TOKENS: Mapping[str, str] = {}


def token_coverage() -> dict[str, dict[str, Any]]:
    """Report, per token, which renderer surfaces can actually draw it.

    Returns ``{token: {"rich": role | None, "textual": variable | None,
    "reason": str | None}}``.  ``rich``/``textual`` are ``None`` for a token
    no renderer generates, and ``reason`` is then the documented reason — a
    token in neither bucket is a gap, not a preference.
    """
    rich_by_token = {token: role for role, (token, _) in RICH_ROLE_TOKENS.items()}
    for role, (_token, style_kwargs) in RICH_ROLE_TOKENS.items():
        background = style_kwargs.get("background")
        if isinstance(background, str) and background not in rich_by_token:
            rich_by_token.setdefault(background, f"{role} (background)")
    textual_by_token: dict[str, str] = {}
    for variable, spec in TEXTUAL_VARIABLE_TOKENS.items():
        source = spec[0] if isinstance(spec, tuple) else spec
        textual_by_token.setdefault(source, variable)
    coverage: dict[str, dict[str, Any]] = {}
    for name in TOKEN_NAMES:
        coverage[name] = {
            "rich": rich_by_token.get(name),
            "textual": textual_by_token.get(name),
            "reason": None,
        }
    for name, reason in UNMAPPED_TOKENS.items():
        coverage.setdefault(name, {"rich": None, "textual": None, "reason": None})[
            "reason"
        ] = reason
    return coverage


def unmapped_tokens() -> dict[str, str]:
    """Return tokens no renderer surface draws, mapped to their reason.

    A token with no role and no declared reason is a GAP, not a preference:
    the reason it gets back says so, and `tests/test_cli_theme.py` fails on
    a non-empty result.
    """
    return {
        name: (entry["reason"] or "no renderer role declared")
        for name, entry in token_coverage().items()
        if entry["rich"] is None and entry["textual"] is None and name in TOKEN_NAMES
    }


def state_names() -> tuple[str, ...]:
    """Return the names of every state that carries a non-color marker."""
    return tuple(marker.name for marker in STATE_MARKERS)


def _encodable(ch: str, stream: Any = None) -> bool:
    """Return whether a stream can encode a marker glyph."""
    if is_dumb_terminal():
        return False
    encoding = terminal_encoding(stream)
    try:
        ch.encode(encoding, errors="strict")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def state_marker(name: str, *, stream: Any = None) -> str:
    """Return the encoding-safe marker for a state, never an empty string.

    Falls back to the ASCII marker on a legacy console and to the text label
    when even ASCII is unavailable, so a renderer can always print something
    a reader can interpret without color.
    """
    marker = _STATE_MARKERS_BY_NAME.get(str(name or "").strip().lower())
    if marker is None:
        return ""
    if _encodable(marker.glyph, stream):
        return marker.glyph
    if _encodable(marker.ascii_mark, stream):
        return marker.ascii_mark
    return marker.label


def state_label(name: str) -> str:
    """Return the text alternative for a state's marker."""
    marker = _STATE_MARKERS_BY_NAME.get(str(name or "").strip().lower())
    return marker.label if marker is not None else ""


def state_hue(name: str) -> str:
    """Return the token carrying a state's hue, or an empty string."""
    marker = _STATE_MARKERS_BY_NAME.get(str(name or "").strip().lower())
    return marker.hue if marker is not None else ""


def state_markers(*, stream: Any = None) -> dict[str, str]:
    """Return every state's encoding-safe marker keyed by state name."""
    return {
        marker.name: state_marker(marker.name, stream=stream)
        for marker in STATE_MARKERS
    }


def channel_report(
    tokens: Optional[TerminalTokens] = None, *, stream: Any = None
) -> dict:
    """Describe which channels carry state at the resolved capability.

    ``hue`` is on only when the resolved depth can emit color.  ``marker`` and
    ``text`` are always on, which is the point: a renderer can always answer
    "what state is this?" without asking the color system.  ``hue_collisions``
    names every group of states that resolves to ONE color at this depth,
    together with the non-color channel that separates them — a collapse is
    reported, never silently accepted.
    """
    active = tokens or resolve_theme(depth=ColorDepth.TRUECOLOR)
    hue_on = active.depth is not ColorDepth.NONE and active.color_enabled
    groups: dict[str, list[str]] = {}
    for marker in STATE_MARKERS:
        groups.setdefault(active[marker.hue], []).append(marker.name)
    collisions = [
        {
            "token": hue,
            "states": sorted(names),
            "resolved": hue,
            "separated_by": [
                f"marker:{state_marker(name, stream=stream)}" for name in sorted(names)
            ]
            + [f"text:{state_label(name)}" for name in sorted(names)],
        }
        for hue, names in sorted(groups.items())
        if len(names) > 1
    ]
    return {
        "depth": active.depth.value,
        "hue": hue_on,
        "weight": True,
        "marker": True,
        "text": True,
        "legacy_encoding": active.legacy_encoding,
        "hue_collisions": collisions if hue_on else [],
        "hue_collision_count": len(collisions),
        "unmapped_tokens": unmapped_tokens(),
    }


def _parse_override_source(value: Any) -> Mapping[str, Any]:
    """Parse a mapping or JSON object containing user token overrides."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _fallback_values(colors: Mapping[str, str], depth: ColorDepth) -> dict[str, str]:
    """Return capability-specific values while preserving semantic names."""
    if depth in {ColorDepth.TRUECOLOR, ColorDepth.NONE}:
        return dict(colors)
    source = _FALLBACK_256 if depth is ColorDepth.ANSI256 else _FALLBACK_16
    return {name: source.get(name, value) for name, value in colors.items()}


def resolve_theme(
    name: Optional[str] = None,
    overrides: Any = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    depth: Optional[ColorDepth | str] = None,
    is_tty: Optional[bool] = None,
    legacy_encoding: Optional[bool] = None,
    strict: bool = False,
) -> TerminalTokens:
    """Resolve a named theme with optional user overrides and fallbacks.

    Inputs may be a theme name, a JSON override object, or a settings mapping.
    Invalid names and colors fall back to the default theme unless ``strict`` is
    true.  The function never reads secrets and only accepts known token names.
    """
    values_env = os.environ if env is None else env
    if config:
        config_name = config.get("theme")
        if name is None and config_name:
            name = str(config_name)
        if overrides is None:
            overrides = config.get("theme_overrides")
        if name is None and config.get("reduced_motion"):
            name = "reduced-motion"
    raw_name = str(name or values_env.get("NEO_THEME") or "default").strip().lower()
    normalized_name = _THEME_ALIASES.get(raw_name, raw_name.replace("_", "-"))
    definition = _BUILTIN_THEMES.get(normalized_name)
    if definition is None:
        if strict:
            raise ValueError(f"unknown terminal theme: {raw_name}")
        normalized_name = "default"
        definition = _BUILTIN_THEMES[normalized_name]
    combined: dict[str, Any] = {}
    combined.update(_parse_override_source(values_env.get("NEO_THEME_OVERRIDES")))
    combined.update(_parse_override_source(overrides))
    canonical = dict(definition.colors)
    motion = bool(definition.motion)
    source = "builtin" if not combined else "override"
    invalid: list[str] = []
    for raw_key, raw_value in combined.items():
        key = _canonical_name(raw_key)
        if key == "motion":
            motion = bool(raw_value)
            continue
        if key not in canonical:
            invalid.append(str(raw_key))
            continue
        if not _valid_color(raw_value):
            invalid.append(str(raw_key))
            continue
        canonical[key] = str(raw_value).strip().upper()
    if invalid and strict:
        raise ValueError(f"invalid terminal theme overrides: {', '.join(invalid)}")
    if depth is None:
        resolved_depth = resolve_color_depth(values_env, is_tty=is_tty)
    elif isinstance(depth, ColorDepth):
        resolved_depth = depth
    else:
        try:
            resolved_depth = ColorDepth(str(depth).strip().lower())
        except ValueError:
            if strict:
                raise
            resolved_depth = ColorDepth.TRUECOLOR
    legacy = is_legacy_encoding() if legacy_encoding is None else bool(legacy_encoding)
    return TerminalTokens(
        theme=normalized_name,
        depth=resolved_depth,
        colors=_fallback_values(canonical, resolved_depth),
        motion=motion,
        legacy_encoding=legacy,
        source=source,
    )


def rich_theme(tokens: Optional[TerminalTokens] = None) -> Any:
    """Build a Rich Theme from the authoritative role table.

    Generated from ``RICH_ROLE_TOKENS`` so a role can never be declared here
    and resolve to a different token than the one it names.
    """
    from rich.theme import Theme

    active = tokens or resolve_theme(depth=ColorDepth.TRUECOLOR)
    roles = {
        role: active.style(token_name, **style_kwargs)
        for role, (token_name, style_kwargs) in RICH_ROLE_TOKENS.items()
    }
    return Theme(roles)


def textual_variables(tokens: Optional[TerminalTokens] = None) -> dict[str, str]:
    """Return Textual CSS variables generated from the authoritative table."""
    active = tokens or resolve_theme(depth=ColorDepth.TRUECOLOR)
    return {
        variable: (
            _with_alpha(active[spec[0]], spec[1])
            if isinstance(spec, tuple)
            else active[spec]
        )
        for variable, spec in TEXTUAL_VARIABLE_TOKENS.items()
    }


def theme_summary(tokens: Optional[TerminalTokens] = None) -> dict[str, Any]:
    """Return a JSON-friendly description of a resolved token set."""
    active = tokens or resolve_theme(depth=ColorDepth.TRUECOLOR)
    return {
        "theme": active.theme,
        "depth": active.depth.value,
        "motion": active.motion,
        "legacy_encoding": active.legacy_encoding,
        "source": active.source,
        "tokens": active.as_dict(),
        "states": list(state_names()),
        "channels": channel_report(active),
        "coverage": token_coverage(),
    }


def theme_preview(
    tokens: Optional[TerminalTokens] = None,
    *,
    color: Optional[bool] = None,
    stream: Any = None,
) -> str:
    """Render a deterministic token preview for humans and terminal checks.

    The preview is the token system's own demonstration surface, so it shows
    all three requirements a reader has to be able to check by eye: every
    token's resolved value, the non-color channel each state keeps when hue
    is unavailable, and — stated plainly rather than left for the reader to
    discover — which states collapse onto one color at this depth.
    """
    active = tokens or resolve_theme(depth=ColorDepth.TRUECOLOR)
    show_color = (
        active.color_enabled if color is None else bool(color) and active.color_enabled
    )
    report = channel_report(active, stream=stream)
    lines = [
        f"Neo terminal theme: {active.theme} | depth: {active.depth.value} | "
        f"motion: {'on' if active.motion else 'off'} | source: {active.source}"
    ]
    for name in TOKEN_NAMES:
        value = active[name]
        swatch = f"[{value}]██[/]" if show_color else "[ ]"
        lines.append(f"{name:<18} {value} {swatch}")
    lines.append("")
    lines.append("state channels (marker + text survive every depth):")
    for marker in STATE_MARKERS:
        lines.append(
            f"  {marker.name:<11} {state_marker(marker.name, stream=stream):<4} "
            f"{marker.label:<11} hue={marker.hue}"
        )
    lines.append("")
    lines.append(
        "channels: hue {hue} | weight on | marker on | text on | "
        "collisions {count}".format(
            hue="on" if report["hue"] else "off", count=report["hue_collision_count"]
        )
    )
    for collision in report["hue_collisions"]:
        lines.append(
            f"  {collision['resolved']} carries "
            f"{' / '.join(collision['states'])} - separated by "
            f"{', '.join(collision['separated_by'])}"
        )
    if not report["hue"]:
        lines.append(
            "  no color is emitted at this depth; every state is carried by "
            "its marker and its label"
        )
    unmapped = report["unmapped_tokens"]
    lines.append(
        "token coverage: {drawn}/{total} tokens have a renderer role".format(
            drawn=len(TOKEN_NAMES) - len(unmapped), total=len(TOKEN_NAMES)
        )
    )
    for name, reason in sorted(unmapped.items()):
        lines.append(f"  unmapped {name}: {reason}")
    return "\n".join(lines)
