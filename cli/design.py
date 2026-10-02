"""cli/design.py — the ONE layout authority for the Neo terminal shell.

Why this file exists
--------------------
Neo rendered every panel it had. opencode renders almost none of them by
default, and that difference is the whole "cluttered, feels like a REPL"
complaint. The defect was never a missing widget or a missing rewrite — it was
a missing RULE, and a rule that lives in six widgets is not a rule.

So this module declares the shell's layout as DATA, and the test suite proves
nothing else declares it:

* every region, its widget id, and its rectangle at a given viewport;
* the breakpoints, the widths, the gutter, and the chrome arithmetic;
* the tri-state sidebar policy (``auto`` / ``show`` / ``hide``);
* the type scale and the single spacing unit;
* the density profiles and the rows they buy;
* the anti-clutter rule and its declared exemptions;
* the statusline's sections, their priority, and its explicit hint limit;
* the sidebar's collapsible sections and their persisted state.

``tests/test_design_layout.py::test_no_layout_constant_lives_outside_this_file``
scans every module under ``cli/`` with ``ast`` and fails if a layout constant
is defined anywhere else. A comment cannot satisfy it, a reformat cannot
empty it, and a new hard-coded breakpoint in a widget cannot hide from it.

Terminals have no font size
---------------------------
``TYPE_SCALE`` therefore declares FOUR roles, and a role whose notional size
differs from ``body`` is realised the only way a terminal can realise size:
by **weight/hue** and by a **column budget** (``max_columns``). A
``font-size: 1.2em`` here would be a number Textual does not implement and
nobody would notice, so there isn't one. ``pts`` is the notional size the
budget is derived from and is documented as exactly that.
"""

from __future__ import annotations

import html
import itertools
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cli.ui import DOT

# ---------------------------------------------------------------------------
# The palette is LOCKED and lives in cli/theme.py. This module holds no colour.
# ---------------------------------------------------------------------------

__all__ = [
    "AESTHETIC_PROPERTIES",
    "AESTHETIC_STATES",
    "ANTI_CLUTTER_EXEMPT",
    "ANTI_CLUTTER_MIN_ENTRIES",
    "CANCEL_AFFORDANCE_MIN_COLUMNS",
    "CHROME",
    "CONTENT_GUTTER_COLUMNS",
    "CONTEXT_MAX_WIDTH",
    "CONTEXT_MIN_COLUMNS",
    "CONTEXT_MIN_HEIGHT",
    "CONTEXT_MIN_WIDTH",
    "DECORATION_EXEMPT",
    "DEFAULT_DENSITY",
    "DEFAULT_SIDEBAR_MODE",
    "DENSITIES",
    "DENSITY_PROFILES",
    "DUPLICATE_EXEMPT",
    "DUPLICATE_PAIRS",
    "FRAME_PATTERNS",
    "HEADER_BUDGET",
    "HEADER_CHROME_COLUMNS",
    "HEADER_SEPARATOR",
    "HEADER_STATUS_MAX",
    "HEADER_TASK_FLOOR",
    "HEADER_VALUE_MAX",
    "INK_INSET_COLUMNS",
    "LAYOUT_AUTHORITY_SCOPE",
    "LAYOUT_SCOPE_EXEMPT",
    "LIVE_SURFACES",
    "MIN_RAIL_HEIGHT",
    "MOTION_GLYPHS",
    "NON_LIVE_SURFACES",
    "ORANGE_EXEMPT_TOKENS",
    "ORANGE_HUE_BAND",
    "PLAN_MIN_COLUMNS",
    "RAIL_BLOCK_SEPARATOR",
    "RAIL_PLACEMENT",
    "RAIL_PLACEMENTS",
    "REGIONS",
    "REGION_IDS",
    "SCREENSHOT_CHROME",
    "SIDEBAR_BREAKPOINT",
    "SIDEBAR_MODES",
    "SIDEBAR_MODE_ALIASES",
    "SIDEBAR_SECTIONS",
    "SIDEBAR_WIDTH",
    "SOURCE_TOKEN_EXEMPT",
    "SPACING_UNIT",
    "SPLIT_MIN_COLUMNS",
    "TYPE_ROLE_EVIDENCE",
    "TYPE_SCALE",
    "ULTRAWIDE_COLUMNS",
    "UNTOKENED_RECEIPT_EXEMPT",
    "VERTICAL_HEIGHT_RATIO",
    "VERTICAL_MIN_HEIGHT",
    "AestheticProperty",
    "AestheticReport",
    "AlignmentReport",
    "Cell",
    "DensityProfile",
    "DuplicateReport",
    "FrameAudit",
    "HeaderBudget",
    "HierarchyReport",
    "LayoutSpec",
    "Region",
    "RenderedFrame",
    "ResponsivenessReport",
    "RestraintReport",
    "RhythmReport",
    "SidebarSection",
    "StatusSection",
    "StatuslineFit",
    "UiPreferences",
    "aesthetic_report",
    "alignment_report",
    "audit_frame",
    "content_width",
    "density_profile",
    "density_report",
    "duplicate_report",
    "expected_ink_edges",
    "fit_statusline",
    "hex_literal_audit",
    "hierarchy_report",
    "load_preferences",
    "parse_svg",
    "preferences_path",
    "rail_block_gap",
    "receipt_token_audit",
    "region_names",
    "resolve_layout",
    "responsiveness_report",
    "restraint_report",
    "rhythm_report",
    "save_preferences",
    "section_is_collapsible",
    "section_is_rendered",
    "sidebar_footer",
    "sidebar_visible",
    "source_token_audit",
    "spacing",
    "statusline_sections",
    "surface_facts",
    "token_hexes",
    "type_role",
    "usable_rows",
    "vertical_terminal",
    "warm_hue_audit",
]

# ---------------------------------------------------------------------------
# Breakpoints, widths, and the gutter
# ---------------------------------------------------------------------------

#: A deliberately narrow split terminal. Below this the shell collapses BOTH
#: rails rather than squeezing the conversation between them: a 42-column
#: sidebar plus a 30-column context rail leaves 0 usable columns at 60.
SPLIT_MIN_COLUMNS = 72

#: A vertical terminal is one that is clearly taller than it is wide.
VERTICAL_HEIGHT_RATIO = 1.35
VERTICAL_MIN_HEIGHT = 80

#: The left rail's floor width. This is the historical `_PLAN_BREAKPOINT = 96`
#: and it is kept, deliberately, as the ``show`` mode's floor (see
#: `sidebar_visible`). The responsive 120 breakpoint below is the NEW
#: ``auto`` policy; the shipped default is ``show`` so the shell nobody
#: opted out of keeps the region it has always had.
PLAN_MIN_COLUMNS = 96

#: The sidebar's own responsive breakpoint, under ``auto``. The rule is
#: ``width > SIDEBAR_BREAKPOINT``, so the sidebar is visible at 121 and
#: hidden at 120 — measured, not asserted.
SIDEBAR_BREAKPOINT = 120

#: The sidebar's width. It is fixed rather than a fraction because a
#: fractional rail is a rail whose content rewraps on every column of resize.
SIDEBAR_WIDTH = 42

#: Columns the shell keeps for the transcript's own frame and the boundary
#: between regions. Content width is therefore
#: ``width - (sidebar ? SIDEBAR_WIDTH : 0) - CONTENT_GUTTER_COLUMNS``.
CONTENT_GUTTER_COLUMNS = 4

#: The context rail's own floors.
CONTEXT_MIN_COLUMNS = 120
CONTEXT_MIN_HEIGHT = 26
CONTEXT_MIN_WIDTH = 30
CONTEXT_MAX_WIDTH = 34

#: At and above this the context rail takes its wider width.
ULTRAWIDE_COLUMNS = 160

#: The left rail's floor height, and the narrowest width at which the cancel
#: affordance is still shown (VEX-CEILING-10's floor).
MIN_RAIL_HEIGHT = 22
CANCEL_AFFORDANCE_MIN_COLUMNS = 62

# ---------------------------------------------------------------------------
# The one spacing unit
# ---------------------------------------------------------------------------

#: EVERY margin, padding, and gap in the shell is a multiple of this. One
#: number, so "make it roomier" is one edit rather than forty.
SPACING_UNIT = 1


def spacing(units: int | float) -> int:
    """Return `units` spacing units as terminal rows/columns.

    The ONLY way a caller turns a spacing decision into a number, so a
    padding can never drift off the unit.
    """
    return round(float(units) * SPACING_UNIT)


# ---------------------------------------------------------------------------
# The type scale — four roles, realised as weight + hue + column budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypeRole:
    """One rung of the type scale.

    `pts` is the NOTIONAL size the column budget is derived from; a terminal
    has no font size, so the scale is realised by `weight`/`hue` and by
    `max_columns`. There is deliberately no `font-size` anywhere in this
    module — see the module docstring.
    """

    name: str
    pts: float
    css_class: str
    weight: str
    hue: str
    max_columns: int


TYPE_SCALE: Mapping[str, TypeRole] = {
    "display": TypeRole("display", 1.6, "neo-type-display", "bold", "accent", 48),
    "title": TypeRole("title", 1.25, "neo-type-title", "bold", "accent", 36),
    "body": TypeRole("body", 1.0, "neo-type-body", "normal", "primary", 120),
    "micro": TypeRole("micro", 0.85, "neo-type-micro", "normal", "secondary", 64),
}

#: The scale's rungs, largest first. The ORDER is the contract: a new role
#: that is not one of these four has not been placed on the scale.
TYPE_SCALE_ORDER: Tuple[str, ...] = ("display", "title", "body", "micro")


def type_role(name: str) -> TypeRole:
    """Return the declared type role, or the `body` role for an unknown name.

    Fails SOFT: an unrecognised role must not take a panel down, and
    silently falling back to body is the only answer that cannot lie about
    the size being used.
    """
    return TYPE_SCALE.get(str(name or "").strip().lower(), TYPE_SCALE["body"])


# ---------------------------------------------------------------------------
# Density — a measurable change in rows per screen, not a label
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DensityProfile:
    """How much chrome the shell spends per screen at one density."""

    name: str
    composer_height: int
    block_gap: int
    section_gap: int
    statusline_reserved_rows: int

    @property
    def chrome_rows(self) -> int:
        """Total fixed rows the shell costs before the middle region."""
        rows = 0
        for part in CHROME.values():
            rows += int(part.get("rows", 0))
        return rows + spacing(self.composer_height) + self.statusline_reserved_rows


#: Every fixed band of the shell, and the rows each costs. The run line and
#: the announcement band are counted even when they are empty: the shell's
#: arithmetic UNDER-promises on purpose, so a rail can never be handed rows
#: the composer needs.
CHROME: Mapping[str, Mapping[str, int]] = {
    "header": {"rows": 1},
    "runline": {"rows": 1},
    "announce": {"rows": 1},
    "footer": {"rows": 1},
}

DENSITIES: Tuple[str, ...] = ("comfortable", "compact")
DEFAULT_DENSITY = "comfortable"

DENSITY_PROFILES: Mapping[str, DensityProfile] = {
    "comfortable": DensityProfile(
        name="comfortable",
        composer_height=3,
        block_gap=1,
        section_gap=1,
        statusline_reserved_rows=0,
    ),
    # Compact buys its rows from two places, and both are measurable: a
    # one-row composer and no gap between rail blocks. At 200x50 that is 44
    # usable middle rows instead of 43, and a five-block context rail gets
    # five rows back.
    "compact": DensityProfile(
        name="compact",
        composer_height=2,
        block_gap=0,
        section_gap=0,
        statusline_reserved_rows=0,
    ),
}


def density_profile(density: str = DEFAULT_DENSITY) -> DensityProfile:
    """Return the declared density profile (unknown names get `comfortable`).

    Fails SAFE: compact is the profile that removes decoration, so a typo
    must never be able to remove a row the composer needs.
    """
    return DENSITY_PROFILES.get(
        str(density or "").strip().lower(), DENSITY_PROFILES[DEFAULT_DENSITY]
    )


def rail_block_gap(density: str = DEFAULT_DENSITY) -> int:
    """Rows a rail spends between two blocks at this density."""
    return spacing(density_profile(density).block_gap)


def usable_rows(height: int, density: str = DEFAULT_DENSITY) -> int:
    """Rows left for the middle region (transcript + rails) at this height."""
    return max(0, int(height or 0) - density_profile(density).chrome_rows)


# ---------------------------------------------------------------------------
# The anti-clutter rule
# ---------------------------------------------------------------------------

#: A section is rendered only when it offers at least this many entries, so
#: **a section with two or fewer entries is not rendered at all.** Two rows
#: of chrome that say almost nothing cost the reader more than they tell
#: them, and they are the reason the shell felt like a REPL.
#:
#: `cli.toggles.MIN_SECTION_ENTRIES` declares the same threshold for the
#: toggle surfaces, and
#: `tests/test_design_layout.py::test_the_anti_clutter_threshold_is_one_number`
#: pins the two equal — one rule, two homes, a gate that says so.
ANTI_CLUTTER_MIN_ENTRIES = 3

#: Rail blocks the rule does NOT apply to, each with the reason. A section
#: count is counted in DATA entries, never including a block's heading.
#:
#: This is a declared exemption list rather than silence for two reasons: a
#: rule with named exceptions can be argued with, and an unstated one is a
#: hole. `usage` and `files` and `diagnostics` are exempt because a run with
#: ONE of those facts is exactly the run a reader opened the rail to see, and
#: `tests/test_cli_tui_layout.py::test_context_panel_is_projected_from_
#: journal_events` pins a single-file and a single-diagnostic block on screen.
ANTI_CLUTTER_EXEMPT: Mapping[str, str] = {
    "usage": (
        "the run's verification state, cost, and call counts are the product's "
        "central promise; two of them is still a run worth reading about"
    ),
    "files": "a single changed file is the most load-bearing row a rail can carry",
    "diagnostics": "one diagnostic is the reason /diagnostics exists",
}


def section_is_rendered(entries: Sequence[Any] | int | None) -> bool:
    """Whether a section is rendered under the anti-clutter rule.

    Counts the section's ENTRIES, never the rows that survive a row budget:
    a section admitted with a `+N more` marker is a rendered section. A
    section whose content is thin is the thing the rule is about.
    """
    count = (
        entries if isinstance(entries, int) else len([item for item in (entries or ())])
    )
    return count >= ANTI_CLUTTER_MIN_ENTRIES


def section_is_collapsible(entries: Sequence[Any] | int | None) -> bool:
    """Whether a section earns a collapse triangle.

    Only a section the anti-clutter rule RENDERS can be collapsed: a
    triangle on a section that is not there is a control for nothing.
    """
    return section_is_rendered(entries)


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------

#: region name -> the Textual widget id that region is mounted as. The test
#: asserts every id resolves in the real app at every viewport.
REGION_IDS: Mapping[str, str] = {
    "header": "neo-header",
    "statusline": "neo-statusline",
    "transcript": "neo-body",
    "sidebar": "neo-side",
    "context": "neo-context",
    "runline": "neo-runline",
    "announce": "neo-announce",
    "composer": "neo-inputwrap",
}

#: WHICH SIDE the two rails sit on, relative to the transcript.
#:
#: This was previously implicit and WRONG. `NeoApp.compose` mounts
#: `#neo-body`, then `#neo-side`, then `#neo-context` inside one
#: `Horizontal`, so the product's own DOM puts the plan rail to the RIGHT of
#: the transcript. This module placed `sidebar` at `x = 0` — to the LEFT —
#: so the ONE layout authority and the product it describes disagreed about
#: the most basic question the shell asks, and no test caught it because
#: nothing compared a `Region` to a mounted widget's own `region.x`.
#:
#: Measured on the live app at 100x30 (`parse_svg` on `export_screenshot`):
#: the plan rail's first text column is **59** and its right border is **99**,
#: i.e. the rail occupies columns 58..99 — the right edge of the terminal.
#: The authority now says the same thing, and
#: `tests/test_aesthetic_gate.py::test_the_authority_and_the_product_agree_about
#: _which_side_a_rail_is_on` is the gate that keeps them from drifting again.
#:
#: `"right"` is a decision, not an accident: a rail on the right keeps the
#: conversation flush with the left margin, where a reader's eye and the
#: window manager's own edge already are. `"left"` remains declared so the
#: choice is an editable one rather than an emergent one.
RAIL_PLACEMENT = "right"

#: The sides the rails may be placed on. A value outside this set is refused
#: by :func:`resolve_layout`, because a layout that cannot be rendered is
#: worse than one that falls back to a known one.
RAIL_PLACEMENTS: Tuple[str, ...] = ("left", "right")

#: The declared regions, in DOM order (which is reading order, and which is
#: also the mount order `NeoApp.compose` uses: header, statusline, then the
#: transcript and its rails, then the composer).
REGIONS: Tuple[str, ...] = (
    "header",
    "statusline",
    "transcript",
    "sidebar",
    "context",
    "runline",
    "announce",
    "composer",
)


def region_names() -> Tuple[str, ...]:
    """The declared region names, in DOM order."""
    return REGIONS


@dataclass(frozen=True)
class Region:
    """One region's rectangle in a resolved layout, in terminal cells."""

    name: str
    widget_id: str
    x: int
    y: int
    width: int
    height: int

    @property
    def visible(self) -> bool:
        """Whether this region occupies at least one cell."""
        return self.width > 0 and self.height > 0

    @property
    def right(self) -> int:
        """The first column past this region."""
        return self.x + self.width

    @property
    def bottom(self) -> int:
        """The first row past this region."""
        return self.y + self.height

    def within(self, width: int, height: int) -> bool:
        """Whether this region is entirely inside the viewport."""
        if self.x < 0 or self.y < 0:
            return False
        return self.right <= int(width) and self.bottom <= int(height)


# ---------------------------------------------------------------------------
# The sidebar's tri-state policy
# ---------------------------------------------------------------------------

SIDEBAR_MODES: Tuple[str, ...] = ("auto", "show", "hide")


def design_mode_order() -> Tuple[str, ...]:
    """The order one keypress walks the sidebar's modes in.

    ``auto -> show -> hide -> auto``: the cycle starts at the responsive
    policy, forces the column on, forces it off, and comes back. A cycle is
    declared rather than derived from a dict so the key is a small, readable
    thing and its order is a decision rather than an accident.
    """
    return SIDEBAR_MODES


#: ``cli.toggles`` (Prompt 04's toggle registry) speaks the USER vocabulary
#: for the same three states — ``auto`` / ``shown`` / ``hidden`` — and owns
#: the key that advances it. This map is the whole translation layer, so the
#: toggle module is never asked to speak the layout module's words and the
#: shell is never handed a mode it does not have a rule for.
SIDEBAR_MODE_ALIASES: Mapping[str, str] = {
    "auto": "auto",
    "follow": "auto",
    "shown": "show",
    "show": "show",
    "always": "show",
    "on": "show",
    "hidden": "hide",
    "hide": "hide",
    "never": "hide",
    "off": "hide",
}

#: ``show`` is the shipped default, and the reason is that it is the mode
#: which PRESERVES the shell that already exists: the left rail has always
#: appeared from 96 columns, and shipping ``auto`` as the unset default
#: would take a region away from every user who never asked for a
#: responsive shell. ``auto`` is a real, tested, reachable mode — visible at
#: 121, hidden at 120 — and it is what ``ctrl+5`` / ``/sidebar`` select.
#:
#: `cli.toggles.TOGGLE_DEFAULTS["sidebar"]` is ``"auto"``. The two differ
#: DELIBERATELY, and only for the UNSET case: an explicit choice, from the
#: key or the command, is honoured exactly. The constraint that pins the
#: unset case is
#: `tests/test_cli_tui_layout.py::test_responsive_policy_collapses_rails_deliberately`,
#: which asserts the rail is visible at 100x30. Shipping ``auto`` by default
#: means changing this constant AND retargeting that one row; both are
#: filed in `cli/AGENTS.md`.
DEFAULT_SIDEBAR_MODE = "show"


def normalize_sidebar_mode(mode: Any) -> str:
    """Resolve any spelling of the sidebar mode to a declared mode.

    An unrecognised value resolves to :data:`DEFAULT_SIDEBAR_MODE`, because
    a preference file is a file a person can edit and a typo must not be
    able to hide a region.
    """
    key = str(mode or "").strip().lower()
    return SIDEBAR_MODE_ALIASES.get(key, DEFAULT_SIDEBAR_MODE)


def vertical_terminal(width: int, height: int) -> bool:
    """Whether this viewport is a vertical (taller than wide) terminal."""
    safe_width = max(1, int(width or 0))
    return int(height or 0) >= max(
        VERTICAL_MIN_HEIGHT, int(safe_width * VERTICAL_HEIGHT_RATIO)
    )


def split_terminal(width: int) -> bool:
    """Whether this viewport is a deliberately narrow split terminal."""
    return int(width or 0) < SPLIT_MIN_COLUMNS


def sidebar_visible(
    width: int,
    height: int = 24,
    mode: str = DEFAULT_SIDEBAR_MODE,
) -> bool:
    """Whether the left sidebar is shown, under one of the three modes.

    ``auto``  — shown only when the terminal is wider than
    :data:`SIDEBAR_BREAKPOINT`. Visible at 121, hidden at 120.
    ``show``  — shown wherever the shell can physically carry it, which is
    the historical policy: from :data:`PLAN_MIN_COLUMNS`, never in a split
    or vertical terminal, never below :data:`MIN_RAIL_HEIGHT`.
    ``hide``  — never. The mode a user picks when they want the whole
    terminal for the conversation.
    """
    safe_width = max(1, int(width or 0))
    safe_height = max(0, int(height or 0))
    if split_terminal(safe_width) or vertical_terminal(safe_width, safe_height):
        return False
    resolved = normalize_sidebar_mode(mode)
    if resolved == "hide":
        return False
    if resolved == "auto":
        return safe_width > SIDEBAR_BREAKPOINT and safe_height >= MIN_RAIL_HEIGHT
    return safe_width >= PLAN_MIN_COLUMNS and safe_height >= MIN_RAIL_HEIGHT


def sidebar_width(visible: bool) -> int:
    """The sidebar's width, or zero when it is not shown."""
    return SIDEBAR_WIDTH if visible else 0


def context_width(width: int, visible: bool) -> int:
    """The context rail's width, or zero when it is not shown."""
    if not visible:
        return 0
    return (
        CONTEXT_MAX_WIDTH if int(width or 0) >= ULTRAWIDE_COLUMNS else CONTEXT_MIN_WIDTH
    )


def context_visible(width: int, height: int = 24) -> bool:
    """Whether the right context rail is shown at this viewport."""
    safe_width = max(1, int(width or 0))
    if split_terminal(safe_width) or vertical_terminal(safe_width, height):
        return False
    return safe_width >= CONTEXT_MIN_COLUMNS and int(height or 0) >= CONTEXT_MIN_HEIGHT


def content_width(width: int, sidebar_shown: bool) -> int:
    """The transcript's content width at this viewport.

    Exactly the declared formula: ``width - (sidebar ? 42 : 0) - 4``.
    """
    return max(
        1,
        int(width or 0)
        - (sidebar_width(bool(sidebar_shown)))
        - spacing(CONTENT_GUTTER_COLUMNS),
    )


# ---------------------------------------------------------------------------
# The header's column budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderBudget:
    """The column budget of the persistent header.

    Lives here because it IS layout: how many columns the header spends on
    its own chrome decides whether the last segment's value survives.
    """

    chrome_columns: int
    value_max: Mapping[str, int]
    status_max: int
    task_floor: int
    separator: str

    def separator_columns(self) -> int:
        """The columns the separator costs AS RENDERED.

        Not 1. A budget that counts the separator as one column is a budget
        that clips the last segment's value, which is how a header once
        rendered `◆ neo 0.2.1 · mode` — a label with no value.
        """
        return len(self.separator)


HEADER_BUDGET = HeaderBudget(
    chrome_columns=4,
    value_max={"repo": 24, "model": 28, "mode": 18},
    status_max=24,
    task_floor=8,
    separator=f" {DOT} ",
)

#: The budget's parts, exported individually so a caller imports the fact it
#: needs instead of reaching into a dataclass for every comparison.
HEADER_CHROME_COLUMNS = HEADER_BUDGET.chrome_columns
HEADER_VALUE_MAX = HEADER_BUDGET.value_max
HEADER_STATUS_MAX = HEADER_BUDGET.status_max
HEADER_TASK_FLOOR = HEADER_BUDGET.task_floor
HEADER_SEPARATOR = HEADER_BUDGET.separator

#: The row a rail block boundary costs, taken from the shipped density's
#: gap rather than restated, so "one row between blocks" and the
#: comfortable/compact switch are the same decision.
RAIL_BLOCK_SEPARATOR = DENSITY_PROFILES[DEFAULT_DENSITY].block_gap

#: What this file is the authority FOR, so a reader can tell a genuine second
#: authority from a constant that is about something else. The gate is
#: `tests/test_design_layout.py::test_no_layout_constant_lives_outside_design`.
LAYOUT_AUTHORITY_SCOPE = (
    "the six shell regions and their widget ids",
    "every breakpoint, width, and the gutter",
    "the chrome arithmetic and the usable-row count",
    "the header's column budget",
    "the one spacing unit",
    "the type scale",
    "the density profiles",
    "the anti-clutter rule and its declared exemptions",
    "the statusline's sections, priority, and hint limit",
    "the sidebar's sections, collapse keys, and mode vocabulary",
)

#: Layout-shaped constants that live OUTSIDE the shell and are therefore not
#: this file's to own, each with the reason. A gate with named, reasoned
#: exceptions can be argued with; a gate with hidden ones is theatre.
#:
#: `cli/review.py` is a MODAL's internal diff layout, not the shell's, and it
#: belongs to whoever owns the review surface. Moving it here is a
#: cross-terminal request, not a silent exemption.
LAYOUT_SCOPE_EXEMPT: Mapping[str, str] = {
    "review.py:DIFF_STYLE_SPLIT_MIN_COLUMNS": (
        "the diff-review modal's own column split inside a 96-column modal; "
        "it is not a shell region width and it is not reachable from the "
        "shell's layout. Owner: the review surface."
    ),
}


# ---------------------------------------------------------------------------
# The resolved layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutSpec:
    """One resolved shell layout: every region, every policy decision."""

    width: int
    height: int
    density: str
    sidebar_mode: str
    rail_placement: str
    split: bool
    vertical: bool
    sidebar_shown: bool
    sidebar_cols: int
    context_shown: bool
    context_cols: int
    content_cols: int
    composer_height: int
    statusline_rows: int
    show_model: bool
    show_repo: bool
    task_width: int
    modal_width: int
    modal_max_width: int
    regions: Tuple[Region, ...]

    def region(self, name: str) -> Region:
        """Return one declared region by name.

        Raises `KeyError` for an undeclared name rather than inventing a
        zero rectangle: a region nobody declared is a bug, and a silent
        zero is how a bug reads as "the rail is just empty".
        """
        for item in self.regions:
            if item.name == name:
                return item
        raise KeyError(name)

    def out_of_bounds(self) -> Tuple[str, ...]:
        """Names of regions that are not fully inside the viewport.

        Empty is the pass condition; this is the receipt a test asserts on
        rather than a re-derivation of the arithmetic.
        """
        return tuple(
            item.name
            for item in self.regions
            if not item.within(self.width, self.height)
        )

    @property
    def sidebar_width(self) -> int:
        """The sidebar's width, or zero when it is not shown."""
        return self.sidebar_cols

    @property
    def usable_rows(self) -> int:
        """Rows left for the middle region at this density."""
        return usable_rows(self.height, self.density)


def resolve_layout(
    width: int,
    height: int = 24,
    *,
    sidebar: str = DEFAULT_SIDEBAR_MODE,
    density: str = DEFAULT_DENSITY,
    statusline_rows: int = 0,
    placement: str = RAIL_PLACEMENT,
) -> LayoutSpec:
    """Resolve the whole shell layout for one viewport. Pure; never raises.

    `statusline_rows` is 0 when the statusline has nothing true to say, and
    a statusline that says nothing is not a row the shell spends.

    `placement` is the SIDE the rails sit on (`RAIL_PLACEMENT`). It is a
    parameter rather than a constant baked into the arithmetic because a
    caller that wants to reason about the other side must be able to ASK
    without editing this file; an unrecognised value falls back to the
    declared one.
    """
    safe_width = max(1, int(width or 0))
    safe_height = max(1, int(height or 0))
    profile = density_profile(density)
    mode = str(sidebar or "").strip().lower()
    resolved_mode = mode if mode in SIDEBAR_MODES else DEFAULT_SIDEBAR_MODE

    split = split_terminal(safe_width)
    vertical = vertical_terminal(safe_width, safe_height)
    side_shown = sidebar_visible(safe_width, safe_height, resolved_mode)
    ctx_shown = context_visible(safe_width, safe_height)
    side_cols = sidebar_width(side_shown)
    ctx_cols = context_width(safe_width, ctx_shown)
    content = content_width(safe_width - ctx_cols, side_shown)
    status_rows = max(0, int(statusline_rows or 0))

    header_rows = spacing(CHROME["header"]["rows"])
    composer_rows = spacing(profile.composer_height)
    footer_rows = spacing(CHROME["footer"]["rows"])
    middle_top = header_rows
    middle_rows = max(
        0, safe_height - header_rows - status_rows - composer_rows - footer_rows
    )
    placement = placement if placement in RAIL_PLACEMENTS else RAIL_PLACEMENT
    # The transcript takes every column the rails do not, and the rails sit
    # against the far edge in the direction `RAIL_PLACEMENT` declares. The
    # two rails never overlap the transcript and never overlap each other,
    # which is the whole point of the arithmetic: the content width is the
    # remainder, not a fraction nobody can predict.
    if placement == "left":
        sidebar_x = 0
        transcript_x = sidebar_x + side_cols
        transcript_width = max(0, safe_width - side_cols - ctx_cols)
    else:
        transcript_x = 0
        transcript_width = max(0, safe_width - side_cols - ctx_cols)
        sidebar_x = transcript_x + transcript_width
    context_x = max(0, sidebar_x + side_cols)
    transcript_width = max(0, min(transcript_width, context_x - transcript_x))
    composer_y = max(0, safe_height - composer_rows - footer_rows)
    statusline_y = max(0, composer_y - status_rows)
    # The run line and the announcement band are the middle region's LAST TWO
    # ROWS, and they are declared even though `CHROME` counts them
    # unconditionally. Two things follow, and both were found by measuring
    # the receipt rather than by reading this file:
    #
    # * The authority keeps under-promising. `middle_rows` does not subtract
    #   these two, so the declared transcript rectangle is deliberately
    #   larger than the text area the composer really leaves -- that is the
    #   documented choice, and the two bands are carved out of its BOTTOM so
    #   the declaration and the product agree about where they are.
    # * Without the declaration, a run line's cells at column 0 were
    #   attributed to the TRANSCRIPT by the aesthetic gate, which then read
    #   the gap between the transcript's last line and the run line as an
    #   18-row rhythm break. A band the authority does not name is a band
    #   every consumer has to guess about.
    runline_y = max(middle_top, middle_top + middle_rows - 2)
    announce_y = max(middle_top, middle_top + middle_rows - 1)

    regions: Tuple[Region, ...] = (
        Region("header", REGION_IDS["header"], 0, 0, safe_width, header_rows),
        # The statusline sits DIRECTLY ABOVE the composer: it is the bottom
        # bar, and a bottom bar that renders under the transcript is a bar
        # that reads as part of it.
        Region(
            "statusline",
            REGION_IDS["statusline"],
            0,
            statusline_y,
            safe_width,
            status_rows,
        ),
        Region(
            "sidebar",
            REGION_IDS["sidebar"],
            sidebar_x,
            middle_top,
            side_cols,
            middle_rows if side_shown else 0,
        ),
        Region(
            "transcript",
            REGION_IDS["transcript"],
            transcript_x,
            middle_top,
            transcript_width,
            middle_rows,
        ),
        Region(
            "context",
            REGION_IDS["context"],
            context_x,
            middle_top,
            ctx_cols,
            middle_rows if ctx_shown else 0,
        ),
        Region(
            "runline",
            REGION_IDS["runline"],
            0,
            runline_y,
            safe_width,
            spacing(CHROME["runline"]["rows"]),
        ),
        Region(
            "announce",
            REGION_IDS["announce"],
            0,
            announce_y,
            safe_width,
            spacing(CHROME["announce"]["rows"]),
        ),
        Region(
            "composer",
            REGION_IDS["composer"],
            0,
            composer_y,
            safe_width,
            composer_rows,
        ),
    )
    return LayoutSpec(
        width=safe_width,
        height=safe_height,
        density=profile.name,
        sidebar_mode=resolved_mode,
        rail_placement=placement,
        split=split,
        vertical=vertical,
        sidebar_shown=side_shown,
        sidebar_cols=side_cols,
        context_shown=ctx_shown,
        context_cols=ctx_cols,
        content_cols=content,
        composer_height=spacing(profile.composer_height),
        statusline_rows=status_rows,
        show_model=safe_width >= 80,
        show_repo=safe_width >= 72,
        task_width=14 if safe_width < 80 else 20 if safe_width < 120 else 24,
        modal_width=max(1, min(96, safe_width - 4)),
        modal_max_width=max(1, safe_width - 2),
        regions=regions,
    )


# ---------------------------------------------------------------------------
# The statusline
# ---------------------------------------------------------------------------

#: The statusline's explicit hint limit. A statusline with no limit is a
#: statusline that eventually shows nine facts and is a REPL again.
STATUSLINE_HINT_LIMIT = 4

#: What the statusline puts BETWEEN sections, as rendered. The middle dot is
#: the product's own separator (`cli.ui.DOT`), not a second glyph.
STATUSLINE_SEPARATOR = f" {DOT} "


@dataclass(frozen=True)
class StatusSection:
    """One statusline section: a fact, its priority, and the key to reach it."""

    key: str
    label: str
    priority: int
    keybind: str = ""

    def render(self, count: Any = None, detail: str = "") -> str:
        """The section's plain text, or `""` when it has nothing to say.

        An empty string is a first-class answer: "a hint must be true", so
        a section whose count is zero renders nothing at all rather than a
        `0 queued` that reads as a claim.
        """
        if count is None or int(count) <= 0:
            return ""
        body = f"{int(count)} {self.label}" if not detail else f"{int(count)} {detail}"
        return f"{body} ({self.keybind})" if self.keybind else body


#: The statusline's sections, in ADMISSION order. Priority is the order a
#: narrowing terminal drops them from the TAIL, so a fact nobody can act on
#: is the first thing to go.
STATUSLINE_SECTIONS: Tuple[StatusSection, ...] = (
    StatusSection("queue", "queued", 0, "ctrl+g"),
    StatusSection("subagents", "subagents", 1, "ctrl+b"),
    StatusSection("background", "background", 2, "ctrl+o"),
    StatusSection("density", "density", 3, "ctrl+o"),
    StatusSection("sidebar", "sidebar", 4, "ctrl+5"),
)


def statusline_sections() -> Tuple[StatusSection, ...]:
    """The declared statusline sections, in admission order."""
    return STATUSLINE_SECTIONS


@dataclass(frozen=True)
class StatuslineFit:
    """The receipt for one statusline fit."""

    text: str
    kept: Tuple[str, ...]
    dropped: Tuple[str, ...]
    limited: bool
    overflow: bool

    @property
    def sections(self) -> Tuple[str, ...]:
        """The section keys that were rendered."""
        return self.kept


def fit_statusline(
    sections: Mapping[str, Any] | Sequence[Tuple[str, str]],
    width: int,
    *,
    hint_limit: int = STATUSLINE_HINT_LIMIT,
) -> StatuslineFit:
    """Fit the statusline into `width` columns, dropping by priority.

    `sections` is either a mapping of section key -> rendered text (already
    truth-checked by the caller, so an empty value is simply absent) or a
    sequence of ``(key, text)`` pairs. A section whose text is empty is
    NEVER rendered: that is the "a hint must be true" rule, and it is why
    the count is the caller's decision and not this function's guess.
    """
    limit = max(0, int(hint_limit))
    available = max(0, int(width or 0))
    pairs: list[tuple[str, str]] = (
        [(str(key), str(value)) for key, value in sections.items()]
        if isinstance(sections, Mapping)
        else [(str(key), str(value)) for key, value in sections]
    )
    by_key = {item.key: item for item in STATUSLINE_SECTIONS}
    ordered: list[tuple[str, str]] = []
    for key, value in pairs:
        text = str(value or "").strip()
        if not text:
            continue
        item = by_key.get(key)
        priority = item.priority if item is not None else len(STATUSLINE_SECTIONS)
        ordered.append((f"{priority:03d}:{key}", text))
    ordered.sort(key=lambda pair: pair[0])

    kept: list[str] = []
    keys: list[str] = []
    dropped: list[str] = []
    used = 0
    limited = False
    for sort_key, text in ordered:
        key = sort_key.split(":", 1)[1]
        cost = len(text) if not keys else len(text) + len(STATUSLINE_SEPARATOR)
        if len(keys) >= limit:
            limited = True
            dropped.append(key)
            continue
        if used + cost > available:
            dropped.append(key)
            continue
        used += cost
        kept.append(text)
        keys.append(key)
    return StatuslineFit(
        text=STATUSLINE_SEPARATOR.join(kept),
        kept=tuple(keys),
        dropped=tuple(dropped),
        limited=limited,
        overflow=bool(dropped),
    )


# ---------------------------------------------------------------------------
# The sidebar's collapsible sections
# ---------------------------------------------------------------------------

#: The sidebar's sections, in reading order. `key` is the persistence key
#: and the collapse triangle's identity; changing one silently orphans
#: everybody's saved collapse state, so they are stable strings.
SIDEBAR_SECTIONS: Tuple[str, ...] = (
    "session",
    "context",
    "mcp",
    "lsp",
    "todo",
    "files",
    "startup",
)

#: The triangle, expanded and collapsed. A section is only collapsible when
#: the anti-clutter rule renders it, so a triangle is always a control for
#: something that is there.
TRIANGLE_EXPANDED = "▾"
TRIANGLE_COLLAPSED = "▸"


@dataclass(frozen=True)
class SidebarSection:
    """One rendered sidebar section."""

    key: str
    title: str
    entries: Tuple[str, ...] = ()
    collapsed: bool = False

    @property
    def rendered(self) -> bool:
        """Whether the anti-clutter rule renders this section at all."""
        return section_is_rendered(self.entries)

    @property
    def collapsible(self) -> bool:
        """Whether this section earns a triangle."""
        return section_is_collapsible(self.entries)

    @property
    def indicator(self) -> str:
        """The triangle for the current state, or "" when there is none."""
        if not self.collapsible:
            return ""
        return TRIANGLE_COLLAPSED if self.collapsed else TRIANGLE_EXPANDED

    @property
    def heading(self) -> str:
        """The section's heading, with the triangle when it has one."""
        mark = f"{self.indicator} " if self.indicator else ""
        return f"{mark}{self.title}"


# ---------------------------------------------------------------------------
# The sidebar footer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SidebarFooter:
    """The sidebar's footer: the directory as parent + leaf, and the version."""

    parent: str
    leaf: str
    version: str

    @property
    def rendered(self) -> bool:
        """Whether the footer has anything true to say."""
        return bool(self.leaf or self.version)

    @property
    def lines(self) -> Tuple[str, ...]:
        """The footer's plain lines, parent first."""
        out: list[str] = []
        if self.leaf:
            out.append(self.leaf)
        if self.parent:
            out.append(self.parent)
        if self.version:
            out.append(f"neo {self.version}")
        return tuple(out)


def sidebar_footer(path: str | os.PathLike[str], version: str = "") -> SidebarFooter:
    """Build the sidebar footer from a directory path and a version.

    A footer that printed the whole absolute path would be one long
    unbreakable token at 42 columns, so the path is split into the parent
    and the leaf and the LEAF is the first line: the one fact a reader
    needs ("which repo am I in") is the one they see first.
    """
    try:
        resolved = Path(str(path or ""))
    except Exception:
        resolved = Path(".")
    raw = str(path or "").strip()
    if not raw or raw in (".", "./"):
        return SidebarFooter(parent="", leaf="", version=str(version or ""))
    leaf = resolved.name or str(resolved)
    parent = str(resolved.parent) if leaf else ""
    if parent in (".", ""):
        parent = ""
    return SidebarFooter(parent=parent, leaf=leaf, version=str(version or ""))


# ---------------------------------------------------------------------------
# Persisted UI preferences (sidebar mode, collapsed sections, density)
# ---------------------------------------------------------------------------


@dataclass
class UiPreferences:
    """The shell preferences that survive a restart.

    Deliberately a TUI concern and NOT a `Task.config` key: a value in
    `harness/config.DEFAULTS` is merged into every task and every eval arm,
    so "which panes does this person want open" is not a value that belongs
    in a table merged into every run. It is also not a `Task` knob at all.
    """

    sidebar_mode: str = DEFAULT_SIDEBAR_MODE
    density: str = DEFAULT_DENSITY
    collapsed: frozenset = field(default_factory=frozenset)
    source: str = "default"

    def is_collapsed(self, key: str) -> bool:
        """Whether a sidebar section is collapsed."""
        return str(key) in self.collapsed

    def with_sidebar_mode(self, mode: str) -> "UiPreferences":
        """Return a copy with the sidebar mode resolved and validated."""
        resolved = str(mode or "").strip().lower()
        if resolved not in SIDEBAR_MODES:
            resolved = DEFAULT_SIDEBAR_MODE
        return UiPreferences(
            sidebar_mode=resolved,
            density=self.density,
            collapsed=self.collapsed,
            source=self.source,
        )

    def with_density(self, density: str) -> "UiPreferences":
        """Return a copy with the density resolved and validated."""
        resolved = str(density or "").strip().lower()
        if resolved not in DENSITIES:
            resolved = DEFAULT_DENSITY
        return UiPreferences(
            sidebar_mode=self.sidebar_mode,
            density=resolved,
            collapsed=self.collapsed,
            source=self.source,
        )

    def with_collapsed(self, collapsed: Iterable[str]) -> "UiPreferences":
        """Return a copy whose collapsed set is exactly `collapsed`."""
        declared = set(SIDEBAR_SECTIONS)
        kept = frozenset(str(key) for key in collapsed if str(key) in declared)
        return UiPreferences(
            sidebar_mode=self.sidebar_mode,
            density=self.density,
            collapsed=kept,
            source=self.source,
        )

    def toggled(self, key: str) -> "UiPreferences":
        """Return a copy with one section's collapsed state flipped."""
        name = str(key)
        if name not in SIDEBAR_SECTIONS:
            return self
        current = set(self.collapsed)
        if name in current:
            current.discard(name)
        else:
            current.add(name)
        return self.with_collapsed(current)

    def next_sidebar_mode(self) -> "UiPreferences":
        """Return a copy one step along the auto -> show -> hide cycle."""
        order = ("auto", "show", "hide")
        try:
            index = order.index(self.sidebar_mode)
        except ValueError:
            index = 0
        return self.with_sidebar_mode(order[(index + 1) % len(order)])

    def next_density(self) -> "UiPreferences":
        """Return a copy at the other density."""
        other = "compact" if self.density == "comfortable" else "comfortable"
        return self.with_density(other)

    def to_dict(self) -> Dict[str, Any]:
        """The bounded, JSON-safe document this preference writes."""
        return {
            "schema_version": 1,
            "sidebar_mode": self.sidebar_mode,
            "density": self.density,
            "collapsed": sorted(self.collapsed),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "UiPreferences":
        """Build preferences from an untrusted mapping, validating every value.

        Never raises. A preference file is a file a person can edit, so an
        unknown mode, an unknown density, and an unknown section name all
        degrade to the declared default rather than taking the shell down.
        """
        payload = data if isinstance(data, Mapping) else {}
        mode = str(payload.get("sidebar_mode") or "").strip().lower()
        density = str(payload.get("density") or "").strip().lower()
        raw_collapsed = payload.get("collapsed")
        collapsed: Iterable[Any]
        if isinstance(raw_collapsed, (list, tuple, set, frozenset)):
            collapsed = [str(item) for item in raw_collapsed]
        else:
            collapsed = []
        return cls(
            sidebar_mode=mode if mode in SIDEBAR_MODES else DEFAULT_SIDEBAR_MODE,
            density=density if density in DENSITIES else DEFAULT_DENSITY,
            collapsed=frozenset(
                item for item in collapsed if item in set(SIDEBAR_SECTIONS)
            ),
            source="file",
        )


def preferences_path(log_root: str | os.PathLike[str]) -> Path:
    """Where the shell's preferences live for a given artifact root.

    Under the artifact root, not the user's home: a preferences file beside
    a run's journal is discoverable, and the root is already the thing a
    user can point ``--log-root`` at.
    """
    return Path(str(log_root or ".")) / "_ui" / "preferences.json"


def load_preferences(log_root: str | os.PathLike[str]) -> UiPreferences:
    """Load the shell's preferences, or the defaults when unreadable.

    A corrupt preference file is not an error the user has to see: it is a
    layout preference, and the shell's job is to start. The defaults are
    returned with `source="default"` so a caller can tell.
    """
    path = preferences_path(log_root)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return UiPreferences()
    if not isinstance(data, Mapping):
        return UiPreferences()
    return UiPreferences.from_mapping(data)


def save_preferences(
    log_root: str | os.PathLike[str], preferences: UiPreferences
) -> bool:
    """Write the shell's preferences atomically. Returns whether it wrote.

    Atomic because a half-written preferences file is a corrupt one, and a
    corrupt preference file is indistinguishable from a preference the user
    never set. Never raises: a shell that cannot persist a layout
    preference should still lay out.
    """
    path = preferences_path(log_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(preferences.to_dict(), indent=2, sort_keys=True)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".preferences-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# THE AESTHETIC GATE (VEX-PF-09)
# ---------------------------------------------------------------------------
#
# "Premium" is not a taste argument, so this section refuses to make one. It
# declares SIX properties, each with the question it answers, the number it
# is bounded by, and the function that produces that number. Every
# measurement below reads a RENDERED RECEIPT -- the SVG Textual exports from
# the real `NeoApp` -- so a claim about how the shell looks is a claim about
# the bytes a terminal would have drawn, not about the code that drew them.
#
# The measurement code is pure and Textual-free on purpose: it takes an SVG
# string plus a `LayoutSpec` and returns numbers. That is what makes it a
# test rather than a review comment.
# ---------------------------------------------------------------------------

#: The eight states the shell is audited in. Every one of them gets a
#: rendered receipt, because "the shell looks good" is a statement about none
#: of them in particular.
#:
#: They are named after what the READER is looking at, not after the code
#: path that produced them, so a state cannot quietly be renamed to match a
#: regression.
AESTHETIC_STATES: Tuple[str, ...] = (
    "idle",
    "thinking",
    "acting",
    "diff",
    "failure",
    "permission",
    "complete",
    "help",
)


@dataclass(frozen=True)
class AestheticProperty:
    """One property of "premium", with the number that decides it.

    `bound` is the DECLARED limit in words and `measured_by` names the
    function in this module that produces the number it is compared against,
    so a reader can go from a claim to the arithmetic without reading a
    widget.
    """

    name: str
    question: str
    measured_by: str
    bound: str


#: The six. The ORDER is a designer's reading order: structure, then rhythm,
#: then voice, then the two failure modes (too much, too little), then
#: stability.
AESTHETIC_PROPERTIES: Tuple[AestheticProperty, ...] = (
    AestheticProperty(
        "alignment",
        "is there one grid, or are there ragged edges?",
        "alignment_report",
        "every rendered line starts on a declared ink edge",
    ),
    AestheticProperty(
        "rhythm",
        "is the vertical spacing consistent?",
        "rhythm_report",
        "every within-region gap is a multiple of the spacing unit and at "
        "most the declared block gap",
    ),
    AestheticProperty(
        "hierarchy",
        "are there four distinguishable roles?",
        "hierarchy_report",
        "all four type roles are realised on the receipt, and their "
        "silhouettes are pairwise distinct",
    ),
    AestheticProperty(
        "density",
        "is there information without crowding?",
        "density_report",
        "ink coverage at or below the declared ceiling, and the receipt is not vacuous",
    ),
    AestheticProperty(
        "restraint",
        "is there an element that carries no information?",
        "restraint_report",
        "no row is decoration-only and no marker is alone on its row",
    ),
    AestheticProperty(
        "responsiveness",
        "is there a jump on resize?",
        "responsiveness_report",
        "the transcript's left edge never moves and the visible region set "
        "changes only at a declared breakpoint",
    ),
)


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------

#: The shell's horizontal grid step, in terminal columns. One.
#:
#: It is 1 and not something rounder because a terminal has no sub-cell
#: layout: a "12-column grid" in a 100-column terminal is a claim about a
#: canvas this product does not have. What a terminal CAN promise is that
#: every block's left edge is a whole number of columns from the region's
#: own origin, which is what :func:`expected_ink_edges` enumerates.
GRID_COLUMNS = 1

#: The inset a region's own content sits at, in columns. `#neo-header` is
#: `padding: 0 1`, so header content is one column in from the region's
#: edge. The two declared exceptions are the shell's own BORDER -- a
#: full-bleed rule and the active run bar -- which belong at the region's
#: origin rather than at its inset, and a grid that put a border at an inset
#: would leave a column of dead space at the terminal's edge.
INK_INSET_COLUMNS = 1


def expected_ink_edges(spec: "LayoutSpec") -> frozenset:
    """The columns a rendered line is allowed to start at, for one layout.

    Two edges per visible region -- the region's own origin and that origin
    plus the inset -- which is a DERIVATION from the resolved layout rather
    than a list somebody has to remember to update.

    Column 0 is always allowed: it is the terminal's left edge, and ink
    flush against the edge of the screen is not a ragged edge.
    """
    edges = {0}
    for region in spec.regions:
        if not region.visible:
            continue
        edges.add(int(region.x))
        edges.add(int(region.x) + INK_INSET_COLUMNS)
    return frozenset(int(value) for value in edges)


#: The largest vertical gap, in rows, allowed BETWEEN two lines of the same
#: region. One, because `DENSITY_PROFILES[<density>].block_gap` is the
#: shell's own declared separation between two rail blocks, and a rhythm a
#: reader cannot predict is what a ragged layout is.
RHYTHM_MAX_GAP_ROWS = 1

#: The widest horizontal gap between a line's last cell and its region's
#: right edge, in columns, that still counts as "that line filled its
#: region". A line padded out to the right edge is a rule or a frame, not a
#: ragged text edge, and a rhythm that counted those as content would be
#: measuring the frame instead of the type.
TRAILING_RULE_MIN_COLUMNS = 8

#: The states whose transcript carries the SHELL'S OWN CONVERSATION, and the
#: edge it is therefore required to keep in every one of them. The three
#: exclusions are named with their reasons, because "we checked a subset" is
#: the sentence that makes a coverage claim untrue.
#:
#: `help` is excluded because the transcript is a full-width TABLE there --
#: a command column at column 3 is that block's grid, not the conversation's.
#: `idle` is excluded because the transcript is the empty state, three lines
#: of it. `permission` is excluded because a modal covers the shell and the
#: transcript is not what the reader is looking at.
ALIGNMENT_EDGE_RECEIPTS: Tuple[str, ...] = (
    "thinking",
    "acting",
    "diff",
    "failure",
    "complete",
)
ALIGNMENT_EDGE_EXCLUDED: Mapping[str, str] = {
    "help": (
        "the transcript is a full-width table here; its command column is the "
        "table's grid, and requiring it to match the conversation's edge "
        "would be requiring a table to be prose"
    ),
    "idle": "the transcript is the three-line empty state and has no conversation in it",
    "permission": "a modal covers the whole shell, so the transcript is not the surface on screen",
}

#: The measured number of WRAPPED CONTINUATION lines across all eight
#: receipts -- a row that moves LEFT of the row it continues, instead of
#: hanging at or right of its parent's own edge -- and the ceiling this
#: round accepts.
#:
#: This is a real ragged edge, not a taste: a wrapped `ctrl+o density
#: switch between comfortable` / `and compact density` pair puts its
#: continuation one column left of its parent and destroys the table's
#: column. It is measured, not asserted, and it is a DEBT REGISTER rather
#: than a gate because the renderers that produce those lines
#: (`cli.interactive.py::render_help` and `cli/runview.py::failure_lines`)
#: belong to two other terminals.
#:
#: The ceiling is the measured TOTAL across the eight receipts, so a new
#: ragged edge in any state fails while the known ones stay visible and
#: counted. Measured: help 8, complete 3, failure 2, and 0 in the other five
#: states -- 13 in all. See `cli/AGENTS.md` for the per-state breakdown.
WRAPPED_CONTINUATION_CEILING = 13
WRAPPED_CONTINUATION_OWNER = (
    "the owner is cli/interactive.py::render_help and "
    "cli/runview.py::failure_lines, which wrap a row to the region's own "
    "left margin instead of hanging the continuation under the body column. "
    "The fix is to indent the continuation by the block's own body column."
)

#: The maximum share of the viewport that inked cells may occupy.
#:
#: Measured on the eight receipts at 100x30 on this tree: permission 15.67,
#: idle 17.90, acting 30.03, thinking 31.07, complete 37.60, failure 51.93,
#: diff 52.60, help 52.97. The ceiling sits ABOVE the observed maximum with
#: room to grow rather than AT it, because a bound pinned to the current
#: number fails the first honest change and is then raised without being
#: read. 60% is where a 100x30 terminal stops being scannable; a 200x50
#: viewport of the same 60% is 6000 cells and the eye has no entry point.
DENSITY_CEILING_PCT = 60.0

#: A receipt with fewer inked cells than this is VACUOUS rather than sparse:
#: a frame that rendered nothing would pass every other property, which is
#: the "a claim without a receipt is not a claim" failure inverted.
DENSITY_FLOOR_PCT = 1.0

#: The fewest text lines a receipt may carry and still be evidence.
DENSITY_MIN_TEXT_ROWS = 3


# ---------------------------------------------------------------------------
# Hierarchy: which rendered form carries which type role
# ---------------------------------------------------------------------------

#: The four type roles, and the SHAPE of the rendered cell that carries each
#: one, with the reason that shape is this rung of the scale.
#:
#: A terminal has no font size, so a role is realised the way a terminal can
#: realise one: by what the line IS. Each `pattern` is matched against a
#: rendered row's plain text, and each `reason` says why that shape is this
#: rung rather than another. A role whose pattern matches nothing on the
#: receipt is reported `found: False` -- the declaration is not the
#: evidence, and counting it would make the property unfalsifiable.
#:
#: Patterns are matched against the row's ALPHANUMERIC cells joined by a
#: space, never against one cell: the rail publishes a metered row as two
#: runs (`time` at column 59 and `0s` at column 65), so a per-cell pattern
#: would report the micro role missing on every frame that has a meter --
#: which is to say, on every frame that has content.
TYPE_ROLE_EVIDENCE: Mapping[str, Mapping[str, str]] = {
    "display": {
        "pattern": r"^\S+\s+neo\b",
        "reason": (
            "the brand lockup is the only row on the terminal that exists to "
            "be looked at rather than read, which is what the display rung is"
        ),
    },
    "title": {
        "pattern": r"^[▾▸\s]*[A-Z][A-Z0-9 /&+.-]{2,}$",
        "reason": (
            "a block heading is set in capitals at its region's own origin; "
            "capitals are the terminal's only reliable 'this is a heading' "
            "channel, so that is the channel the title rung is measured on"
        ),
    },
    "body": {
        "pattern": r"[a-z]{4,}",
        "reason": (
            "a sentence cell is the body rung; four lowercase letters is the "
            "shortest thing that is a word, and a cell with no word in it is "
            "not body copy"
        ),
    },
    "micro": {
        "pattern": r"^[a-z][a-z ]{0,11} [0-9$][0-9a-z$.,:+-]*$",
        "reason": (
            "a metered row -- a lowercase label and a value and nothing else "
            "-- is the micro rung: the shortest thing the shell says, and the "
            "only shape a reader scans instead of reads"
        ),
    },
}

#: How many ADJACENT rendered cells a role's pattern may span. Three, and the
#: reason is that the rail publishes a metered row as two runs (`time` at
#: column 59 and `0s` at column 65) while a block heading is a single run and
#: the brand lockup is two (`◆` and `neo` sit in one cell). A matcher that
#: only ever saw one cell would report the micro role missing on every frame
#: that has a meter, which is to say on every frame that has content; one
#: that saw whole rows would report the title role missing whenever a rail
#: row and a heading share a row, which is most of them.
TYPE_ROLE_RUN_MAX = 3

#: The same bound for a FLOATING surface (a modal). Two, and the reason is a
#: measurement rather than a tolerance: the approval modal declares
#: `padding: 1 2` on its own panel, which puts a blank row above and below
#: its title, so a modal's internal step is two rows while the shell's bands
#: step by one. Measured on this tree: the permission receipt's rows are at
#: 10, 12, 15 and 18 -- gaps of 1, 2 and 2. One number for both surfaces
#: would either fail a correct modal or licence a two-row gap in the shell.
FLOATING_MAX_GAP_ROWS = 2

#: The states whose receipt shows the SHELL's COMPOSITION -- header,
#: transcript, rail, run line and footer all populated at once. These are the
#: six the four-role hierarchy is measured on, and the list is declared with
#: a reason per exclusion because "we checked a subset" is the sentence that
#: makes a coverage claim untrue.
#:
#: `idle` is excluded because it is the state with no run and therefore no
#: rail content: it has no block heading to be a title and no meter to be a
#: micro row, so demanding all four roles of an empty rail would be
#: demanding decoration. `permission` is excluded because a modal covers the
#: whole shell, so its receipt contains the modal and nothing else. Both are
#: still audited on the other five properties -- a receipt that is excused
#: from one property is not excused from the gate.
HIERARCHY_RECEIPTS: Tuple[str, ...] = (
    "thinking",
    "acting",
    "diff",
    "failure",
    "complete",
    "help",
)
HIERARCHY_EXCLUDED: Mapping[str, str] = {
    "idle": (
        "no run is live, so the rail publishes no block heading and no meter. "
        "This is the anti-clutter rule WORKING, not a gap: a rail with two "
        "entries does not render, and a receipt that had to carry a heading to "
        "satisfy the type scale would be a receipt that added decoration to "
        "pass a test."
    ),
    "permission": (
        "a modal is over the whole shell, so the receipt is the modal. A "
        "permission question is an interruption rather than a composition, "
        "and it is audited on alignment, rhythm, density, restraint and colour."
    ),
}


# ---------------------------------------------------------------------------
# Restraint: what the shell may draw without carrying information
# ---------------------------------------------------------------------------

#: Rows that carry no INFORMATION but are not decoration, each with the
#: reason it is load-bearing. A declared list rather than silence, for two
#: reasons: a rule with named exceptions can be argued with, and an unstated
#: one is a hole.
DECORATION_EXEMPT: Mapping[str, str] = {
    "horizontal_rule": (
        "a full-width rule is the transcript's floor; without it the "
        "conversation has no edge and the composer reads as part of the "
        "scrollback. It is the shell's only separator and it spans the "
        "region it separates."
    ),
    "card_frame": (
        "the summary card's top and bottom rules bound the card. A card "
        "without them is a paragraph, and a paragraph cannot be scanned as a "
        "unit -- which is the whole reason the card exists."
    ),
    "modal_border": (
        "a modal's left and right rules are what make it a modal: they are "
        "the visible edge of a surface that is not the shell. Without them a "
        "permission question is an unlocated line of text."
    ),
    "region_divider": (
        "the rail's right-hand rule is the boundary between the conversation "
        "and the evidence. It is one column wide and it is the only thing "
        "telling the reader which side of it they are on."
    ),
}

#: A row matching one of these is a FRAME, not decoration. Matched against
#: the row's plain text with its own border glyphs intact, so a rule reads as
#: a rule and a sentence does not. A frame row that shares its line with a
#: NEIGHBOURING region's divider (`╰─────╯│`) is the same frame, because in a
#: two-column shell a card's bottom rule and the rail's right-hand rule land
#: on the same terminal row and are drawn as one run.
FRAME_PATTERNS: Tuple[str, ...] = (
    r"^[─━═-]+$",
    r"^[╭╰╔╚╒╘][─━═]*[╮╯╕╛]?[│┃║]?$",
    r"^[╭╰╔╚╒╘][─━═]*[│┃║][─━═]*[╮╯╕╛]?$",
    r"^│.*│$",
    r"^[│┃║]( ?[│┃║])*$",
)

#: Glyphs that are MOTION. A motion glyph carries information -- the thing is
#: still happening -- only when something on its row says WHAT, so
#: `restraint_report` requires a word beside it. This is the "a spinner
#: conveying nothing is noise" rule as a measurement rather than a taste.
MOTION_GLYPHS: Tuple[str, ...] = (
    "⠋",
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
    "◐",
    "◓",
    "◑",
    "◒",
)


# ---------------------------------------------------------------------------
# Duplicate information
# ---------------------------------------------------------------------------

#: The surfaces whose job is a run's LIVE state. Two of these publishing the
#: same fact is a duplicate: the reader cannot tell which one to believe, and
#: the second one costs a row to do it.
LIVE_SURFACES: Tuple[str, ...] = (
    "header",
    "statusline",
    "sidebar",
    "context",
    "runline",
)

#: The surfaces that are NOT live state, and therefore do not compete with
#: the live surfaces. Each is named with the reason, because "the check only
#: looks at five regions" is exactly the kind of scope limit a reader has to
#: be told about.
NON_LIVE_SURFACES: Mapping[str, str] = {
    "transcript": (
        "the transcript is the run's HISTORY. A fact being in the scrollback "
        "AND on a rail is what history and a rail are each for; a rule that "
        "flagged it would have forbidden the completion card and the run "
        "line's own outcome line, both of which are the product's promise."
    ),
    "composer": "the composer holds the user's own uncommitted text; it is an input, not a readout.",
    "footer": (
        "the hint bar is a KEY LEGEND. Its words are commands and key names, "
        "and the same word meaning a command in two places is a legend, not a "
        "second statement of a fact."
    ),
}

#: Pairs of LIVE surfaces that are checked against each other, and the reason
#: the pairs are chosen rather than all of them.
#:
#: `header` is NOT paired with `sidebar`. The header is a strip of reserved
#: identity chips and the rail is the run's evidence; they overlap only on the
#: run's identity, and the header's own column budget (Terminal-02's
#: `HEADER_BUDGET`) exists to keep that identity visible at every width
#: precisely because the rail can be collapsed. Pairing them would report a
#: designed overlap as a defect on every frame, and a gate that fires on
#: every frame is a gate nobody reads.
DUPLICATE_PAIRS: Tuple[Tuple[str, str], str] = (
    ("sidebar", "runline", "both publish the run's meters at the same moment"),
    ("sidebar", "statusline", "both publish live counts and modes"),
    (
        "header",
        "statusline",
        "the header's status chip and the statusline both name the run's state",
    ),
    ("context", "sidebar", "the two rails are read as one column of evidence"),
    ("context", "runline", "the context rail's meters against the live ticker"),
)

#: Facts two live surfaces both publish TODAY, each with the reason and the
#: owner of the fix. This is a DEBT REGISTER, not a design: a pair named
#: here is a pair a reader can see twice on one frame, and each entry says
#: whose file has to change.
#:
#: There are SEVEN rows with effectively ONE cause -- the run line re-states
#: what the rail already states -- split by fact because the gate keys on the
#: fact. `cost`, `duration` and `unknown` are the run's METERS; `tool`,
#: `result` and `received` are its last ACTION; `agent-1` is its IDENTITY and
#: `thinking` is its PHASE.
#:
#: A stale entry (a fact that is no longer published twice) is itself reported
#: by `duplicate_rollup`, because a debt table that keeps entries nobody
#: fixed stops being a register and starts being a hiding place. That check
#: earned its place on its first full run: a row for the PRICED cost case
#: (`cost $0.0012` beside `$0.0012`) was declared, all eight receipts are
#: unpriced runs, and the row was reported stale and deleted rather than kept
#: as a place to put a defect nobody is looking at.
DUPLICATE_EXEMPT: Mapping[Tuple[str, str, str], str] = {
    ("sidebar", "runline", "cost"): (
        "the run's cost. The rail publishes `cost unknown` / `cost $0.00` "
        "in its METERS block and the run line publishes the same unpriced "
        "call as `unknown cost`. Owner: cli/tui.py::_render_side, the rail's "
        "status block. The RUN LINE's copy is load-bearing -- "
        "evals/daily_driver.py:2483 requires the cost string on the run line "
        "and tests/test_tui_contract.py asserts it -- so the RAIL's copy is "
        "the one to remove."
    ),
    ("sidebar", "runline", "agent-1"): (
        "the run's IDENTITY. The rail's plan block is headed `PLAN agent-1` "
        "and the run line's own text carries the same id. Owner: "
        "cli/tui.py::_render_side together with the run-line composition in "
        "cli/tui_components.py::LoadingState -- the header's task chip is the "
        "reserved, always-visible identity, so one of the other two is the "
        "one to go."
    ),
    ("sidebar", "runline", "thinking"): (
        "the run's PHASE. The rail's projection block publishes "
        "`action thinking` and the run line's own label is `thinking`. The "
        "run line is the load-bearing copy -- it is the surface that updates "
        "in place during a run, which is this product's central promise -- so "
        "the RAIL's copy is the one to shorten to the verb. Owner: "
        "cli/tui.py::_render_side."
    ),
    ("sidebar", "runline", "duration"): (
        "elapsed time. `time 0s` on the rail, `0s` on the run line. Owner: "
        "cli/tui.py::_render_side -- the same owner and the same fix as the "
        "cost row, because they are one METERS block."
    ),
    ("sidebar", "runline", "tool"): (
        "the run's CURRENT ACTION. The rail's projection block publishes "
        "`action tool result received` and the run line publishes the same "
        "words. This is the most load-bearing duplicate of the seven, because "
        "a reader who sees it twice has to decide which one is now. Owner: "
        "cli/tui.py::_render_side -- the projection block is the authority "
        "and the run line's echo is the one to shorten to the verb."
    ),
    ("sidebar", "runline", "result"): (
        "part of the same `tool result received` echo as the `tool` row "
        "above. Owner: cli/tui.py::_render_side."
    ),
    ("sidebar", "runline", "received"): (
        "part of the same `tool result received` echo as the `tool` row "
        "above. Owner: cli/tui.py::_render_side."
    ),
    ("sidebar", "runline", "unknown"): (
        "the honest 'we cannot price this' value, published by the rail's "
        "`calls unknown` / `tokens unknown` / `cost unknown` and by the run "
        "line's `unknown cost`. Owner: cli/tui.py::_render_side -- the same "
        "owner and fix as the cost row."
    ),
}


# ---------------------------------------------------------------------------
# Responsiveness
# ---------------------------------------------------------------------------

#: The widths at which the set of visible regions may change. Every one is a
#: declared breakpoint from this module, which is what makes a resize
#: "responsive" a claim rather than a hope: a region that appears or vanishes
#: anywhere else is a jump.
RESPONSIVE_BREAKPOINTS: Tuple[int, ...] = tuple(
    sorted(
        {
            SPLIT_MIN_COLUMNS,
            PLAN_MIN_COLUMNS,
            CONTEXT_MIN_COLUMNS,
            SIDEBAR_BREAKPOINT,
            ULTRAWIDE_COLUMNS,
        }
    )
)

#: The largest change, in columns of region WIDTH per column of TERMINAL
#: width, allowed between two widths except at a declared breakpoint. One.
#:
#: This is the whole responsiveness claim, and it is stronger than a
#: position check: the shell's geometry is a PIECEWISE-LINEAR function of
#: the terminal width, with its knots at the widths it declared in advance.
#: A region therefore never grows faster than the terminal (no band outruns
#: the space it was given) and never loses a column off a breakpoint (no
#: band shrinks for an undeclared reason).
#:
#: The bound is a SLOPE, not a step, and the reason is the sweep: a run that
#: resizes through 101, 119, 120, 121, 159, 160, 161 and 200 has an
#: eighteen-column step between two of its samples, and a transcript that
#: grows eighteen columns across it is behaving exactly as declared. Two
#: adjacent samples are not two adjacent widths of a terminal.
#:
#: Positions are deliberately not gated here, and the reason is in the data:
#: a trailing rail's left edge travels as the window widens, and a rail
#: pinned between the transcript and a context rail moves because the
#: transcript widened. Both are the same fact this clause already measures.
#: The one POSITION that is pinned is the transcript's, and the suite
#: asserts it directly at every width in the sweep.
RESPONSIVE_MAX_WIDTH_SLOPE = 1

#: The largest number of columns a region's ANCHORED edge may move between
#: two adjacent widths, except at a declared breakpoint. Zero.
#:
#: The edge, not the rectangle: a full-width region's right edge must be the
#: terminal's right edge and a leading-anchored region's left edge must be
#: column 0. A bar that stops short of the terminal and a conversation that
#: leaves the left margin are the two jumps a reader notices first.
RESPONSIVE_MAX_EDGE_MOVE_COLUMNS = 0


def region_anchor(spec: "LayoutSpec", name: str) -> str:
    """Which edge a region is anchored to.

    One of `full` (it spans the terminal and is pinned at both edges),
    `leading` (pinned to column 0), `trailing` (pinned to the terminal's
    right edge), or `flow`.

    DERIVED from the resolved layout, not declared per region, so adding a
    region cannot make the answer stale. `flow` is the honest fourth answer
    and it matters: a plan rail pinned between the transcript and a context
    rail touches NEITHER edge, and reporting it as `full` would then demand
    that it reach the terminal's right edge -- which is how this function's
    first version came to demand that of a 42-column rail at width 120.
    """
    region = spec.region(name)
    if region.x <= 0 and region.right >= spec.width:
        return "full"
    if region.x <= 0:
        return "leading"
    if region.right >= spec.width:
        return "trailing"
    return "flow"


def region_edge(spec: "LayoutSpec", name: str) -> int:
    """The column this region's anchor is pinned to, for one layout.

    `leading` reports the left edge, `trailing` reports one past the right
    edge, and `full` reports the left edge (which is pinned at 0 for every
    full-width region, so the check is the same either way).
    """
    region = spec.region(name)
    anchor = region_anchor(spec, name)
    if anchor == "trailing":
        return int(region.right)
    return int(region.x)


# ---------------------------------------------------------------------------
# Colour: the token table, and the two kinds of thing that is not one
# ---------------------------------------------------------------------------

#: Colours drawn by Textual's OWN screenshot exporter rather than by the
#: application, keyed to what each one is. `App.export_screenshot()` wraps the
#: frame in a fake OS window (a title bar and three traffic lights) before it
#: serialises anything, so these five hexes appear in every receipt and are
#: not the product's palette.
#:
#: They are separated from `UNTOKENED_RECEIPT_EXEMPT` deliberately. This map
#: is "the exporter drew it"; the other is "the product drew something that
#: is not a token", and conflating the two would let a real product colour
#: hide behind an exporter excuse.
SCREENSHOT_CHROME: Mapping[str, str] = {
    "#292929": "the window frame drawn by textual's export_screenshot()",
    "#C5C8C6": "the window title text drawn by textual's export_screenshot()",
    "#FF5F57": "the close traffic light drawn by textual's export_screenshot()",
    "#FEBC2E": "the minimise traffic light drawn by textual's export_screenshot()",
    "#28C840": "the zoom traffic light drawn by textual's export_screenshot()",
}

#: Colours that appear in a rendered receipt and are NOT in the token table
#: and NOT exporter chrome. Each entry names the owner and the one-line fix.
#:
#: Measured: exactly one, and only in the `permission` receipt, where the
#: prompt box paints `background: $surface` -- a Textual DESIGN variable
#: rather than a Neo token -- and resolves to Textual's own dark default
#: `#151515`. It is on screen only while a modal is up, which is precisely
#: why a source scan for hex LITERALS could never have found it.
UNTOKENED_RECEIPT_EXEMPT: Mapping[str, str] = {
    "#151515": (
        "the approval modal's panel background is Textual's default "
        "`$surface`, not a Neo surface token. Owner: cli/tui.py "
        "(`_PromptScreen.CSS`, `background: $surface`). Fix: "
        "`background: $neo-panel`. It is declared here rather than left as a "
        "failure because the required suite must pass on a tree this terminal "
        "does not own; the number is reported so it cannot grow unnoticed."
    ),
}

#: Source locations permitted to hold a hex literal, keyed to
#: ``"file.py:LINE"``, each with the reason. The locked wordmark gradient in
#: `cli/ui.py` is the one historical exemption; the two shipped drivers under
#: `logs/` are excluded from the source scan entirely because they are
#: evidence, not product.
SOURCE_TOKEN_EXEMPT: Mapping[str, str] = {
    "ui.py:859": (
        "`_NEO_RAMP` is the LOCKED wordmark gradient. Its interpolation stops "
        "are brand, not chrome, and they are interpolated into the logo "
        "raster at import time."
    ),
}

#: A NEUTRAL token -- a surface, a border, or body text -- may not exceed
#: this saturation. A warm grey is a low-saturation colour with a warm hue,
#: and 0.05 is far below every one in the shipped palette: the neutrals are
#: all exactly 0.0. The ceiling is 0.05 rather than 0.0 because a theme a
#: user overrides by hand should be able to land on a very slightly tinted
#: grey without tripping a gate; a visibly warm one still fails.
NEUTRAL_MAX_SATURATION = 0.05

#: The token names that are allowed to carry a hue. Everything else must be
#: neutral, which is the machine-checkable form of "no warm greys, no
#: orange": an orange surface or an orange border cannot be expressed by
#: this palette at all.
HUE_BEARING_TOKENS: Tuple[str, ...] = (
    "accent_primary",
    "accent_text",
    "accent_glow",
    "success",
    "warning",
    "error",
    "info",
    "selection_bg",
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

#: Hue band, in turns of the colour wheel, that counts as ORANGE/AMBER. The
#: design's own words: semantic green, red and amber are reserved for
#: OUTCOMES and approval warnings, and the crimson accent is the only brand
#: hue. A token in this band that is not an outcome token is the exact thing
#: the prompt bans, and the band is a range rather than a name because a
#: single hue number would be trivially evaded by one degree.
ORANGE_HUE_BAND: Tuple[float, float] = (0.03, 0.14)

#: The outcome tokens permitted inside `ORANGE_HUE_BAND`. Amber IS the
#: declared warning/approval colour, and the 16-colour fallback for the
#: accent is a red rather than a hue, so nothing else belongs here.
ORANGE_EXEMPT_TOKENS: Tuple[str, ...] = ("warning", "approval")


# ---------------------------------------------------------------------------
# Measuring a rendered receipt
# ---------------------------------------------------------------------------

_SVG_TEXT_RE = re.compile(
    r'<text[^>]*\bx="(-?[0-9.]+)"[^>]*\by="(-?[0-9.]+)"'
    r'[^>]*\btextLength="([0-9.]+)"[^>]*>(.*?)</text>',
    re.S,
)
_SVG_FILL_RE = re.compile(r'fill="(#[0-9a-fA-F]{3,8})"')
_SVG_VIEWBOX_RE = re.compile(r'viewBox="0 0 ([0-9.]+) ([0-9.]+)"')
_SVG_SHORT_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3}$")
_WORD_RE = re.compile(r"[A-Za-z0-9_./$:@+-]{2,}")
_MONEY_RE = re.compile(r"^\$")
_DURATION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h)$")
_COUNT_RE = re.compile(r"^[0-9][0-9,]*$")


@dataclass(frozen=True)
class Cell:
    """One text run in a rendered receipt, in TERMINAL coordinates.

    `column` and `row` are derived from the SVG's own pixel geometry, not
    from the caller's guesses: the cell width is the modal
    `textLength / len(text)` over every run, which is the exporter's own
    arithmetic. A receipt whose text is a monospaced grid therefore reads
    back as exactly the columns a terminal would have drawn.
    """

    column: int
    row: int
    text: str
    span: int
    color: str = ""


@dataclass(frozen=True)
class RenderedFrame:
    """Everything a receipt is measured against, extracted once."""

    width: int
    height: int
    cell_width: float
    cell_height: float
    cells: Tuple[Cell, ...]
    colors: Tuple[str, ...]

    @property
    def rows(self) -> Mapping[int, Tuple[Cell, ...]]:
        """The receipt's cells grouped by terminal row, left to right."""
        grouped: Dict[int, List[Cell]] = {}
        for cell in self.cells:
            grouped.setdefault(cell.row, []).append(cell)
        return {
            row: tuple(sorted(items, key=lambda item: (item.column, item.text)))
            for row, items in sorted(grouped.items())
        }

    def row_text(self, row: int) -> str:
        """The row's plain text, cells joined in column order."""
        return "".join(cell.text for cell in self.rows.get(row, ()))

    @property
    def text_rows(self) -> Tuple[int, ...]:
        """The rows carrying at least one alphanumeric character.

        A row made only of box-drawing characters is a FRAME, and measuring
        a rhythm against frames would measure the card's border instead of
        its type.
        """
        return tuple(
            row
            for row, cells in self.rows.items()
            if any(ch.isalnum() for cell in cells for ch in cell.text)
        )

    def left_edges(self) -> Tuple[int, ...]:
        """The first occupied column of every row, ascending."""
        return tuple(min(cell.column for cell in cells) for cells in self.rows.values())


def _normalize_hex(value: str) -> str:
    """Upper-case a hex colour and expand the 3-digit form to 6 digits."""
    text = str(value or "").strip()
    if not text.startswith("#"):
        text = f"#{text}"
    if _SVG_SHORT_HEX_RE.match(text):
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text.upper()


def _resolved_palettes() -> Dict[str, Dict[str, str]]:
    """Every palette the token system can actually resolve, by name.

    Read through the PUBLIC surface -- `theme.theme_names()` crossed with
    `theme.ColorDepth` and `theme.resolve_theme` -- rather than by naming the
    token module's private tables, which `tests/test_cli_theme.py::
    test_the_token_module_is_the_only_palette` forbids on purpose: the
    palette must be findable in one file, and a second module reaching into
    it is a second place to look.

    It is also BETTER coverage than the private tables: crossing every
    shipped theme with every capability depth is twelve resolved palettes,
    and the 256/16 fallbacks only exist as resolved output anyway.
    """
    from cli import theme as _theme

    out: Dict[str, Dict[str, str]] = {}
    for name in _theme.theme_names():
        for depth in _theme.ColorDepth:
            tokens = _theme.resolve_theme(name, depth=depth)
            out[f"{name}/{depth.value}"] = dict(tokens.colors)
    return out


def token_hexes() -> frozenset:
    """Every hex value the whole token system can resolve to, upper-cased.

    Read through the public resolution surface rather than restated, so a new
    theme or a new capability depth is covered the day it is added and a
    receipt colour that came from a token is never reported as untokenised.
    """
    values: set = set()
    for palette in _resolved_palettes().values():
        for value in palette.values():
            if isinstance(value, str) and value.strip():
                values.add(_normalize_hex(value))
    return frozenset(values)


def parse_svg(svg: str, *, width: int = 0, height: int = 0) -> RenderedFrame:
    """Extract a measured grid from an exported screenshot.

    The cell width is MEASURED, not assumed: every run's
    `textLength / len(text)` is the exporter's own column arithmetic, and the
    modal value is the grid the frame was drawn on. The row height is the
    modal difference between distinct `y` values, which is the same number
    for a monospaced terminal and a proportional one.

    A frame with no text runs raises `ValueError` rather than reporting zero
    cells: a receipt that rendered nothing is not a measurement.
    """
    raw = str(svg or "")
    raw_runs = list(_SVG_TEXT_RE.finditer(raw))
    if not raw_runs:
        raise ValueError("the receipt carries no text runs: it is not evidence")
    widths = [
        round(float(match.group(3)) / max(1, len(html.unescape(match.group(4)))), 4)
        for match in raw_runs
    ]
    cell_width = max(set(widths), key=widths.count)
    ys = sorted({round(float(match.group(2)), 2) for match in raw_runs})
    steps = [round(b - a, 2) for a, b in itertools.pairwise(ys) if round(b - a, 2) > 0]
    cell_height = (
        max(set(steps), key=steps.count) if steps else float(round(cell_width * 2, 2))
    )
    first_y = ys[0]
    fills = [_normalize_hex(value) for value in _SVG_FILL_RE.findall(raw)]
    cells: List[Cell] = []
    for match in raw_runs:
        text = html.unescape(match.group(4)).replace("\u00a0", " ")
        if not text or text == "\n":
            continue
        cells.append(
            Cell(
                column=round(float(match.group(1)) / cell_width),
                row=round((float(match.group(2)) - first_y) / cell_height),
                text=text,
                span=len(text),
            )
        )
    view = _SVG_VIEWBOX_RE.search(raw)
    if width <= 0:
        width = round((float(view.group(1)) if view else cell_width) / cell_width)
    if height <= 0:
        height = len(ys)
    return RenderedFrame(
        width=max(1, int(width)),
        height=max(1, int(height)),
        cell_width=cell_width,
        cell_height=cell_height,
        cells=tuple(cells),
        colors=tuple(sorted(set(fills))),
    )


# ---------------------------------------------------------------------------
# Region attribution
# ---------------------------------------------------------------------------

#: The region a floating surface is attributed to when no declared rectangle
#: contains it. A modal is over the shell, not in it, and attributing its
#: rows to whichever band it happened to land on would both invent a region
#: and hand the modal the shell's rhythm bound.
FLOATING_REGION = "floating"


def attribute_row(row: int, spec: "LayoutSpec", left: int = -1, right: int = -1) -> str:
    """Which declared region owns terminal row `row` of this layout.

    `left` and `right` are the row's own occupied columns. A row whose ink
    CROSSES two regions belongs to neither of them: that is a modal, which is
    over the shell rather than inside it, and attributing its rows to the
    transcript would measure a permission question against the shell's
    one-row rhythm and fail a correct modal.

    A row with no `left`/`right` (a caller that only knows the row) falls
    back to the vertical band alone, which is right for a region query and
    wrong for a modal -- so the gate always passes the columns.
    """
    for region in spec.regions:
        if not region.visible or not (region.y <= row < region.bottom):
            continue
        if (
            left >= 0
            and right >= 0
            and not (region.x <= left and right <= region.right)
        ):
            return FLOATING_REGION
        return region.name
    if left >= 0 and right >= 0:
        return FLOATING_REGION
    transcript = spec.region("transcript")
    composer = spec.region("composer")
    if row < transcript.y:
        return "header"
    if transcript.bottom <= row < composer.y:
        return "runline"
    return FLOATING_REGION


def row_spans(frame: RenderedFrame, row: int) -> Tuple[int, int]:
    """The first and one-past-last occupied columns of one rendered row."""
    cells = frame.rows.get(row, ())
    if not cells:
        return (-1, -1)
    return (
        min(cell.column for cell in cells),
        max(cell.column + cell.span for cell in cells),
    )


def nested_insets(
    frame: RenderedFrame, spec: "LayoutSpec"
) -> Mapping[str, Tuple[int, ...]]:
    """The nested-block inset columns each region used, in ascending order.

    A nested block is a sub-panel drawn inside a region: the completion and
    failure cards, the help table. Its content sits in from the region's own
    inset, and this returns the columns it used so the caller can check they
    are inside the declared band and that the whole shell does not use more
    than `NESTED_EDGE_BUDGET` of them.

    Region membership is per CELL, not per row. A row in a two-column shell
    carries the transcript's text and the rail's at the same time, so a
    per-row owner would hand every content row to whichever region came
    first in the declaration order and the measurement would be of the
    declaration rather than of the frame.
    """
    out: Dict[str, set] = {}
    for row in frame.text_rows:
        # Only the row's FIRST CONTENT cell can start a nested block. A run
        # that starts further along the same line is a later word of the same
        # line, and counting it would report the header's five chips as five
        # nested blocks. A lone border glyph is a block's own edge rather than
        # its content, so the composer's right-hand rule -- 98 columns in --
        # is not a block indented by 98 either.
        for cell in frame.rows[row]:
            if not any(ch.isalnum() for ch in cell.text):
                continue
            owner = cell_region(cell.column, spec, row)
            if owner == FLOATING_REGION:
                continue
            inset = cell.column - spec.region(owner).x
            if inset > INK_INSET_COLUMNS:
                out.setdefault(owner, set()).add(inset)
            break
    return {name: tuple(sorted(values)) for name, values in out.items()}


def cell_region(column: int, spec: "LayoutSpec", row: int) -> str:
    """Which declared region owns the CELL at this column and row.

    The SMALLEST rectangle that contains the cell wins, not the first one in
    declaration order. The authority deliberately under-promises and lets the
    transcript's declared rectangle cover the run line and the announcement
    band (`CHROME` counts them unconditionally so a rail can never be handed
    rows the composer needs), which means a specific one-row band and the
    general container both contain the run line's cells. Taking the first
    match attributed the live run line to the TRANSCRIPT, and the rhythm
    check then read the gap between the transcript's last line and the run
    line as an eighteen-row break. A specific band beating a general
    container is the same rule the compositor uses when it stacks them.

    A cell that no rectangle contains -- a modal, which floats over the whole
    shell -- is `FLOATING_REGION`, which is the honest answer rather than a
    guess at the nearest band.
    """
    best: Optional[str] = None
    best_area = 0
    for region in spec.regions:
        if not region.visible:
            continue
        if region.y <= row < region.bottom and region.x <= column < region.right:
            area = max(1, region.width) * max(1, region.height)
            if best is None or area < best_area:
                best, best_area = region.name, area
    return best if best is not None else FLOATING_REGION


# ---------------------------------------------------------------------------
# The six reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlignmentReport:
    """One shared grid: the receipt's line edges against the declared ones."""

    observed_edges: Tuple[int, ...]
    expected_edges: Tuple[int, ...]
    intruders: Tuple[Tuple[int, str], ...]
    continuations: int
    nested: Mapping[str, Tuple[int, ...]]
    primary_edges: Mapping[str, int]

    @property
    def ok(self) -> bool:
        """No gutter intrusion and no new wrapped-continuation ragged edge."""
        return not self.intruders and self.continuations <= WRAPPED_CONTINUATION_CEILING

    def as_dict(self) -> Dict[str, Any]:
        """The receipt a test asserts on."""
        return {
            "observed_edges": list(self.observed_edges),
            "expected_edges": list(self.expected_edges),
            "gutter_intruders": [list(item) for item in self.intruders],
            "wrapped_continuations": self.continuations,
            "continuation_ceiling": WRAPPED_CONTINUATION_CEILING,
            "nested_insets": {
                name: list(values) for name, values in sorted(self.nested.items())
            },
            "primary_edges": dict(sorted(self.primary_edges.items())),
            "ok": self.ok,
        }


def alignment_report(frame: RenderedFrame, spec: "LayoutSpec") -> AlignmentReport:
    """Measure ALIGNMENT: one grid, no ragged edges.

    Three clauses, each a design property rather than a number fitted to the
    pixels, and all three read from the rendered frame:

    1. **Nothing intrudes into the gutter.** A line whose left edge is
       BEFORE its own region's origin is a block in the gutter, which is the
       one place a terminal has that a document does not and the fastest way
       to make a layout look accidental.
    2. **A wrapped continuation does not jump back left.** A row that moves
       LEFT of the row it continues returns to the block's margin instead of
       hanging under its body column -- the exact ragged edge this property
       is about. The number is bounded at the measured total
       (`WRAPPED_CONTINUATION_CEILING`) so a NEW one fails while the
       thirteen known ones stay visible and owned.

    `primary_edges` is the cross-state half and the strongest clause: the
    left edge each region uses MOST often. A transcript whose content sits at
    column 1 in one state and column 2 in another has two grids, and no
    per-state check can see that -- only comparing the receipts can. The
    suite requires those edges to be identical across
    `ALIGNMENT_EDGE_RECEIPTS`.

    `nested` is the INVENTORY of a region's nested-block insets. It is
    reported rather than bounded: a sub-panel's internal table is its own
    renderer's structure, and a bound on it would be a number fitted to one
    card's pixels while claiming to describe a grid.

    A LINE's left edge is the minimum column of its cells, never a run's
    column: a run that starts at column 27 is the third word of a line whose
    left edge is column 1, and measuring runs would report the header's own
    separator as a ragged edge on every frame.
    """
    nested = nested_insets(frame, spec)
    counts: Dict[str, Dict[int, int]] = {}
    intruders: List[Tuple[int, str]] = []
    edges: List[int] = []
    for row in frame.text_rows:
        left, _right = row_spans(frame, row)
        edges.append(left)
        owner = cell_region(left, spec, row)
        if owner == FLOATING_REGION:
            continue
        origin = spec.region(owner).x
        if left < origin:
            intruders.append((row, frame.row_text(row)[:72]))
        bucket = counts.setdefault(owner, {})
        bucket[left] = bucket.get(left, 0) + 1
    primary = {
        name: max(sorted(bucket), key=lambda column: bucket[column])
        for name, bucket in counts.items()
        if bucket
    }
    continuations = 0
    text_rows = frame.text_rows
    for index in range(1, len(text_rows)):
        previous, current = text_rows[index - 1], text_rows[index]
        previous_left, previous_right = row_spans(frame, previous)
        current_left, _current_right = row_spans(frame, current)
        if current_left >= previous_left:
            continue
        if current_left > previous_right - TRAILING_RULE_MIN_COLUMNS:
            continue
        # Both rows must be the SAME region's. A rail row followed by the
        # composer is a band change, not a wrap, and counting it would
        # inflate the debt register with a number that is not a defect.
        if cell_region(previous_left, spec, previous) != cell_region(
            current_left, spec, current
        ):
            continue
        continuations += 1
    return AlignmentReport(
        observed_edges=tuple(sorted(set(edges))),
        expected_edges=tuple(sorted(expected_ink_edges(spec))),
        intruders=tuple(intruders),
        continuations=continuations,
        nested=nested,
        primary_edges=primary,
    )


@dataclass(frozen=True)
class RhythmReport:
    """Consistent spacing: the receipt's within-region vertical gaps."""

    gaps: Mapping[str, Tuple[int, ...]]
    max_gap: int
    bound: int
    offenders: Tuple[Tuple[str, int], ...]

    @property
    def ok(self) -> bool:
        """Whether every within-region gap is inside its declared bound."""
        return not self.offenders

    def as_dict(self) -> Dict[str, Any]:
        """The receipt a test asserts on."""
        return {
            "gaps": {name: list(values) for name, values in sorted(self.gaps.items())},
            "max_gap_rows": self.max_gap,
            "bound_rows": self.bound,
            "offenders": [list(item) for item in self.offenders],
            "ok": self.ok,
        }


def rhythm_report(
    frame: RenderedFrame, spec: "LayoutSpec", *, floating: bool = False
) -> RhythmReport:
    """Measure RHYTHM: every gap inside a region is the declared step.

    Region membership is per CELL, not per row: a row in a two-column shell
    carries the transcript's text and the rail's at the same time, so a
    per-row owner would hand every content row to whichever region came
    first in the declaration order.

    A region is measured over the rows in which it has at least one cell, so
    the blank tail of a scroll region is never a gap -- the blank rows
    between a short transcript and the composer are that region's padding to
    the bottom of the screen, not a spacing decision, and a rhythm that
    counted them would report the idle shell's own empty space as an
    inconsistency.

    The bound is the density's own block gap, so `compact` -- which declares
    a zero gap between rail blocks -- is measured against zero and
    `comfortable` against one, rather than against one number that is right
    for one of them and wrong for the other.

    `floating` is the caller's statement that this receipt is a modal over
    the whole shell, which a screenshot cannot tell us: the modal's border
    is drawn over the shell and the pixels alone are ambiguous. It is
    passed in rather than inferred so the fact has a name and a place.
    """
    bound = max(spacing(rail_block_gap(spec.density)), RHYTHM_MAX_GAP_ROWS)
    rows_owned: Dict[str, set] = {}
    for row in frame.text_rows:
        if floating:
            rows_owned.setdefault(FLOATING_REGION, set()).add(row)
            continue
        for cell in frame.rows[row]:
            rows_owned.setdefault(cell_region(cell.column, spec, row), set()).add(row)
    gaps: Dict[str, List[int]] = {}
    for name, owned in rows_owned.items():
        ordered = sorted(owned)
        gaps[name] = [
            ordered[index] - ordered[index - 1] - 1 for index in range(1, len(ordered))
        ]
    offenders: List[Tuple[str, int]] = []
    for name, values in sorted(gaps.items()):
        limit = FLOATING_MAX_GAP_ROWS if name == FLOATING_REGION else bound
        for value in values:
            if value < 0 or value > limit:
                offenders.append((name, int(value)))
    largest = max((value for values in gaps.values() for value in values), default=0)
    return RhythmReport(
        gaps={
            name: tuple(int(value) for value in values) for name, values in gaps.items()
        },
        max_gap=int(largest),
        bound=int(bound),
        offenders=tuple(offenders),
    )


@dataclass(frozen=True)
class HierarchyReport:
    """Four distinguishable roles, each proved present on the receipt."""

    roles: Mapping[str, Mapping[str, Any]]
    distinct: bool
    missing: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether all four roles are realised and are distinguishable."""
        return not self.missing and self.distinct

    def as_dict(self) -> Dict[str, Any]:
        """The receipt a test asserts on."""
        return {
            "roles": {name: dict(entry) for name, entry in sorted(self.roles.items())},
            "distinct": self.distinct,
            "missing": list(self.missing),
            "ok": self.ok,
        }


def hierarchy_report(frame: RenderedFrame) -> HierarchyReport:
    """Measure HIERARCHY: the four type roles, proved on the receipt.

    Each role's `pattern` is matched against every rendered row's ALPHANUMERIC
    cells joined by a space -- a row, not a cell, because the rail publishes
    `time 0s` as two runs and a per-cell pattern would report the micro role
    missing on every frame that has a meter. The first row that matches is
    the receipt's evidence for that role, and a role whose pattern matches
    nothing is `found: False` and lands in `missing`: the declaration in
    `TYPE_ROLE_EVIDENCE` is not the evidence, and counting it would make the
    property unfalsifiable.

    `distinct` is a derivation, not a claim: it walks the four roles' own
    `weight`/`hue`/`max_columns` from `TYPE_SCALE` and requires the
    silhouettes to be pairwise different on at least one channel AND the
    column budgets to strictly decrease down the scale. Four rungs that
    render identically are one rung with three names.
    """
    found: Dict[str, Dict[str, Any]] = {}
    rows = frame.rows
    for role in TYPE_SCALE_ORDER:
        spec_entry = TYPE_ROLE_EVIDENCE.get(role) or {}
        pattern = str(spec_entry.get("pattern") or "")
        evidence = ""
        if pattern:
            matcher = re.compile(pattern)
            for _row, cells in rows.items():
                for start in range(len(cells)):
                    for length in range(1, TYPE_ROLE_RUN_MAX + 1):
                        window = cells[start : start + length]
                        if not window or start + length > len(cells):
                            continue
                        content = " ".join(cell.text.strip() for cell in window).strip()
                        if content and matcher.match(content):
                            evidence = content[:72]
                            break
                    if evidence:
                        break
                if evidence:
                    break
        found[role] = {
            "found": bool(evidence),
            "evidence": evidence,
            "channel": "rendered form",
            "reason": str(spec_entry.get("reason") or ""),
        }
    silhouettes = {
        role: (
            TYPE_SCALE[role].weight,
            TYPE_SCALE[role].hue,
            TYPE_SCALE[role].max_columns,
        )
        for role in TYPE_SCALE_ORDER
    }
    distinct = len(set(silhouettes.values())) == len(silhouettes)
    # A terminal has TWO realisable channels -- weight and hue -- plus a
    # column budget, so four rungs that share the first two are separated by
    # the third alone. Three roles sharing both would be three names for one
    # rung, and that is the bound rather than a taste.
    weight_hue = [
        (TYPE_SCALE[role].weight, TYPE_SCALE[role].hue) for role in TYPE_SCALE_ORDER
    ]
    shared = sum(1 for pair in weight_hue if weight_hue.count(pair) > 1) // 2
    distinct = distinct and shared <= 1
    return HierarchyReport(
        roles=found,
        distinct=distinct,
        missing=tuple(role for role in TYPE_SCALE_ORDER if not found[role]["found"]),
    )


@dataclass(frozen=True)
class DensityReport:
    """Information without crowding, and not a vacuous receipt."""

    inked_cells: int
    coverage_pct: float
    ceiling_pct: float
    floor_pct: float
    text_rows: int
    overflowing: Tuple[Tuple[int, int], ...]

    @property
    def ok(self) -> bool:
        """Whether the receipt is dense enough to be evidence and not crowded."""
        return (
            self.coverage_pct <= self.ceiling_pct
            and self.coverage_pct >= self.floor_pct
            and self.text_rows >= DENSITY_MIN_TEXT_ROWS
            and not self.overflowing
        )

    def as_dict(self) -> Dict[str, Any]:
        """The receipt a test asserts on."""
        return {
            "inked_cells": self.inked_cells,
            "coverage_pct": self.coverage_pct,
            "ceiling_pct": self.ceiling_pct,
            "floor_pct": self.floor_pct,
            "text_rows": self.text_rows,
            "overflowing": [list(item) for item in self.overflowing],
            "ok": self.ok,
        }


def density_report(frame: RenderedFrame) -> DensityReport:
    """Measure DENSITY on one receipt.

    Coverage is inked cells over the whole viewport, so a receipt that grows
    a second dashboard fails the ceiling and a receipt that renders an empty
    frame fails the floor. Both directions matter: a gate with only a
    ceiling passes a blank screen, which is the vacuous receipt this whole
    section exists to make impossible.
    """
    inked = sum(cell.span for cell in frame.cells)
    total = max(1, frame.width * frame.height)
    overflowing = tuple(
        (cell.row, cell.column + cell.span)
        for cell in frame.cells
        if cell.column + cell.span > frame.width
    )
    return DensityReport(
        inked_cells=inked,
        coverage_pct=round(100.0 * inked / total, 2),
        ceiling_pct=DENSITY_CEILING_PCT,
        floor_pct=DENSITY_FLOOR_PCT,
        text_rows=len(frame.text_rows),
        overflowing=overflowing,
    )


@dataclass(frozen=True)
class RestraintReport:
    """No element that carries no information."""

    decoration_only: Tuple[Tuple[int, str], ...]
    lone_markers: Tuple[Tuple[int, str], ...]
    frame_rows: int
    rules_surviving: int

    @property
    def ok(self) -> bool:
        """Whether every rendered row earns its place."""
        return not self.decoration_only and not self.lone_markers

    def as_dict(self) -> Dict[str, Any]:
        """The receipt a test asserts on."""
        return {
            "decoration_only": [list(item) for item in self.decoration_only],
            "lone_markers": [list(item) for item in self.lone_markers],
            "frame_rows": self.frame_rows,
            "rules_surviving": self.rules_surviving,
            "ok": self.ok,
        }


def restraint_report(frame: RenderedFrame) -> RestraintReport:
    """Measure RESTRAINT: every row is information or a declared frame.

    A row with no alphanumeric character is a candidate for deletion. It is
    deleted only if it matches NONE of `FRAME_PATTERNS`, so a rule and a
    card's frame row survive -- each with its reason in `DECORATION_EXEMPT` --
    and a row of decorative dots does not.

    The second half is the spinner rule: a motion glyph with no word beside
    it on the same row is noise, because a reader cannot tell from a
    rotating character alone whether the run is alive, stuck, or finished.
    """
    frame_matchers = [re.compile(pattern) for pattern in FRAME_PATTERNS]
    decoration: List[Tuple[int, str]] = []
    frame_rows = 0
    rules = 0
    lone: List[Tuple[int, str]] = []
    for row, cells in frame.rows.items():
        text = frame.row_text(row)
        has_word = any(ch.isalnum() for ch in text)
        if not has_word:
            if any(matcher.match(text.strip()) for matcher in frame_matchers):
                frame_rows += 1
                if set(text.strip()) & set("─━═-"):
                    rules += 1
            else:
                decoration.append((row, text[:72]))
        glyphs = [cell for cell in cells if cell.text.strip() in MOTION_GLYPHS]
        if glyphs:
            words = "".join(
                cell.text for cell in cells if any(ch.isalnum() for ch in cell.text)
            )
            if not words:
                lone.append((row, text[:72]))
    return RestraintReport(
        decoration_only=tuple(decoration),
        lone_markers=tuple(lone),
        frame_rows=frame_rows,
        rules_surviving=rules,
    )


# ---------------------------------------------------------------------------
# Duplicate information, measured on the surfaces themselves
# ---------------------------------------------------------------------------


#: Function words, dropped from the fact vocabulary. A fact is a CONTENT
#: word: `not` in "it is not a bug" and `no` in "no provider is connected"
#: are grammar, and two surfaces using the same grammar is not a reader
#: being told the same thing twice. The list is short and declared, because a
#: stopword list nobody can see is a place to hide a duplicate.
FACT_STOPWORDS: Tuple[str, ...] = (
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "no",
    "not",
    "of",
    "on",
    "or",
    "so",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
)


def surface_facts(text: str) -> frozenset:
    """The facts one surface publishes, as comparable tokens.

    Four normalisations, each because two surfaces spell one fact
    differently and a gate that cannot see that is a gate that reports
    thirty findings nobody reads:

    * case is dropped, so `GETTING STARTED` and `getting started` are one;
    * a FUNCTION WORD is dropped, so shared grammar is not a shared fact;
    * a money value becomes `money`, a duration `duration` and a bare count
      `count` -- the CLASS and not the value, because two surfaces showing
      `0s` and `1s` are still both publishing "elapsed time", and a check
      keyed on the value would go stale every second the shell is alive; and
    * a token shorter than two characters is dropped, because a separator is
      not a fact.
    """
    facts: set = set()
    for raw in _WORD_RE.findall(str(text or "")):
        token = raw.strip().lower()
        if len(token) < 2 or token in FACT_STOPWORDS:
            continue
        if _MONEY_RE.match(token):
            facts.add("money")
        elif _DURATION_RE.match(token):
            facts.add("duration")
        elif _COUNT_RE.match(token):
            facts.add("count")
        else:
            facts.add(token)
    return frozenset(facts)


@dataclass(frozen=True)
class DuplicateReport:
    """The facts two live surfaces both publish, and what is declared.

    `found` is the UNDECLARED duplicates -- the failures. `declared` is the
    registered debt that is still live, `stale` the registered debt that is
    no longer duplicated. Keeping the three apart is what makes the table a
    register rather than a blanket: a fact nobody declared fails, and a fact
    that stopped being duplicated is reported so the entry can be removed.
    """

    found: Tuple[Tuple[str, str, str], ...]
    declared: Tuple[Tuple[str, str, str], ...]
    stale: Tuple[Tuple[str, str, str], ...]
    pairs_checked: int

    @property
    def ok(self) -> bool:
        """Whether every observed duplicate is a declared, still-live one."""
        return not self.found and not self.stale

    def as_dict(self) -> Dict[str, Any]:
        """The receipt a test asserts on."""
        return {
            "undeclared": [list(item) for item in self.found],
            "declared": [list(item) for item in self.declared],
            "stale": [list(item) for item in self.stale],
            "pairs_checked": self.pairs_checked,
            "live_surfaces": list(LIVE_SURFACES),
            "ok": self.ok,
        }


def duplicate_report(surfaces: Mapping[str, str]) -> DuplicateReport:
    """Measure the duplicate-information property over the live surfaces.

    `surfaces` maps a surface name to the PLAIN TEXT that surface rendered,
    read from the live app's own widgets. Region membership comes from the
    app rather than from the SVG's geometry, because a receipt cannot say
    which widget a row belonged to and guessing it is how a check starts
    reporting the wrong thing.

    A fact two live surfaces both publish is a duplicate unless it is in
    `DUPLICATE_EXEMPT`, which names the fact, the reason, and the file whose
    owner has to change. A declared fact that is no longer duplicated is
    reported `stale`: a debt table that keeps entries nobody fixed stops
    being a register and becomes a hiding place.
    """
    facts = {name: surface_facts(surfaces.get(name, "")) for name in LIVE_SURFACES}
    found: List[Tuple[str, str, str]] = []
    checked = 0
    for first, second, _reason in DUPLICATE_PAIRS:
        if first not in facts or second not in facts:
            continue
        checked += 1
        for fact in sorted(facts[first] & facts[second]):
            found.append((first, second, fact))
    declared = {key for key in DUPLICATE_EXEMPT}
    observed = set(found)
    return DuplicateReport(
        found=tuple(sorted(observed - declared)),
        declared=tuple(sorted(observed & declared)),
        stale=(),
        pairs_checked=checked,
    )


def duplicate_rollup(reports: Sequence[DuplicateReport]) -> DuplicateReport:
    """Roll the per-receipt duplicate reports up to the PROJECT's register.

    Staleness is a project-level question, not a per-frame one: a fact that
    two surfaces publish in the `thinking` receipt and not in the `idle` one
    is a LIVE duplicate, and reporting it as stale in `idle` would empty the
    register eight times over. So a declared entry is stale only when it is
    live in NO receipt at all, which is the signal to delete the row.

    Undeclared findings union across receipts, because one undeclared
    duplicate in one state is a defect in the product.
    """
    undeclared: set = set()
    live: set = set()
    checked = 0
    for report in reports:
        undeclared.update(report.found)
        live.update(report.declared)
        checked = max(checked, report.pairs_checked)
    declared = set(DUPLICATE_EXEMPT)
    return DuplicateReport(
        found=tuple(sorted(undeclared)),
        declared=tuple(sorted(live)),
        stale=tuple(sorted(declared - live)),
        pairs_checked=checked,
    )


# ---------------------------------------------------------------------------
# Responsiveness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResponsivenessReport:
    """No jump on resize, measured across a sweep of adjacent widths."""

    widths: Tuple[int, ...]
    edge_moves: Tuple[Tuple[int, int], ...]
    visibility_changes: Tuple[Tuple[int, str], ...]
    undeclared: Tuple[Tuple[int, str], ...]
    violations: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether nothing moved and nothing appeared off a breakpoint."""
        return not self.violations

    def as_dict(self) -> Dict[str, Any]:
        """The receipt a test asserts on."""
        return {
            "widths": list(self.widths),
            "edge_moves": [list(item) for item in self.edge_moves],
            "visibility_changes": [list(item) for item in self.visibility_changes],
            "undeclared": [list(item) for item in self.undeclared],
            "violations": list(self.violations),
            "breakpoints": list(RESPONSIVE_BREAKPOINTS),
            "ok": self.ok,
        }


def responsiveness_report(sweep: Sequence["LayoutSpec"]) -> ResponsivenessReport:
    """Measure RESPONSIVENESS across a sweep of resolved layouts.

    Three things are a "jump" and none of them is a preference:

    * a region's WIDTH changing by more than
      `RESPONSIVE_MAX_WIDTH_SLOPE` columns between two adjacent terminal
      widths, except at a declared breakpoint. This is the strong form: the
      shell's geometry has to be piecewise-linear in the terminal width with
      its knots at the declared breakpoints, so no band can outrun the space
      it was given and no band can lose a column for an undeclared reason;
    * a full-width region that is not actually full width, which is a bar
      that stops short of the terminal and reads as a mistake; and
    * a region appearing or disappearing at a width that is not a declared
      breakpoint.

    Positions are measured and reported but not gated: a trailing rail's
    left edge travels as the window widens, which is what trailing means,
    and a rail pinned between the transcript and a context rail moves because
    the transcript widened -- the same fact the width clause already
    measures. The one position that IS pinned is the transcript's, and the
    suite asserts it at every width directly.
    """
    ordered = sorted(sweep, key=lambda spec: spec.width)
    violations: List[str] = []
    moves: List[Tuple[int, int]] = []
    changes: List[Tuple[int, str]] = []
    undeclared: List[Tuple[int, str]] = []
    for spec in ordered:
        for name in REGIONS:
            try:
                region = spec.region(name)
            except KeyError:
                continue
            if (
                region.visible
                and region_anchor(spec, name) == "full"
                and region.right < spec.width
            ):
                violations.append(
                    f"at width {spec.width} the {name} region stops at column "
                    f"{region.right} instead of the terminal's {spec.width}"
                )
    for previous, current in itertools.pairwise(ordered):
        at_breakpoint = current.width in RESPONSIVE_BREAKPOINTS
        # The bound SCALES with the terminal step, because a sweep that jumps
        # from 101 to 119 is 18 columns of terminal and a transcript that
        # grows 18 columns is a slope of one, not a jump of eighteen. Two
        # adjacent widths in the sweep are not necessarily adjacent widths of
        # the terminal, and a gate that treated them as if they were would
        # fail a shell that behaves perfectly.
        terminal_step = max(1, current.width - previous.width)
        allowance = RESPONSIVE_MAX_WIDTH_SLOPE * terminal_step
        for name in REGIONS:
            before = previous.region(name)
            after = current.region(name)
            if before.x != after.x:
                moves.append((current.width, abs(after.x - before.x)))
            slope = abs(after.width - before.width)
            if slope > allowance and not at_breakpoint:
                violations.append(
                    f"the {name} region changed width by {slope} columns over a "
                    f"{terminal_step}-column terminal step ending at width "
                    f"{current.width}, which is more than the declared slope "
                    f"of {RESPONSIVE_MAX_WIDTH_SLOPE} and is not a declared "
                    "breakpoint"
                )
            if before.visible != after.visible:
                changes.append((current.width, name))
                if not at_breakpoint:
                    undeclared.append((current.width, name))
                    violations.append(
                        f"{name} appeared or disappeared at width "
                        f"{current.width}, which is not a declared breakpoint"
                    )
    return ResponsivenessReport(
        widths=tuple(spec.width for spec in ordered),
        edge_moves=tuple(moves),
        visibility_changes=tuple(changes),
        undeclared=tuple(undeclared),
        violations=tuple(violations),
    )


# ---------------------------------------------------------------------------
# Colour: the audits
# ---------------------------------------------------------------------------


def _docstring_lines(source: str) -> frozenset:
    """The line numbers of every docstring in `source`, as a set.

    Read from the AST rather than pattern-matched, so a docstring is
    excluded because the interpreter says it is one and not because it
    happens to start in a column a regex can recognise. A class, a function,
    and a module all have one, and nested ones are collected too.
    """
    lines: set = set()
    try:
        import ast

        tree = ast.parse(str(source or ""))
    except Exception:
        return frozenset()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            value = first.value.value
            if isinstance(value, str):
                lines.add(int(first.lineno))
                lines.update(
                    range(
                        int(first.lineno),
                        int(getattr(first, "end_lineno", first.lineno) or first.lineno)
                        + 1,
                    )
                )
    return frozenset(lines)


def hex_literal_audit(source: str) -> Tuple[Tuple[int, str], ...]:
    """Every ``#rrggbb`` literal in EXECUTABLE source, with its line.

    Docstrings and comments are excluded, and the exclusion is structural
    rather than a regex somebody can satisfy by rewording: a comment is a
    ``COMMENT`` token and a docstring is a ``STRING`` token whose line the
    AST reports as one. The design system NAMES its own colours in prose,
    and a gate that flagged that prose would be a gate nobody keeps.

    Everything else is fair game: a hex in an f-string, a dict, a call
    argument, or a CSS block is a colour the product is about to draw.
    """
    import io
    import tokenize

    skip = _docstring_lines(source)
    found: List[Tuple[int, str]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(str(source or "")).readline))
    except Exception:
        return tuple(found)
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        if token.type != tokenize.STRING:
            continue
        if token.start[0] in skip:
            continue
        prefix = token.string[
            : len(token.string) - len(token.string.lstrip("rbufRBUF"))
        ]
        if "b" in prefix.lower():
            continue
        for match in re.finditer(r"#[0-9a-fA-F]{6}\b", token.string):
            found.append((int(token.start[0]), _normalize_hex(match.group(0))))
    return tuple(found)


def source_token_audit(paths: Iterable[Any], *, root: Any = None) -> Dict[str, Any]:
    """Scan the shell's own sources for hex literals outside the token table.

    The token table is read from `cli.theme`, so a colour that IS a token is
    never reported -- the audit is about LITERALS, which is the only form in
    which a token can be bypassed. A location in `SOURCE_TOKEN_EXEMPT` is
    reported separately from a finding, because an exemption that has
    stopped matching is a gate that has stopped working.
    """
    from pathlib import Path as _Path

    base = _Path(root) if root is not None else _Path.cwd()
    tokens = token_hexes()
    # A hex that one of this module's own tables NAMES as "not a product
    # colour" is a DECLARATION, not a use. `cli/design.py` has to spell
    # `#151515` out to say "this is the modal's untokened surface, and here
    # is who owns it", and an audit that failed on that would force the
    # declaration to be written in a form it could not read -- which is how
    # gates get defeated. Such a hit is still reported, in `declared`, and
    # the RECEIPT audit is the backstop that catches a renderer hard-coding
    # one of these values.
    declared_values = {*SCREENSHOT_CHROME, *UNTOKENED_RECEIPT_EXEMPT}
    findings: List[Dict[str, Any]] = []
    declared_hits: List[Dict[str, Any]] = []
    exempt_hits: List[Dict[str, Any]] = []
    for entry in paths:
        path = _Path(entry)
        try:
            source = path.read_text(encoding="utf-8")
        except Exception:
            continue
        label = str(path.relative_to(base)) if _is_relative(path, base) else str(path)
        for line, value in hex_literal_audit(source):
            if value in tokens:
                continue
            site = f"{path.name}:{line}"
            record = {"file": label, "line": line, "color": value}
            if site in SOURCE_TOKEN_EXEMPT:
                exempt_hits.append(record)
            elif value in declared_values:
                declared_hits.append(record)
            else:
                findings.append(record)
    matched = {f"{Path(hit['file']).name}:{hit['line']}" for hit in exempt_hits}
    return {
        "tokens": len(tokens),
        "findings": findings,
        "declared": declared_hits,
        "exempt": exempt_hits,
        "exempt_declared_but_unmatched": sorted(set(SOURCE_TOKEN_EXEMPT) - matched),
        "ok": not findings,
    }


def _is_relative(path: Any, base: Any) -> bool:
    """Whether `path` lives under `base`, without raising on either."""
    try:
        from pathlib import Path as _Path

        _Path(path).relative_to(_Path(base))
        return True
    except Exception:
        return False


def receipt_token_audit(frame: RenderedFrame) -> Dict[str, Any]:
    """Scan a RENDERED receipt for colours that are not tokens.

    This is the strongest form of the token gate, because it audits the bytes
    a terminal would have drawn rather than the code that drew them. A
    colour that reaches a widget through a Textual DESIGN variable
    (`$surface`) or through a framework default is invisible to a source scan
    and unmissable here -- which is how the approval modal's `#151515` was
    found.
    """
    tokens = token_hexes()
    declared: List[str] = []
    unknown: List[str] = []
    for value in frame.colors:
        if value in tokens or value in SCREENSHOT_CHROME:
            continue
        if value in UNTOKENED_RECEIPT_EXEMPT:
            declared.append(value)
        else:
            unknown.append(value)
    return {
        "tokens": len(tokens),
        "colors": list(frame.colors),
        "chrome": sorted(set(frame.colors) & set(SCREENSHOT_CHROME)),
        "declared_untokened": sorted(set(declared)),
        "unknown": sorted(set(unknown)),
        "ok": not unknown,
    }


def warm_hue_audit() -> Dict[str, Any]:
    """Audit the whole palette for warm greys and orange.

    Two rules, both computed from the token table rather than from a list of
    approved screenshots:

    * a NEUTRAL token -- a surface, a border, or body text -- may not carry
      any meaningful saturation, so a warm grey is unrepresentable; and
    * no token may sit in the orange/amber hue band unless it is a declared
      outcome token, so the accent can never drift into orange.

    EVERY resolved palette is audited -- each shipped theme crossed with each
    capability depth -- because a colour that only appears at 16 colours is
    still a colour somebody sees, and the resolved set is what the audit can
    reach without naming the token module's private tables.
    """
    import colorsys

    from cli import theme as _theme

    findings: List[Dict[str, Any]] = []
    checked = 0
    for palette_name, palette in sorted(_resolved_palettes().items()):
        for name in _theme.TOKEN_NAMES:
            value = palette.get(name)
            if not isinstance(value, str) or not value.strip():
                continue
            checked += 1
            text = _normalize_hex(value)
            try:
                red = int(text[1:3], 16) / 255.0
                green = int(text[3:5], 16) / 255.0
                blue = int(text[5:7], 16) / 255.0
            except ValueError:
                findings.append(
                    {"palette": palette_name, "token": name, "reason": "unparseable"}
                )
                continue
            hue, saturation, _value = colorsys.rgb_to_hsv(red, green, blue)
            if name not in HUE_BEARING_TOKENS and saturation > NEUTRAL_MAX_SATURATION:
                findings.append(
                    {
                        "palette": palette_name,
                        "token": name,
                        "reason": (
                            f"a neutral token carries saturation "
                            f"{saturation:.3f}: this is a warm grey"
                        ),
                    }
                )
            if (
                ORANGE_HUE_BAND[0] <= hue <= ORANGE_HUE_BAND[1]
                and name not in ORANGE_EXEMPT_TOKENS
            ):
                findings.append(
                    {
                        "palette": palette_name,
                        "token": name,
                        "reason": (
                            f"hue {hue * 360:.0f}deg is in the orange band and "
                            "the token is not a declared outcome"
                        ),
                    }
                )
    return {
        "checked": checked,
        "palettes": len(_resolved_palettes()),
        "findings": findings,
        "ok": not findings,
    }


# ---------------------------------------------------------------------------
# The composite report: one call, one verdict, every number
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameAudit:
    """Every property, measured on ONE receipt."""

    state: str
    frame: RenderedFrame
    alignment: AlignmentReport
    rhythm: RhythmReport
    hierarchy: HierarchyReport
    density: DensityReport
    restraint: RestraintReport
    duplicates: DuplicateReport
    colors: Dict[str, Any]

    @property
    def ok(self) -> bool:
        """Whether this one receipt satisfies the six properties.

        HIERARCHY is scoped: the four roles are required on the states that
        show the shell's COMPOSITION (`HIERARCHY_RECEIPTS`), and the two
        that do not are still required to be DISTINCT and are still measured.
        The scope lives in the report rather than in the caller so that a
        test cannot quietly narrow it.
        """
        hierarchy_ok = self.hierarchy.distinct
        if str(self.state) in HIERARCHY_RECEIPTS:
            hierarchy_ok = hierarchy_ok and self.hierarchy.ok
        return (
            self.alignment.ok
            and self.rhythm.ok
            and hierarchy_ok
            and self.density.ok
            and self.restraint.ok
            and self.duplicates.ok
            and bool(self.colors.get("ok"))
        )

    def as_dict(self) -> Dict[str, Any]:
        """The JSON a reader and a test both consume."""
        return {
            "state": self.state,
            "ok": self.ok,
            "hierarchy_required": str(self.state) in HIERARCHY_RECEIPTS,
            "viewport": {"width": self.frame.width, "height": self.frame.height},
            "cell": {"width": self.frame.cell_width, "height": self.frame.cell_height},
            "alignment": self.alignment.as_dict(),
            "rhythm": self.rhythm.as_dict(),
            "hierarchy": self.hierarchy.as_dict(),
            "density": self.density.as_dict(),
            "restraint": self.restraint.as_dict(),
            "duplicates": self.duplicates.as_dict(),
            "colors": dict(self.colors),
        }


def audit_frame(
    state: str,
    svg: str,
    spec: "LayoutSpec",
    *,
    surfaces: Mapping[str, str] | None = None,
    width: int = 0,
    height: int = 0,
    floating: bool = False,
) -> FrameAudit:
    """Measure all six properties on one rendered receipt.

    `svg` is the exporter's own output for the real app in one state; `spec`
    is the layout this viewport was resolved to; and `surfaces` is the plain
    text each live surface rendered, read from the app's own widgets (region
    membership is not recoverable from a screenshot, and guessing it is how a
    duplicate check starts reporting the wrong thing).

    `floating` declares that this receipt is a modal over the whole shell,
    which the pixels cannot say: a modal's border is drawn over the shell and
    the frame alone is ambiguous. It is a parameter rather than a guess
    because the fact has to have a name and a place in the receipt.

    Everything is derived from those inputs. There is no tolerance parameter,
    because a bound nobody can tighten is a bound nobody reads.
    """
    frame = parse_svg(svg, width=width or spec.width, height=height or spec.height)
    return FrameAudit(
        state=str(state),
        frame=frame,
        alignment=alignment_report(frame, spec),
        rhythm=rhythm_report(frame, spec, floating=floating),
        hierarchy=hierarchy_report(frame),
        density=density_report(frame),
        restraint=restraint_report(frame),
        duplicates=duplicate_report(dict(surfaces or {})),
        colors=receipt_token_audit(frame),
    )


@dataclass(frozen=True)
class AestheticReport:
    """Every receipt, every property, and one verdict."""

    frames: Tuple[FrameAudit, ...]
    responsiveness: ResponsivenessReport
    source_colors: Dict[str, Any]
    hues: Dict[str, Any]
    states_required: Tuple[str, ...]

    @property
    def measured_states(self) -> Tuple[str, ...]:
        """The states that actually got a receipt."""
        return tuple(frame.state for frame in self.frames)

    @property
    def missing_states(self) -> Tuple[str, ...]:
        """The required states with no receipt. Empty is the pass condition."""
        return tuple(
            state for state in self.states_required if state not in self.measured_states
        )

    @property
    def ok(self) -> bool:
        """The whole verdict. Every clause is a measurement, not a claim."""
        register = duplicate_rollup([frame.duplicates for frame in self.frames])
        return (
            not self.missing_states
            and all(frame.ok for frame in self.frames)
            and register.ok
            and self.responsiveness.ok
            and bool(self.source_colors.get("ok"))
            and bool(self.hues.get("ok"))
        )

    @property
    def duplicates(self) -> DuplicateReport:
        """The project-level duplicate register, rolled up from the receipts."""
        return duplicate_rollup([frame.duplicates for frame in self.frames])

    def failures(self) -> Tuple[str, ...]:
        """Named reasons the verdict is false, one per failing clause."""
        out: List[str] = []
        for state in self.missing_states:
            out.append(f"{state}: no rendered receipt")
        for frame in self.frames:
            if frame.ok:
                continue
            for name, report in (
                ("alignment", frame.alignment),
                ("rhythm", frame.rhythm),
                ("hierarchy", frame.hierarchy),
                ("density", frame.density),
                ("restraint", frame.restraint),
                ("duplicates", frame.duplicates),
            ):
                if not report.ok:
                    out.append(f"{frame.state}/{name}: {report.as_dict()}")
            if not frame.colors.get("ok"):
                out.append(f"{frame.state}/colors: {frame.colors.get('unknown')}")
        register = self.duplicates
        for first, second, fact in register.found:
            out.append(f"duplicates: {first} and {second} both publish {fact!r}")
        for first, second, fact in register.stale:
            out.append(
                f"duplicates: {first}|{second}|{fact} is registered but is no "
                "longer published twice"
            )
        for violation in self.responsiveness.violations:
            out.append(f"responsiveness: {violation}")
        for finding in self.source_colors.get("findings", ()):
            out.append(f"source colour literal: {finding}")
        for finding in self.hues.get("findings", ()):
            out.append(f"warm hue: {finding}")
        return tuple(out)

    def as_dict(self) -> Dict[str, Any]:
        """The whole audit as one JSON-safe document."""
        return {
            "ok": self.ok,
            "properties": [
                {
                    "name": item.name,
                    "question": item.question,
                    "measured_by": item.measured_by,
                    "bound": item.bound,
                }
                for item in AESTHETIC_PROPERTIES
            ],
            "states_required": list(self.states_required),
            "states_measured": list(self.measured_states),
            "states_missing": list(self.missing_states),
            "frames": [frame.as_dict() for frame in self.frames],
            "duplicates_rollup": self.duplicates.as_dict(),
            "responsiveness": self.responsiveness.as_dict(),
            "source_colors": dict(self.source_colors),
            "hues": dict(self.hues),
            "duplicate_exempt": {
                f"{a}|{b}|{fact}": reason
                for (a, b, fact), reason in DUPLICATE_EXEMPT.items()
            },
            "failures": list(self.failures()),
        }


def aesthetic_report(
    frames: Sequence[FrameAudit],
    *,
    sweep: Sequence["LayoutSpec"] = (),
    source_paths: Sequence[Any] = (),
    root: Any = None,
    states: Sequence[str] = AESTHETIC_STATES,
) -> AestheticReport:
    """Assemble the whole verdict from the receipts and the audits.

    `frames` are the per-state `FrameAudit`s the caller measured (a caller
    that rendered nothing therefore passes nothing -- this function does not
    invent a receipt), `sweep` is the resolved layout at each width the
    responsiveness check covers, and `source_paths` is the shell's own source
    for the hex-literal audit.
    """
    return AestheticReport(
        frames=tuple(frames),
        responsiveness=responsiveness_report(list(sweep)),
        source_colors=source_token_audit(list(source_paths), root=root),
        hues=warm_hue_audit(),
        states_required=tuple(str(state) for state in states),
    )
