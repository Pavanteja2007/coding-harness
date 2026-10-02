"""Composable terminal-shell layout and presentation components for Neo."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

from rich.console import Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import RichLog, Static

from cli import a11y as _a11y
from cli import commands as _commands
from cli import design as _design
from cli import runview as _rv
from cli import streamview as _sv
from cli import toggles as _toggles
from cli import ui

# Every layout constant used below is IMPORTED from `cli/design.py`, which is
# the single layout authority. This file may not DECLARE one: the gate is
# `tests/test_design_layout.py::test_no_layout_constant_lives_outside_this_file`,
# which reads every module under `cli/` with `ast`, so a reformat cannot empty
# it and a comment cannot satisfy it. The names are re-exported here because
# this module is the shell's component boundary and other modules already
# import them from it.
from cli.design import (  # noqa: F401
    ANTI_CLUTTER_EXEMPT,
    CANCEL_AFFORDANCE_MIN_COLUMNS,
    CONTEXT_MAX_WIDTH,
    CONTEXT_MIN_COLUMNS,
    CONTEXT_MIN_HEIGHT,
    CONTEXT_MIN_WIDTH,
    HEADER_CHROME_COLUMNS,
    HEADER_SEPARATOR,
    HEADER_STATUS_MAX,
    HEADER_TASK_FLOOR,
    HEADER_VALUE_MAX,
    MIN_RAIL_HEIGHT,
    PLAN_MIN_COLUMNS,
    RAIL_BLOCK_SEPARATOR,
    ULTRAWIDE_COLUMNS,
)

#: WHY the header's budget spends four columns on chrome, per segment bound,
#: and per the separator, all of which live in `cli/design.py`:
#:
#: * `HEADER_CHROME_COLUMNS` — `#neo-header` is `padding: 0 1`, and each
#:   chip carries a one-column left margin so the task and the status can
#:   never render as one run-on word. Measured against the compositor in an
#:   earlier round: without the two margins the rendered header read
#:   `task audit-task-1234running` at EVERY width, so the single most
#:   load-bearing pair of facts in the shell was one 26-character token.
#: * `HEADER_VALUE_MAX` — a value here is an identifier (a repository name, a
#:   model id, a mode name), so a shortened value is still an identifier the
#:   user can act on, unlike a clipped sentence, which stops being true at the
#:   cut. Every bound is MARKED with `…`.
#: * `HEADER_STATUS_MAX` — the status chip is never dropped, so it gets its own
#:   bound for an unknown or extended word.
#: * `HEADER_TASK_FLOOR` — task ids share a prefix (`fix-`, `agent-`), so the
#:   TAIL is the identifying part.
#: * `HEADER_SEPARATOR` — the budget spends the separator's RENDERED width, not
#:   one, because a budget that under-counts it clips the last segment's value.
#:
#: * `CANCEL_AFFORDANCE_MIN_COLUMNS` (VEX-CEILING-10) — 62 columns is the
#:   ceiling prompt's floor: below this the terminal is too narrow for a
#:   two-word hint plus separators, and the key binding is documented in help
#:   instead.


@dataclass(frozen=True)
class ShellLayout:
    """Immutable responsive policy for one terminal viewport.

    Every field is a PROJECTION of `cli.design.resolve_layout`. This class
    exists because the components read a flat record, and it is frozen
    because a layout that a component can mutate is not a policy. Two
    additive fields (`sidebar_mode`, `density`, `statusline_rows`) carry the
    tri-state sidebar and the density profile so a component can ask which
    one it is in without re-deriving it from the width.
    """

    width: int
    height: int
    orientation: str
    plan_rail_visible: bool
    context_rail_visible: bool
    plan_rail_width: int
    context_rail_width: int
    show_model: bool
    show_repo: bool
    task_width: int
    composer_height: int
    modal_width: int
    modal_max_width: int
    sidebar_mode: str = _design.DEFAULT_SIDEBAR_MODE
    density: str = _design.DEFAULT_DENSITY
    statusline_rows: int = 0

    @property
    def split_terminal(self) -> bool:
        """Whether the viewport is a deliberately narrow split terminal."""
        return self.width < _design.SPLIT_MIN_COLUMNS

    @property
    def vertical_terminal(self) -> bool:
        """Whether the viewport is taller than it is wide by a clear margin."""
        return self.orientation == "vertical"


def resolve_shell_layout(
    width: int,
    height: int,
    *,
    sidebar: str = _design.DEFAULT_SIDEBAR_MODE,
    density: str = _design.DEFAULT_DENSITY,
    statusline_rows: int = 0,
) -> ShellLayout:
    """Resolve the shell layout for a viewport, by delegating to `cli.design`.

    This is a PROJECTION, not a second implementation: every breakpoint,
    width, and chrome number behind the returned record lives in
    `cli/design.py`, so the components and the layout authority cannot
    disagree about what a viewport means.
    """
    spec = _design.resolve_layout(
        width,
        height,
        sidebar=sidebar,
        density=density,
        statusline_rows=statusline_rows,
    )
    return ShellLayout(
        width=spec.width,
        height=spec.height,
        orientation="vertical" if spec.vertical else "horizontal",
        plan_rail_visible=spec.sidebar_shown,
        context_rail_visible=spec.context_shown,
        plan_rail_width=spec.sidebar_cols,
        context_rail_width=spec.context_cols,
        show_model=spec.show_model,
        show_repo=spec.show_repo,
        task_width=spec.task_width,
        composer_height=spec.composer_height,
        modal_width=spec.modal_width,
        modal_max_width=spec.modal_max_width,
        sidebar_mode=spec.sidebar_mode,
        density=spec.density,
        statusline_rows=spec.statusline_rows,
    )


@dataclass(frozen=True)
class HeaderSegment:
    """One whole labelled fact in the persistent header.

    A segment is emitted WHOLE or not at all. That is the whole point: the
    first version of this header was clipped by CSS, so a narrow terminal
    rendered `◆ neo 0.2.1 · mode` — a label with no value — and the reader
    was left guessing what the mode was. A partially-rendered labelled fact
    is worse than an absent one, because an absent one is honestly absent.
    """

    key: str
    label: str
    value: str
    truncated: bool = False

    @property
    def plain(self) -> str:
        """The segment's unstyled text, used for the column budget."""
        return f"{self.label} {self.value}"

    def markup(self) -> str:
        """Render the segment as themed markup with both parts escaped.

        The separator is a STYLED dot, not a bare `[·]`: Textual parses
        `[…]` as a markup tag, and a tag it cannot resolve is rendered
        literally — so an unstyled separator prints three characters where
        one was budgeted and pushes the row into a clip. (Measured in this
        round: `◆ neo 0.2.1 [·] repo …` in the real compositor.)
        """
        suffix = HEADER_TRIM_MARKER if self.truncated else ""
        return (
            f" [neo.muted]{ui.DOT}[/] [neo.muted]{escape(self.label)}[/] "
            f"[{ui.TEXT_PRIMARY}]{escape(self.value)}[/]"
            + (f"[{ui.TEXT_SECONDARY}]{suffix}[/]" if suffix else "")
        )


#: The marker for a value shortened to its bound. Says so, rather than
#: letting a clipped identifier read as a whole one.
HEADER_TRIM_MARKER = "…"


def _bound_value(value: Any, key: str) -> Tuple[str, bool]:
    """Shorten an identifier to its declared bound, reporting whether it was."""
    text = ui.strip_ansi(str(value or "")).strip()
    limit = HEADER_VALUE_MAX.get(key, 0)
    if not text or limit <= 0 or len(text) <= limit:
        return (text, False)
    return (text[: limit - 1] + HEADER_TRIM_MARKER, True)


@dataclass(frozen=True)
class HeaderFit:
    """The decided header for one viewport: what is said, and what was left out.

    Every number here is a MEASURED allocation, not an estimate. The
    round-2 audit that produced this class rendered the real compositor at
    eight viewports and found the header silently clipping: the brand ran
    into the task chip, the task ran into the status chip, and the mode
    segment survived as a bare label. The prompt's rule is "collapse panels
    deliberately; do not simply squeeze them", and a one-line header that
    squeezes is the same defect in a smaller space.
    """

    layout: ShellLayout
    status: str
    status_truncated: bool
    task: str
    task_truncated: bool
    segments: Tuple[HeaderSegment, ...]
    brand: str
    dropped: Tuple[str, ...]
    overflow: bool

    @property
    def columns(self) -> int:
        """Columns the whole decided header occupies, for the fit assertion."""
        return (
            len(self.brand)
            + (1 + len(self.task) if self.task else 0)
            + (1 + len(self.status) if self.status else 0)
        )


def fit_header(model: "HeaderModel", status: Any = None) -> HeaderFit:
    """Decide the persistent header for `model`'s viewport. Pure and total.

    Allocation order, and the reason for it:

    1. **status** — bounded, never dropped. It is the answer to "what is the
       agent doing", and it is the one word that changes during a run.
    2. **task id** — bounded, never dropped; it keeps its unique tail, and it
       gives up columns before anything else does. The prompt makes the
       active task non-negotiable.
    3. **brand floor** — the ember mark, the wordmark, and the version.
    4. **whole segments** in the anatomy order the shell promises — repo,
       model, mode — each one admitted only if it fits completely. `mode` is
       last, so it is the first to go: it is the only one of the three the
       transcript and the rails also state.
    5. **the floor itself** is the last thing dropped, and `overflow` says
       so, because a header that cannot fit its floor must be reported as
       one rather than rendered as a row of clipped characters.

    Assumes the caller passes the status the app is about to publish; an
    empty status is treated as absent rather than as a zero-column fact, so
    a caller that does not track status yet still gets a sane header.
    """
    layout = model.layout
    raw_status = ui.strip_ansi(
        str(status if status is not None else model.status or "")
    )
    status_text = raw_status
    status_truncated = False
    if len(status_text) > HEADER_STATUS_MAX:
        status_text = status_text[: HEADER_STATUS_MAX - 1] + HEADER_TRIM_MARKER
        status_truncated = True

    # Both chips are non-negotiable, so they are charged against the viewport
    # FIRST and the brand gets what is left. An earlier version of this
    # function charged the task id twice and produced a header that dropped
    # its own version at 50 columns with forty columns to spare, so the
    # arithmetic is written out rather than folded.
    reserved = HEADER_CHROME_COLUMNS + (len(status_text) if status_text else 0)
    budget = layout.width - reserved
    task_text = model.task_display()
    task_label = f"task {task_text}" if task_text else ""
    # The task gives up columns before the status does: a long task id is a
    # detail, a missing status is a state change nobody saw. It gives them
    # up only far enough to keep the brand floor, and never past the id's
    # unique tail — a chip that has degraded to the bare word `task` has
    # stopped identifying anything.
    floor = model.brand_floor()
    target = max(0, budget - len(floor))
    while (
        task_label and len(task_label) > target and len(task_text) > HEADER_TASK_FLOOR
    ):
        keep = max(HEADER_TASK_FLOOR, len(task_text) - 1)
        task_text = HEADER_TRIM_MARKER + task_text[-(keep - 1) :]
        task_label = f"task {task_text}"
    available = budget - len(task_label)

    segments: List[HeaderSegment] = []
    dropped: List[str] = []
    brand = ""
    overflow = False
    # The floor degrades in declared steps — version, then the wordmark, then
    # the ember mark — rather than being cut mid-word, so a narrow header
    # still says `neo` when it cannot say which version it is.
    ladder = [floor]
    words = floor.split(" ")
    for count in range(len(words) - 1, 0, -1):
        ladder.append(" ".join(words[:count]))
    for candidate in ladder:
        if available >= len(candidate):
            brand = candidate
            if candidate != floor:
                dropped.append("version" if candidate != ladder[-1] else "wordmark")
            break
    if not brand:
        overflow = True
        if available > 0:
            brand = ladder[-1][:available]
    else:
        for segment in model.segments():
            # A PREFIX, not a best-effort fill. Admitting a short segment
            # after a long one was refused would render
            # `◆ neo 0.2.1 · repo X · mode build` with no model in it, which
            # reads as a missing fact rather than a deliberate collapse.
            # The separator is budgeted as it is RENDERED — a space, the dot,
            # a space — and not as the single space this function joins with.
            # The two-column version of that mistake was measured in a real
            # compositor: the fit admitted `mode build` with 51 columns
            # promised, the markup spent three per separator, and the value
            # was clipped off the end of a `height: 1` header.
            need = len(HEADER_SEPARATOR) + len(segment.plain)
            if len(brand) + need > available:
                dropped.append(segment.key)
                break
            brand += HEADER_SEPARATOR + segment.plain
            segments.append(segment)
    return HeaderFit(
        layout=layout,
        status=status_text,
        status_truncated=status_truncated,
        task=task_label,
        task_truncated=bool(
            task_text and task_text != str(model.task_id or "").strip()
        ),
        segments=tuple(segments),
        brand=brand,
        dropped=tuple(dropped),
        overflow=overflow,
    )


@dataclass(frozen=True)
class HeaderModel:
    """Header text derived from journal-backed session state.

    `status` is the word the shell's status chip is about to publish. It is
    part of the model because the header cannot be budgeted without it: the
    task and status chips are reserved columns, and a header that budgets
    itself while pretending the status does not exist is how the two chips
    end up welded together. It defaults to empty so a caller that does not
    track status still gets a correct (if roomier) header.
    """

    version: str
    model: str
    repo: Any
    mode: str
    task_id: Optional[str]
    layout: ShellLayout
    status: str = ""

    def brand_floor(self) -> str:
        """The unstyled brand + version, the last thing the header drops."""
        floor = f"{ui.GLYPHS['ember']} neo"
        if self.version:
            floor += f" {ui.strip_ansi(str(self.version))}"
        return floor

    def task_display(self) -> str:
        """The active task id, bounded to the viewport with its tail kept."""
        task_id = str(self.task_id or "").strip()
        if not task_id:
            return ""
        if len(task_id) > self.layout.task_width:
            keep = max(1, self.layout.task_width - 3)
            task_id = "..." + task_id[-keep:]
        return task_id

    def segments(self) -> List[HeaderSegment]:
        """The header's optional labelled facts, in the promised anatomy order.

        repo, then model, then mode — the order the shell's own anatomy
        diagram states, and the reverse of the order they are dropped in, so
        `mode` is the first fact to go when the terminal narrows.
        """
        out: List[HeaderSegment] = []
        if self.layout.show_repo:
            repo, cut = _bound_value(
                Path(str(self.repo or ".")).name or str(self.repo or "."), "repo"
            )
            if repo:
                out.append(HeaderSegment("repo", "repo", repo, cut))
        if self.layout.show_model:
            model, cut = _bound_value(self.model, "model")
            if model:
                out.append(HeaderSegment("model", "model", model, cut))
        mode = str(self.mode or "auto")
        if mode != "auto":
            mode, cut = _bound_value(mode, "mode")
            if mode:
                out.append(HeaderSegment("mode", "mode", mode, cut))
        return out

    def brand_markup(self, status: Any = None) -> str:
        """Render the responsive brand, workspace, model, and mode segment.

        Budgeted: a segment appears WHOLE or not at all, so the header can
        never present a label whose value was cut off.
        """
        fit = fit_header(self, status)
        bits = f"[neo.accent]{ui.GLYPHS['ember']} neo[/]"
        version = ui.strip_ansi(str(self.version or ""))
        if version and "version" not in fit.dropped:
            bits += f" [neo.muted]{escape(version)}[/]"
        for segment in fit.segments:
            style = "neo.accent2" if segment.key == "model" else "neo.muted"
            # The separator is styled, never a bare `[·]`: Textual reads
            # `[…]` as a markup tag and prints an unresolvable one literally,
            # which costs three columns where one was budgeted and pushes the
            # row into a clip.
            bits += (
                f" [neo.muted]{ui.DOT}[/] [{style}]{escape(segment.label)}[/] "
                f"[{ui.TEXT_PRIMARY}]{escape(segment.value)}[/]"
                + (
                    f"[{ui.TEXT_SECONDARY}]{HEADER_TRIM_MARKER}[/]"
                    if segment.truncated
                    else ""
                )
            )
        return bits

    def task_markup(self, status: Any = None) -> str:
        """Render a bounded active-task label that remains visible when rails collapse."""
        task = fit_header(self, status).task
        if not task:
            return ""
        return f"[neo.muted]task[/] [neo.accent2]{escape(task.split(' ', 1)[-1])}[/]"


# ---------------------------------------------------------------------------
# THE RAIL BUDGET (round 2)
#
# `PlanRail` and `ContextPanel` are `Vertical`s of `height: 1fr` holding
# several `height: auto` blocks, each with a CSS `max-height`. Textual
# satisfies them in order, so when the blocks add up to more rows than the
# rail has, the LAST blocks are pushed out of the rail and clipped — with no
# marker, no scrollbar, and nothing in the app able to tell.
#
# This is not theoretical. Rendering the real compositor at the prompt's own
# 120x36 found:
#
#   * the plan rail's `cost`, `tools`, `journal 1 unreadable event(s)` and
#     `error` rows were not on screen at ANY height where the rail appeared;
#   * the context rail's whole `EVIDENCE + USAGE` block — the verification
#     state, the call and token counts, and the cost — sat BELOW the rail's
#     bottom edge at 120x36 and 120x30, and was gone;
#   * Terminal 06's file-state legend (`? = not verified ...`) was clipped out
#     of the files block by `max-height: 12` at 120x36.
#
# A hidden cost is a budget nobody can see, and a hidden verification state
# is the exact failure the product exists to prevent. So the rails now
# allocate their rows DELIBERATELY, in a declared priority order, and a block
# that does not fit is bounded with the omission STATED.
# ---------------------------------------------------------------------------

#: How a block says it was cut. The row is spent on the admission rather than
#: on a fact, which is the honest trade: a reader who sees `+3 more` knows to
#: ask, and a reader who sees a silent cut concludes the list ended there.
RAIL_OMISSION_MARKER = "+{count} more"

#: Blocks are separated by one blank row (each region's `margin-bottom: 1`).
#: The number itself is `cli.design.RAIL_BLOCK_SEPARATOR`, which is the
#: shipped density's block gap; this file must not restate it.


def rail_rows(
    viewport_height: int,
    *,
    header: int = 1,
    runline: int = 1,
    announce: int = 1,
    composer: int = 3,
    footer: int = 1,
) -> int:
    """Rows a rail can occupy in `viewport_height`, never negative.

    The honest measurement is the rail's own `region.height` once it is
    mounted; this is the pre-mount fallback and the value the layout policy
    itself is asserted against. The chrome rows are the shell's own fixed
    regions, so a rail can never be told it has rows the composer needs.

    `announce` defaults to 1 — the status-announcement band — even though it
    is hidden when idle. A fallback that UNDER-promises is safe and one that
    over-promises is the defect this exists to prevent: the rails are
    rebuilt on every state change, and the announcement region appears and
    disappears with them.
    """
    used = header + runline + announce + composer + footer
    return max(0, int(viewport_height or 0) - used)


def rail_content_width(rail_width: int) -> int:
    """Columns a rail's content actually has: its width less border and padding.

    A rail is `width: N` with `padding: 0 1` and a one-column edge, so three
    columns belong to the frame. The mount tree is the authority — the
    callers pass a measured `region.width` when there is one — and this is
    the pre-mount value, so a caller that never mounts still bounds its
    lines to the space the rail will have.
    """
    return max(1, int(rail_width or 0) - 3)


def bound_block_text(value: Any, width: int) -> Text:
    """Bound every line of a rich `Text` to `width`, marking the cut.

    A rail line that WRAPS is the squeeze the prompt forbids, and it also
    makes a row budget a guess: two logical lines can cost four rendered
    rows, so the allocation that was supposed to guarantee a block its
    space quietly becomes wrong. Bounding per line makes one logical line
    cost exactly one rendered row, which is what lets the budget below be a
    measurement instead of an estimate.

    The same rule that produced the `+0 -0` / `error · E999 ·` fragments in
    the round-2 audit: a fact split across two rows, with the second row
    starting on a dangling separator, is not a fact anyone can read.
    """
    limit = max(0, int(width or 0))
    if limit == 0:
        return Text("")
    try:
        # Split WITHOUT the separator: rich's `include_separator=True` leaves
        # the `\n` on the end of each piece, and `truncate` would then spend
        # the line's last column on a newline and eat it. (Measured: a
        # three-line block came back as one 24-character line.)
        lines = value.split("\n")
    except Exception:
        return Text("")
    out = Text(no_wrap=True, overflow="ellipsis")
    for index, line in enumerate(lines):
        if index:
            out.append("\n")
        piece = line.copy()
        try:
            piece.truncate(limit, overflow="ellipsis")
        except Exception:
            piece = Text("")
        out.append_text(piece)
    return out


def _markup_lines(value: Any) -> List[str]:
    """Split a published rail block into its logical lines, dropping blanks.

    The plan rail publishes Textual MARKUP (its lines mix rich styles with
    `neo.*` theme roles, so they cannot be parsed into a `Text` the way the
    context rail's can). A markup line is therefore bounded by the widget's
    own `text-wrap: nowrap` / `text-overflow: ellipsis` rather than by
    :func:`bound_block_text`; both paths are asserted in the rendered-frame
    test, so "one logical line costs one rendered row" is a measured
    property of the shell and not a comment.
    """
    if value is None:
        return []
    text = value if isinstance(value, str) else str(value)
    return [line for line in text.splitlines() if line.strip()]


def _line_text(line: Any) -> str:
    """The plain text of a rail line, whether it is a string or a `Text`."""
    plain = getattr(line, "plain", None)
    return str(plain) if plain is not None else str(line)


def bounded_lines(
    lines: Sequence[Any],
    rows: int,
    *,
    marker: str = RAIL_OMISSION_MARKER,
    join: Optional[Callable[[List[Any]], Any]] = None,
    marker_line: Optional[Callable[[str], Any]] = None,
) -> str:
    """Render at most `rows` lines, stating the omission. Never raises.

    `rows <= 0` yields an empty string, so a block with no allocation
    disappears rather than printing one clipped line. `rows == 1` yields the
    marker alone: a single row cannot hold both a fact and its admission, and
    "there is more" is the more useful of the two.

    `join` and `marker_line` exist because the two rails publish different
    line types — the plan rail joins markup strings, the context rail joins
    styled `Text` — and the policy must be the same policy either way.
    """
    body = [line for line in (lines or []) if _line_text(line).strip()]
    if not body:
        return ""
    glue = join or (lambda items: "\n".join(str(item) for item in items))
    cap = max(0, int(rows or 0))
    if cap == 0:
        return ""
    if len(body) <= cap:
        return glue(body)
    text = marker.format(count=len(body) - max(0, cap - 1))
    tail = marker_line(text) if marker_line is not None else text
    if cap == 1:
        return tail
    return glue([*body[: cap - 1], tail])


def fit_region_blocks(
    rows: int,
    blocks: Mapping[str, Sequence[Any]],
    priority: Sequence[str],
    *,
    entry_counts: Optional[Mapping[str, int]] = None,
    min_entries: int = _design.ANTI_CLUTTER_MIN_ENTRIES,
    separator: int = RAIL_BLOCK_SEPARATOR,
    exempt: Optional[Mapping[str, str]] = None,
    **render: Any,
) -> Dict[str, Any]:
    """Allocate a rail's rows to its blocks in `priority` order.

    Returns one rendered value per block id, including ids absent from
    `blocks` (mapped to `""`), so a caller can set every region in one pass
    and cannot forget a block that lost its allocation. `render` is passed
    through to :func:`bounded_lines` (`join`, `marker_line`).

    The rule is whole blocks first, remainder second: each block in turn
    takes as many rows as it wants, out of what the higher-priority blocks
    left, minus one separator row per block that still follows. A block that
    cannot take its full share is bounded with the omission stated, and a
    block with nothing left is dropped. `blocks` NOT in `priority` are
    ignored — an unprioritised block would be an unbounded block.

    **The anti-clutter rule runs first, and only when the caller has said
    what the block's entries are.** `entry_counts` is how a caller states
    its real entry count: a block's rendered lines include its heading and
    its rows, neither of which is an entry, and a rule that counted rows
    would drop every bounded section and would make a `+N more` marker look
    like a section that was too thin to keep. With no `entry_counts` the
    rule does NOT run and the allocator keeps its historical contract
    exactly — which is the right default, because a rule about entry
    counts cannot be applied by a function that does not know them.

    `exempt` names the blocks the rule does not apply to. It defaults to
    `cli.design.ANTI_CLUTTER_EXEMPT`, whose every entry carries a reason: a
    rule with stated exceptions can be argued with, and an unstated one is a
    hole.
    """
    skip = dict(ANTI_CLUTTER_EXEMPT if exempt is None else exempt)
    data = {str(key): list(value or []) for key, value in (blocks or {}).items()}
    order = [str(key) for key in priority if str(key) in data]
    wanted = {
        key: len([line for line in data[key] if _line_text(line).strip()])
        for key in order
    }
    if entry_counts is not None:
        for key in order:
            if key in skip:
                continue
            if not _design.section_is_rendered(int(entry_counts.get(key, wanted[key]))):
                data[key] = []
                wanted[key] = 0
    out: Dict[str, Any] = {key: "" for key in data}
    remaining = max(0, int(rows or 0))
    for index, key in enumerate(order):
        # A separator is reserved only for a block that can still take a row.
        # Reserving one for a block that is about to be starved spends a blank
        # line on nothing, which is the same waste this function exists to
        # stop — just at one row instead of a whole block.
        following = sum(1 for later in order[index + 1 :] if wanted[later] > 0)
        available = max(0, remaining - following * max(0, int(separator)))
        take = min(wanted[key], available)
        out[key] = bounded_lines(data[key], take, **render)
        spent = max(0, int(separator)) if (take > 0 and following > 0) else 0
        remaining = max(0, remaining - take - spent)
    return out


def contextual_hints(
    layout: ShellLayout, active: bool = False, waiting: bool = False
) -> str:
    """Return shared viewport-aware command hints for the shell footer.

    The cancel affordance is non-negotiable at every width (VEX-CEILING-10).
    A run you cannot stop is worse than a run you cannot read, so at and
    below 62 columns — where the general hint list is trimmed to nothing —
    the footer still names cancel. Above that width the general list is
    used, and the cancel key is appended whenever a run is active so the
    affordance is present in the *default* hints too, not only the
    truncated ones.
    """
    if waiting and layout.width < 72:
        return "modal · esc cancel"
    hints = _commands.contextual_command_hints(
        active=active,
        waiting=waiting,
        width=layout.width,
    )
    if active and "cancel" not in hints.lower():
        return f"{hints} · ctrl+c cancel" if hints else "ctrl+c cancel"
    if not active and layout.width <= CANCEL_AFFORDANCE_MIN_COLUMNS:
        # Even with no run in flight the affordance stays discoverable at
        # narrow widths, so a user who learns it at 62 columns can find it
        # before starting the run they will need it for.
        return f"{hints} · ctrl+c cancel" if hints else "ctrl+c cancel"
    return hints


class ShellHeader(Horizontal):
    """Persistent status header with independently addressable brand, task, and status regions."""

    def compose(self) -> ComposeResult:
        """Compose the responsive header regions."""
        yield Static("", id="neo-brand")
        yield Static("", id="neo-task")
        yield Static("", id="neo-status")


class PlanRail(Vertical):
    """Left rail for the active plan, checkpoints, and compact run state."""

    #: The rail's blocks in the order they are ADMITTED when the rail is
    #: short. `status` first: mode, state, verify, cost, and a failure line
    #: are the facts the prompt's north star asks for, and they are the ones
    #: that used to be pushed off the bottom of the rail at every height.
    #: The plan list is the block that gives way, because the same run
    #: progress is also on the run line and in the transcript.
    BLOCK_PRIORITY: ClassVar[Tuple[str, ...]] = ("status", "checkpoints", "plan")

    DEFAULT_CSS = """
    PlanRail {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    PlanRail > Static {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose the plan rail, then the sidebar's own sections.

        The section widgets are mounted AFTER the three historical blocks,
        and that order is load-bearing: the plan/status blocks sit at the TOP
        of the column where they have always been, so adding the sidebar's
        sections can never move `#neo-side-status` — and the rendered-row
        budgets asserted against it — by a single row. Each section is
        `display: none` until it has something the anti-clutter rule allows
        it to show, so an idle sidebar costs nothing.
        """
        yield Static("", id="neo-side-header")
        yield Static("", id="neo-todo")
        yield Static("", id="neo-plan-checkpoints")
        yield Static("", id="neo-side-label")
        yield Static("", id="neo-side-status")
        for key in _design.SIDEBAR_SECTIONS:
            yield Static("", id=f"neo-sidebar-{key}")
        yield Static("", id="neo-sidebar-footer")

    def update_content(
        self,
        heading: Any,
        todo: Any,
        checkpoints: Any,
        status: Any,
        *,
        rows: Optional[int] = None,
        content_width: Optional[int] = None,
        entry_counts: Optional[Mapping[str, int]] = None,
        gap: int = RAIL_BLOCK_SEPARATOR,
    ) -> Dict[str, int]:
        """Update all rail regions without changing their mounted identity.

        `rows` is the rail's own measured height. Without it the blocks are
        published unbounded and Textual's layout cuts the tail — which is
        what hid `cost`, `error`, and the unreadable-event warning at every
        height where this rail appeared. With it, the blocks are allocated in
        `BLOCK_PRIORITY` and a block that does not fit says so.

        `content_width` bounds every line, so one logical line costs one
        rendered row and the allocation is a measurement rather than a guess.
        `entry_counts` states each block's real ENTRY count, which is what
        the anti-clutter rule needs: a run with no checkpoints has a
        `checkpoints 0` heading and ZERO entries, so the rule drops the
        block entirely rather than spending a row and a margin on the fact
        that nothing happened.

        `gap` is the row a block boundary costs, and it MUST be the density's
        own block gap rather than a default: an allocator that reserves a
        separator row the layout no longer spends is a budget that
        under-promises on purpose, and at a full rail that is two rows of
        evidence the reader could have had. Returns the row count published
        per block.
        """
        blocks = {
            "plan": _markup_lines(todo),
            "checkpoints": _markup_lines(checkpoints),
            "status": _markup_lines(status),
        }
        if rows is None:
            rendered = {key: "\n".join(value) for key, value in blocks.items()}
            allocation = {
                key: len([line for line in value if _line_text(line).strip()])
                for key, value in blocks.items()
            }
        else:
            rendered = fit_region_blocks(
                int(rows),
                blocks,
                self.BLOCK_PRIORITY,
                entry_counts=entry_counts,
                separator=gap,
                marker_line=lambda text: f"[{ui.TEXT_SECONDARY}]{escape(text)}[/]",
            )
            allocation = {
                key: len([line for line in str(value).splitlines() if line.strip()])
                for key, value in rendered.items()
            }
        self.query_one("#neo-side-header", Static).update(heading)
        for key, selector in (
            ("plan", "#neo-todo"),
            ("checkpoints", "#neo-plan-checkpoints"),
            ("status", "#neo-side-status"),
        ):
            node = self.query_one(selector, Static)
            node.update(rendered[key])
            # An EMPTY block is hidden rather than left as an empty box: an
            # empty `height: auto` Static still costs a row and a margin, and
            # the allocator accounts for a starved block as costing nothing.
            node.styles.display = "block" if rendered[key] else "none"
        self.query_one("#neo-side-label", Static).update("status")
        return allocation

    def update_sections(
        self,
        sections: Sequence[Any],
        *,
        rows: Optional[int] = None,
        content_width: Optional[int] = None,
        gap: int = RAIL_BLOCK_SEPARATOR,
    ) -> Dict[str, int]:
        """Publish the sidebar's own sections under the anti-clutter rule.

        A section the rule does not render is not published AT ALL: no
        heading, no rows, no margin, `display: none`. A section it does
        render spends one row on its heading plus its entries, and a
        COLLAPSED one spends only the heading — the triangle is the whole
        affordance, so the row that shows the triangle is the row that
        explains it.

        `rows` is the budget the sections may use in total; the footer's
        rows are counted first, because the directory a session is in is the
        one fact a reader must never have to scroll for. Returns the row
        count published per section, which is the receipt a test asserts on
        rather than inferring it from a screenshot.
        """
        width = max(1, int(content_width or rail_content_width(self.size.width or 42)))
        tokens = ui.active_tokens()
        published: Dict[str, int] = {}
        remaining = max(0, int(rows)) if rows is not None else None
        for section in sections or ():
            key = str(getattr(section, "key", "") or "")
            node_id = f"#neo-sidebar-{key}"
            if not key:
                continue
            try:
                node = self.query_one(node_id, Static)
            except Exception:
                continue
            if not getattr(section, "rendered", False):
                node.update("")
                node.styles.display = "none"
                node.styles.margin_bottom = 0
                published[key] = 0
                continue
            heading = str(getattr(section, "heading", "") or "")
            entries = [str(item) for item in getattr(section, "entries", ()) or ()]
            if getattr(section, "collapsed", False):
                body = [heading]
            else:
                body = [heading, *entries]
            wanted = len(body)
            if remaining is not None:
                wanted = min(wanted, remaining)
                # Reserve the trailing gap only when something follows.
                remaining = max(0, remaining - wanted - max(0, int(gap)))
            # PLAIN text, never markup. Every entry here is data the shell
            # did not author — a server label, a file path, a directory
            # name, a model's words — and a `[` in any of them would be
            # parsed as a tag and could delete the message it is in. The
            # style is applied by APPENDING to a `Text`, which never
            # re-parses what it is given.
            text = bounded_lines(body, wanted, marker_line=lambda item: str(item))
            shown_lines = [line for line in text.splitlines() if line.strip()]
            block = Text()
            for index, line in enumerate(shown_lines):
                if index:
                    block.append("\n")
                block.append(
                    line,
                    style=(
                        f"bold {tokens['accent_text']}"
                        if index == 0
                        else tokens["text_primary"]
                    ),
                )
            node.update(bound_block_text(block, width))
            shown = len(shown_lines)
            node.styles.display = "block" if shown else "none"
            node.styles.margin_bottom = max(0, int(gap)) if shown else 0
            published[key] = shown
        return published

    def update_footer(
        self, value: Any = None, *, content_width: Optional[int] = None
    ) -> int:
        """Publish the sidebar footer (directory parent + leaf, version).

        Returns the rows it published. An empty footer publishes nothing, so
        a shell that cannot resolve a directory does not carry a blank band.
        """
        width = max(1, int(content_width or rail_content_width(self.size.width or 42)))
        try:
            node = self.query_one("#neo-sidebar-footer", Static)
        except Exception:
            return 0
        if isinstance(value, _design.SidebarFooter):
            lines = list(value.lines) if value.rendered else []
        elif value is None:
            lines = []
        else:
            lines = [str(item) for item in str(value).splitlines() if str(item).strip()]
        text = bound_block_text(Text("\n".join(lines)), width) if lines else Text("")
        node.update(text)
        node.styles.display = "block" if lines else "none"
        node.styles.margin_bottom = 0
        return len(lines)


class ContextPanel(Vertical):
    """Right rail for changed files, diagnostics, verification, and usage."""

    #: `usage` is FIRST and is named for the fact it carries: the run's
    #: verification state, its call and token counts, and its cost. It sat at
    #: the bottom of this rail and was therefore the first thing off screen —
    #: at the prompt's own 120x36 the rail rendered no verification state and
    #: no cost at all. Evidence is the first fact, not the last.
    #:
    #: `legend` is LAST and separate. It is Terminal 06's explanation of the
    #: file-state codes, all five entries, and it is worth having — but it is
    #: five rows of static prose, and when it rode inside the files block it
    #: outranked the diagnostics and starved them to a bare `+4 more` with
    #: their heading gone. A state channel that costs a real fact is a
    #: channel that is too expensive.
    BLOCK_PRIORITY: ClassVar[Tuple[str, ...]] = (
        "usage",
        "files",
        "diagnostics",
        "relevant",
        "sources",
        "legend",
    )

    DEFAULT_CSS = """
    ContextPanel {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ContextPanel > Static {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose the context rail regions in the order they are read.

        The mount order IS the reading order and it matches
        `BLOCK_PRIORITY`, so the guarantee the budget makes is visible rather
        than hidden: the run's evidence is the first fact in the column, not
        the last one to be squeezed out of it. The ids are unchanged, so
        every existing query and pin still resolves.
        """
        yield Static("", id="neo-context-header")
        yield Static("", id="neo-context-usage")
        yield Static("", id="neo-context-files")
        yield Static("", id="neo-context-diagnostics")
        yield Static("", id="neo-context-relevant")
        yield Static("", id="neo-context-sources")
        yield Static("", id="neo-context-legend")

    def update_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        rows: Optional[int] = None,
        content_width: Optional[int] = None,
        gap: int = RAIL_BLOCK_SEPARATOR,
    ) -> Dict[str, int]:
        """Project one journal-derived run snapshot into the context rail.

        `rows` is what the rail actually has; without it the blocks are
        rendered unbounded and Textual's own layout cuts the tail (the
        behaviour this signature exists to end). `content_width` is the
        rail's measured content width; without it the rail's own width minus
        its frame is used. Returns the allocation per block — the receipt a
        caller or a test can assert on, so "the evidence block got zero rows"
        is a visible value rather than an inference from a screenshot.
        """
        data = dict(snapshot or {})
        tokens = ui.active_tokens()
        width = max(1, int(content_width or rail_content_width(self.size.width or 30)))
        task_id = str(data.get("task_id") or "").strip()
        header = Text("CONTEXT", style=f"bold {tokens['accent_text']}")
        if task_id:
            header.append(f" · {task_id}", style=tokens["text_secondary"])
        self.query_one("#neo-context-header", Static).update(
            bound_block_text(header, width)
        )

        changes = data.get("file_changes") or []
        normalized_changes = [item for item in changes if isinstance(item, Mapping)]
        if not normalized_changes:
            normalized_changes = [
                {"path": str(path), "status": "journal", "verified": False}
                for path in data.get("changed_files") or []
            ]
        files_lines: List[Text] = [
            Text("FILES", style=f"bold {tokens['text_secondary']}")
        ]
        if normalized_changes:
            for item in normalized_changes[:5]:
                path = str(item.get("path") or "?")
                staged = bool(item.get("staged"))
                verified = bool(item.get("verified"))
                checkpoint = bool(item.get("checkpoint_ids"))
                # Terminal 06: the rail stays COMPACT (`U?` fits five
                # rows in 30 columns) but the letters must not be the
                # only place the meaning lives — `?` in particular is a
                # shrug, not a word. `a11y.file_state_codes` is the ONE
                # producer of the code, so the header legend, the rails,
                # and the announcements cannot drift apart.
                code = _a11y.file_state_codes(
                    staged=staged, verified=verified, checkpoint=checkpoint
                )
                summary = str(item.get("summary") or item.get("status") or "")
                line = Text()
                line.append(f"[{code}] ", style=tokens["accent_text"])
                line.append(path, style=tokens["text_primary"])
                if summary:
                    line.append(f" {summary}", style=tokens["text_secondary"])
                files_lines.append(line)
            if len(normalized_changes) > 5:
                files_lines.append(
                    Text(
                        f"+{len(normalized_changes) - 5} more",
                        style=tokens["text_secondary"],
                    )
                )
        else:
            files_lines.append(Text("none changed", style=tokens["text_disabled"]))

        relevant = [str(path) for path in (data.get("relevant_files") or [])]
        relevant_lines: List[Text] = [
            Text("RELEVANT", style=f"bold {tokens['text_secondary']}")
        ]
        if relevant:
            for path in relevant[:4]:
                relevant_lines.append(Text(path, style=tokens["text_primary"]))
            if len(relevant) > 4:
                relevant_lines.append(
                    Text(f"+{len(relevant) - 4} more", style=tokens["text_secondary"])
                )
        else:
            relevant_lines.append(Text("not recorded", style=tokens["text_disabled"]))

        sources = data.get("sources") or []
        source_lines: List[Text] = [
            Text("SOURCES", style=f"bold {tokens['text_secondary']}")
        ]
        if sources:
            for item in sources[:4]:
                if isinstance(item, Mapping):
                    label = str(item.get("source") or item.get("path") or "source")
                    path = str(item.get("path") or "")
                    line = item.get("line")
                    detail = f"{label}:{path}" if path else label
                    if line:
                        detail += f":{line}"
                else:
                    detail = str(item)
                source_lines.append(Text(detail, style=tokens["text_secondary"]))
            if len(sources) > 4:
                source_lines.append(
                    Text(f"+{len(sources) - 4} more", style=tokens["text_secondary"])
                )
        else:
            source_lines.append(Text("no cited sources", style=tokens["text_disabled"]))

        diagnostics = [
            item
            for item in (data.get("diagnostics") or [])
            if isinstance(item, Mapping)
        ]
        diagnostics_lines: List[Text] = [
            Text("DIAGNOSTICS", style=f"bold {tokens['text_secondary']}")
        ]
        if diagnostics:
            for item in diagnostics[:4]:
                severity = str(item.get("severity") or "issue")
                path = str(item.get("path") or item.get("file") or "").strip()
                message = str(item.get("message") or "").strip()
                code = str(item.get("code") or "").strip()
                line = item.get("line")
                link = str(
                    item.get("link") or (f"{path}:{line}" if path and line else path)
                )
                # The LINK leads and the message follows, and the line is
                # bounded rather than wrapped: the audit rendered
                # `error · E999 ·` on one row and `· invalid syntax` on the
                # next, which is one fact split across two rows with a
                # dangling separator. A diagnostic is one line of text.
                parts = [part for part in (link, code, severity) if part]
                diagnostics_lines.append(
                    Text(
                        " · ".join(parts + ([message] if message else [])),
                        style=tokens["warning"],
                    )
                )
        else:
            diagnostics_lines.append(
                Text("none reported", style=tokens["text_disabled"])
            )

        usage = data.get("usage_known") or {}
        calls_known = bool(usage.get("calls", int(data.get("model_calls") or 0) > 0))
        tokens_known = bool(usage.get("tokens", int(data.get("tokens") or 0) > 0))
        cost_known = bool(usage.get("cost", False))
        verification = str(data.get("verification_state") or "not_run")
        if verification == "verified":
            verification_style = tokens["success"]
        elif verification in {"failed", "error"}:
            verification_style = tokens["error"]
        else:
            verification_style = tokens["pending"]
        usage_lines: List[Text] = [
            Text("EVIDENCE + USAGE", style=f"bold {tokens['text_secondary']}"),
            Text(f"verify {verification}", style=verification_style),
        ]
        calls = str(int(data.get("model_calls") or 0)) if calls_known else "unknown"
        token_count = f"{int(data.get('tokens') or 0):,}" if tokens_known else "unknown"
        cost = (
            ui.fmt_cost(float(data.get("cost_usd") or 0.0)) if cost_known else "unknown"
        )
        # One row per fact, not one row holding three: a 27-column rail
        # wrapped `1 calls · 1,200 tokens · $0.003100` across three rows and
        # lost the cost off the bottom of the rail with it. A bounded line
        # is a row the budget can actually count.
        usage_lines.append(Text(f"calls {calls}", style=tokens["text_primary"]))
        usage_lines.append(Text(f"tokens {token_count}", style=tokens["text_primary"]))
        usage_lines.append(Text(f"cost {cost}", style=tokens["accent_text"]))

        # Terminal 06's legend, all five entries, in its own LOWEST-priority
        # block directly under the file list. Without it `[U?]` is decoration;
        # with it, the rail is self-describing for anyone who cannot infer the
        # letters. It rides as its own block so the row budget can account
        # for it honestly: the round-2 audit found it clipped out of the files
        # block by a constant `max-height: 12` at 120x36, which is a state
        # channel silently removed from the product.
        legend_lines: List[Text] = [
            Text(entry, style=tokens["text_disabled"]) for entry in _a11y.legend_lines()
        ]

        blocks = {
            "usage": usage_lines,
            "files": files_lines,
            "diagnostics": diagnostics_lines,
            "relevant": relevant_lines,
            "sources": source_lines,
            "legend": legend_lines if normalized_changes else [],
        }
        if rows is None:
            # No measurement yet: bound the lines, publish everything, and
            # let the caller re-render once it knows the rail's height.
            rendered = {
                key: bound_block_text(Text("\n").join(value), width)
                for key, value in blocks.items()
            }
        else:
            rendered = fit_region_blocks(
                int(rows),
                blocks,
                self.BLOCK_PRIORITY,
                join=Text("\n").join,
                separator=gap,
                marker_line=lambda text: Text(text, style=tokens["text_secondary"]),
            )
        # The receipt counts ROWS, not characters. An earlier version re-joined
        # the rendered block through `Text("\n").join`, which iterates a Text
        # CHARACTER by character, and returned the string length of each block
        # (58 rows for a five-row evidence block) — a receipt that could not
        # fail is worse than no receipt.
        allocation = {
            key: len(
                [
                    line
                    for line in getattr(value, "plain", str(value)).split("\n")
                    if line.strip()
                ]
            )
            for key, value in rendered.items()
        }
        for key, selector in (
            ("usage", "#neo-context-usage"),
            ("files", "#neo-context-files"),
            ("diagnostics", "#neo-context-diagnostics"),
            ("relevant", "#neo-context-relevant"),
            ("sources", "#neo-context-sources"),
            ("legend", "#neo-context-legend"),
        ):
            node = self.query_one(selector, Static)
            node.update(
                bound_block_text(rendered[key], width) if rendered[key] else Text("")
            )
            # An EMPTY block is hidden, not left as an empty box. Textual
            # gives an empty `height: auto` Static one row, and the region's
            # `margin-bottom: 1` gives it a second: at 120x26 two empty
            # blocks spent three of the rail's nineteen rows, which pushed the
            # legend past the bottom edge. The allocator already accounts for
            # a starved block as costing nothing, so the widget has to agree.
            node.styles.display = "block" if rendered[key] else "none"
        return allocation


class EventFeed(RichLog):
    """Conversation and event surface with user-controlled scrollback following."""

    def _scroll_to(
        self,
        x: "float | None" = None,
        y: "float | None" = None,
        *,
        animate: bool = True,
        **kwargs: Any,
    ) -> bool:
        was_following = self.auto_scroll
        moved = super()._scroll_to(x, y, animate=animate, **kwargs)
        if y is not None:
            at_bottom = y >= self.max_scroll_y - 1
            self.auto_scroll = at_bottom or (was_following and at_bottom)
        return moved


class ShellFooter(Static):
    """Persistent contextual footer for commands and keyboard shortcuts."""

    def set_context(
        self, layout: ShellLayout, active: bool = False, waiting: bool = False
    ) -> None:
        """Render viewport-aware contextual hints."""
        self.update(contextual_hints(layout, active=active, waiting=waiting))


_ModalResult = TypeVar("_ModalResult")


class ModalFrame(ModalScreen[_ModalResult]):
    """Shared responsive sizing contract for all terminal modal screens."""

    #: The last viewport this modal fitted itself to. Terminal 06
    #: measured the cost of NOT having one: `on_mount` fits once via
    #: `call_after_refresh` and again via a 0.1 s timer, and each fit
    #: re-renders the plan rail and the context rail through
    #: `NeoApp._resize_from_modal`. Two identical fits per modal open is
    #: duplicated work on the UI thread, paid by every modal in the
    #: product. A fit whose viewport is unchanged is a no-op, which is
    #: also what a user would call it.
    _fitted_to: Optional[Tuple[int, int]] = None

    def on_mount(self) -> None:
        """Fit the modal to the viewport that mounted it."""
        self._fitted_to = None
        self.call_after_refresh(self._fit_modal)
        self.set_timer(0.1, self._fit_modal)

    def on_resize(self, event: events.Resize) -> None:
        """Refit an open modal without remounting or resetting its state."""
        self._fit_modal(event.size)

    def _fit_modal(self, size: Any = None) -> None:
        try:
            viewport_width = int(getattr(size, "width", 0) or self.size.width)
            viewport_height = int(getattr(size, "height", 0) or self.size.height)
        except Exception:
            return
        if viewport_width <= 0 or viewport_height <= 0:
            try:
                app_size = getattr(self.app, "size", None)
                viewport_width = int(getattr(app_size, "width", 0) or 0)
                viewport_height = int(getattr(app_size, "height", 0) or 0)
            except Exception:
                return
        if viewport_width < 20 or viewport_height < 5:
            return
        if self._fitted_to == (viewport_width, viewport_height):
            return
        self._fitted_to = (viewport_width, viewport_height)
        layout = resolve_shell_layout(viewport_width, viewport_height)
        try:
            box = next(
                (
                    child
                    for child in self.children
                    if isinstance(child, Widget)
                    and not str(getattr(child, "id", "") or "").startswith("textual-")
                ),
                None,
            )
            if isinstance(box, Widget):
                box.styles.width = layout.modal_width
                box.styles.max_width = layout.modal_max_width
        except Exception:
            return
        try:
            app = self.app
            resize_shell = getattr(app, "_resize_from_modal", None)
            if callable(resize_shell):
                resize_shell(layout.width, layout.height)
        except Exception:
            return


class CommandPaletteFrame(ModalFrame[Optional[Dict[str, Any]]]):
    """Responsive frame reserved for the command palette."""


@dataclass(frozen=True)
class EmptyState:
    """First-launch hero and later-launch compact actionable startup state."""

    first_launch: bool
    version: str
    repo: Any
    log_root: Any
    model: str
    width: int

    def lines(self) -> List[Any]:
        """Render the startup state sized for the current terminal width."""
        if not self.first_launch:
            repo = Path(str(self.repo or ".")).name or str(self.repo or ".")
            return [
                Text("NEO ready", style=f"bold {ui.ACCENT_TEXT}"),
                Text("Ask, change, run, or debug this repo.", style=ui.TEXT_PRIMARY),
                Text(
                    f"{repo}{ui.DOT}/help · ctrl+p palette · /sessions",
                    style=ui.TEXT_SECONDARY,
                ),
            ]
        rows = ui.wordmark_lines()
        logo_rows = (
            [ui.gradient_text(row) for row in rows]
            if ui._enc_ok("╔")
            else [Text(row, style=f"bold {ui.ACCENT_TEXT}") for row in rows]
        )
        tagline = (
            Text("  the AI harness that fixes bugs ", style=ui.TEXT_SECONDARY)
            + Text(ui.DOT, style=ui.ACCENT_TEXT)
            + Text(" verified, not vibed", style=ui.TEXT_PRIMARY)
        )
        if self.width < 64:
            result: List[Any] = list(logo_rows)
            result.extend([Text(""), tagline])
            result.append(Text(f"repo {self.repo}", style=ui.TEXT_PRIMARY))
            if self.version:
                result.append(
                    Text(
                        f"neo {self.version} · model {self.model}",
                        style=ui.TEXT_SECONDARY,
                    )
                )
            return result
        logo = Text()
        for index, row in enumerate(logo_rows):
            if index:
                logo.append_text(Text("\n"))
            logo.append_text(row)
        gap = "  "
        key_width = 5
        budget = max(self.width - 26 - len(gap) - key_width - 1 - 4, 20)

        def fit(value: object) -> str:
            text = str(value)
            return text if len(text) <= budget else "..." + text[-(budget - 3) :]

        values = [
            Text(fit(self.repo), style=f"bold {ui.TEXT_PRIMARY}"),
            Text(fit(self.log_root), style=ui.TEXT_PRIMARY),
            Text(fit(self.model), style=ui.ACCENT_TEXT),
        ]
        keys = ["repo", "logs", "model"]
        if self.version:
            keys.append("neo")
            values.append(Text(fit(self.version), style=ui.TEXT_PRIMARY))
        pad_top = max((len(logo_rows) - len(keys)) // 2, 0)
        result = []
        for index, logo_row in enumerate(logo_rows):
            line = Text()
            line.append_text(logo_row)
            line.append(gap)
            value_index = index - pad_top
            if 0 <= value_index < len(keys):
                line.append(
                    Text(keys[value_index].rjust(key_width), style=ui.TEXT_SECONDARY)
                )
                line.append(" ")
                line.append_text(values[value_index])
            result.append(line)
        result.extend([Text(""), tagline])
        return result


@dataclass(frozen=True)
class LoadingState:
    """Journal-derived live loading line for a running task."""

    task_id: str
    phase: str
    mode: str
    events: int
    cost_text: str
    elapsed_s: int
    thinking: bool
    spinner: str
    joke: str

    def render(self) -> str:
        """Render the current loading state as themed markup."""
        if self.thinking:
            prefix = f"[neo.glow]{self.spinner}[/] " if self.spinner else ""
            return (
                f"{prefix}[neo.glow]{escape(self.phase)}[/] [neo.muted]{ui.DOT}[/] "
                f"[i {ui.TEXT_SECONDARY}]{escape(self.joke)}[/] [neo.muted]{ui.DOT}[/] "
                f"[{ui.TEXT_PRIMARY}]{self.events} events[/] [neo.muted]{ui.DOT}[/] "
                f"[neo.accent2]{self.cost_text}[/] [neo.muted]{ui.DOT}[/] "
                f"[{ui.TEXT_PRIMARY}]{self.elapsed_s}s[/]"
            )
        prefix = f"[neo.running]{self.spinner}[/] " if self.spinner else ""
        return (
            f"{prefix}[neo.running]{escape(self.phase)}[/] [neo.muted]{ui.DOT}[/] "
            f"[{ui.TEXT_PRIMARY}]{self.events} events[/] [neo.muted]{ui.DOT}[/] "
            f"[neo.accent2]{self.cost_text}[/] [neo.muted]{ui.DOT}[/] "
            f"[{ui.TEXT_PRIMARY}]{self.elapsed_s}s[/]"
        )


@dataclass(frozen=True)
class ErrorState:
    """Concise recoverable error state for the conversation surface."""

    message: str
    recovery: str = ""

    def render(self) -> str:
        """Render the error and optional recovery action without exposing tracebacks."""
        text = f"[neo.error]error: {escape(str(self.message)[:160])}[/]"
        if self.recovery:
            text += f" [neo.muted]{ui.DOT} {escape(self.recovery)}[/]"
        return text


# ---------------------------------------------------------------------------
# THE STREAM PAINT (R2-17, closing R2-G09)
#
# `cli.streamview.StreamCoalescer` and `cli.tui._RunState.poll_frame` were
# already correct and live: the journal's `model_delta` rows are folded
# into a bounded coalescer and polled once per window. There was NO widget
# that painted the result, so the text was polled and thrown away.
#
# Three constraints shaped this, and all three are load-bearing:
#
# 1. BOUNDED. A 200k-token answer must cost the same to render as a
#    20-token one, and the paint must never push the one-line run line
#    off screen. `tail_lines` keeps the LAST N lines (the end of an answer
#    is what a waiting user needs; the head is what the transcript is
#    for) and marks the truncation honestly.
# 2. NO MARKUP INJECTION. Model output is UNTRUSTED text. This codebase
#    has already been bitten by exactly this: a `MarkupError` from an
#    orphaned `[/]`. `render()` therefore returns `rich.text.Text` and
#    NEVER a markup string, so a reply containing `[`, `[/]`, or
#    `[neo.accent]` is displayed literally and cannot close a tag it
#    never opened. It is the same discipline the diff path already uses.
# 3. IT DOES NOT SCROLL. The run line is updated in place; the transcript
#    is the scrollback. The paint is a peephole, not a log.
# ---------------------------------------------------------------------------

#: The paint is a peephole, not a log. Three lines is enough to see that
#: text is arriving and what it is about, and small enough that the run
#: line never leaves the viewport.
STREAM_PAINT_MAX_LINES = 3
#: Hard cap on the characters fed to the paint in one frame. The
#: coalescer already bounds its own text (`max_live_chars`); this is the
#: second, independent bound so a caller that bypasses the coalescer
#: still cannot make the paint expensive.
STREAM_PAINT_MAX_CHARS = 4000
#: The truncation marker. Says what happened instead of silently
#: showing a prefix that reads like the whole answer.
STREAM_PAINT_TRIM_MARKER = "…"


def stream_tail_lines(
    text: Any,
    *,
    max_lines: int = STREAM_PAINT_MAX_LINES,
    max_chars: int = STREAM_PAINT_MAX_CHARS,
) -> Tuple[List[str], int]:
    """Split untrusted stream text into the bounded tail to paint.

    Returns ``(lines, dropped)`` where ``dropped`` is the number of
    characters NOT shown, so the caller can be honest about truncation.
    Pure, total, and never raises: any non-string input is coerced with
    ``str()`` and a `None`/empty value yields ``([], 0)``.

    Assumes the caller wants the TAIL. A streaming answer grows at the
    end, and a user waiting on a slow endpoint needs the newest words,
    not the first ones. Blank lines are dropped (a stream of newlines
    would otherwise consume the whole budget) and the result is capped at
    `max_lines` and `max_chars` in that order.
    """
    if text is None:
        return ([], 0)
    raw = text if isinstance(text, str) else str(text)
    if not raw:
        return ([], 0)
    limit = max(1, int(max_chars or 1))
    body = raw[-limit:] if len(raw) > limit else raw
    dropped = len(raw) - len(body)
    parts = [line for line in body.splitlines() if line.strip()]
    if not parts:
        return ([], dropped)
    cap = max(1, int(max_lines or 1))
    if len(parts) > cap:
        dropped += sum(len(part) for part in parts[:-cap])
        parts = parts[-cap:]
    return (parts, dropped)


class StreamPaint(Static):
    """The bounded live-text widget for a streaming model reply.

    Lives directly above the composer, BELOW the one-line run line, so
    the run line's `height: 1` contract is untouched: the paint adds a
    widget, it does not make the run line multi-line.

    `update_stream` is the only write path and it takes UNTRUSTED text.
    It never returns or renders a markup string, so a model reply
    containing `[`, `[/]`, or `[neo.*]` renders literally instead of
    closing a tag (the `MarkupError` this codebase has already hit).
    """

    DEFAULT_CSS = """
    StreamPaint {
        height: auto;
        max-height: 4;
        padding: 0 2;
        background: $neo-panel;
        border-left: outer $neo-streaming;
        color: $neo-secondary;
        display: none;
        overflow: hidden;
    }
    """

    def __init__(
        self,
        *,
        max_lines: int = STREAM_PAINT_MAX_LINES,
        max_chars: int = STREAM_PAINT_MAX_CHARS,
        **kwargs: Any,
    ) -> None:
        """Create the paint. `max_lines`/`max_chars` are the paint's own
        independent bounds; they are clamped to at least 1 so a hostile
        configuration cannot produce a zero-height widget."""
        super().__init__("", **kwargs)
        self.max_lines = max(1, int(max_lines or 1))
        self.max_chars = max(1, int(max_chars or 1))
        self.dropped_chars = 0

    def paint(self, text: Any) -> Text:
        """Build the paintable `Text`. Pure: no widget state is touched.

        Returns an EMPTY `Text` for empty input, which is what lets the
        caller hide the widget instead of painting a blank box.
        """
        lines, dropped = stream_tail_lines(
            text, max_lines=self.max_lines, max_chars=self.max_chars
        )
        self.dropped_chars = dropped
        body = Text(no_wrap=False, overflow="ellipsis")
        if not lines:
            return body
        for index, line in enumerate(lines):
            if index:
                body.append("\n")
            body.append(line, style=ui.TEXT_SECONDARY)
        if dropped:
            body.append("\n")
            body.append(STREAM_PAINT_TRIM_MARKER, style=ui.TEXT_SECONDARY)
        return body

    def update_stream(self, text: Any) -> bool:
        """Paint `text` (untrusted) and reveal/hide the widget. Never raises.

        Returns True when something is being shown. The widget is hidden
        for empty input so an idle shell does not carry a blank box, and
        the display flip is wrapped because a widget mid-unmount can
        legitimately refuse a style write.
        """
        try:
            body = self.paint(text)
            shown = bool(body.plain)
            self.update(body)
            self.styles.display = "block" if shown else "none"
            return shown
        except Exception:
            return False

    def clear_stream(self) -> None:
        """Hide the paint. Never raises."""
        try:
            self.dropped_chars = 0
            self.update(Text(""))
            self.styles.display = "none"
        except Exception:
            pass


@dataclass(frozen=True)
class ResultCard:
    """Single verifier-gated completion card built from run facts."""

    task_id: str
    mode: str
    facts: Mapping[str, Any]

    def render(self) -> Group:
        """Render exactly one completion summary plus an optional answer."""
        facts = dict(self.facts or {})
        raw_status = str(facts.get("status") or "unknown")
        raw_evidence = facts.get("verification_evidence")
        if isinstance(raw_evidence, Mapping):
            evidence = [raw_evidence]
        else:
            evidence = list(raw_evidence or [])
        if not evidence and facts.get("target_passed") is not None:
            evidence = [facts]
        status = _rv.effective_terminal_status(raw_status, evidence)
        verified = _rv.status_is_verified(status)
        completed = _rv.status_is_completed(status)
        if verified:
            mark = ui.GLYPHS["ok"]
            style = ui.SUCCESS
        elif completed:
            mark = ui.GLYPHS["wait"]
            style = ui.WARNING
        else:
            mark = ui.GLYPHS["fail"]
            style = ui.ERROR
        label = (
            "ERROR"
            if raw_status.lower() == "error"
            else _rv.status_label(status)
            if status != "unknown"
            else "UNKNOWN"
        )
        issue = ui.strip_ansi(str(facts.get("issue") or self.task_id))[:90]
        table = Table.grid(padding=(0, 1), expand=False)
        table.add_row(
            Text.assemble(
                (f"{mark} ", f"bold {style}"),
                (label, f"bold {style}"),
                (f" · {self.mode} · {issue}", ui.TEXT_PRIMARY),
            )
        )
        chips: List[str] = []
        if facts.get("attempts") is not None:
            chips.append(f"{facts['attempts']} attempt(s)")
        if facts.get("model_calls"):
            chips.append(f"{int(facts['model_calls'])} model call(s)")
        if facts.get("tokens"):
            chips.append(f"{int(facts['tokens']):,} tokens")
        if facts.get("elapsed_s") is not None:
            chips.append(_rv.fmt_elapsed(facts["elapsed_s"]))
        if facts.get("cost_usd") is not None:
            chips.append(ui.fmt_cost(float(facts["cost_usd"])))
        if chips:
            table.add_row(Text(" · ".join(chips), style=ui.TEXT_SECONDARY))
        file_changes = [
            item
            for item in (facts.get("file_changes") or [])
            if isinstance(item, Mapping)
        ]
        files = list(facts.get("files") or facts.get("changed_files") or [])
        if file_changes:
            labels = []
            for item in file_changes[:6]:
                # Terminal 06: one producer for the compact code, so the
                # card and the rail cannot encode the same state two
                # different ways.
                code = _a11y.file_state_codes(
                    staged=bool(item.get("staged")),
                    verified=bool(item.get("verified")),
                    checkpoint=bool(item.get("checkpoint_ids")),
                )
                labels.append(f"[{code}] {item.get('path', '?')}")
            table.add_row(Text("files: " + ", ".join(labels), style=ui.TEXT_PRIMARY))
            # The expanded wording rides the card too: this is the surface
            # a screen reader reaches first, and `[U?]` means nothing
            # without a sentence somewhere.
            words = "; ".join(
                _a11y.describe_file_state(
                    item.get("path", "?"),
                    staged=bool(item.get("staged")),
                    verified=bool(item.get("verified")),
                    checkpoint=bool(item.get("checkpoint_ids")),
                )
                for item in file_changes[:3]
            )
            if words:
                table.add_row(Text(words, style=ui.TEXT_SECONDARY))
        elif files:
            table.add_row(
                Text(
                    "files: " + ", ".join(str(path) for path in files[:6]),
                    style=ui.TEXT_PRIMARY,
                )
            )
        verification = facts.get("latest_verification") or {}
        if verification:
            verdict = _rv.verification_state(evidence, status)
            verdict_style = (
                ui.SUCCESS
                if verdict == "verified"
                else ui.ERROR
                if verdict in {"failed", "error"}
                else ui.WARNING
            )
            table.add_row(Text(f"verify: {verdict.upper()}", style=verdict_style))
        if facts.get("branch"):
            table.add_row(
                Text(
                    f"branch: {facts['branch']} {str(facts.get('commit_sha') or '')[:8]}",
                    style=ui.TEXT_PRIMARY,
                )
            )
        if facts.get("last_error") and not verified:
            table.add_row(Text(str(facts["last_error"])[:160], style=ui.ERROR))
        renderables: List[Any] = [
            Panel(table, title=f"summary {self.task_id}", border_style=ui.BORDER_SUBTLE)
        ]
        answer = ui.strip_ansi(str(facts.get("answer") or ""))
        if answer:
            renderables.append(Markdown(answer))
        return Group(*renderables)


# ===========================================================================
# VEX-PF-04 — the role hierarchy, rendered
# ===========================================================================
#
# `cli.streamview` decides WHAT a row is; this section decides how it
# looks. The division matters: the role table is a product decision that
# belongs next to the other pure projections, while the styling belongs
# with the widgets, and a surface that wants the role without the
# styling can import one without the other.
#
# Four invariants every function below obeys:
#
# 1. NOTHING IS A MARKUP STRING. Every renderer returns `rich.text.Text`.
#    A journal row, a model reply and a filesystem path all reach this
#    module, and a `[` in any of them must render as a bracket. A
#    render failure NEVER deletes the message: each renderer falls back
#    to a plain `Text` carrying the same characters.
#
# 2. STRUCTURE CARRIES THE MEANING. Indentation, glyph, weight and slant
#    come from `streamview.RoleSpec`; the token is decoration on top of a
#    shape that already works with colour removed.
#
# 3. A BOUND IS MARKED. Truncation, folded lines and omitted files all
#    print the count.
#
# 4. A SECTION WITH TWO OR FEWER ENTRIES IS NOT RENDERED. That rule is
#    `toggles.section_rows`, applied here so a panel cannot opt out of it
#    by accident.

#: One class per role so the band a role draws is CSS, not a computed
#: string. `streamview` owns the names.
ROLE_CLASS = {name: "role-" + name.replace(" ", "-") for name in _sv.ROLE_SPECS}

#: The anti-clutter rule is `cli.design`'s, not this module's. Re-exported
#: so a caller of a widget in this file does not have to know which module
#: owns layout policy; the value is `cli.design.ANTI_CLUTTER_MIN_ENTRIES`
#: either way.
MIN_SECTION_ENTRIES = _design.ANTI_CLUTTER_MIN_ENTRIES
section_is_rendered = _design.section_is_rendered

#: Which token paints which role's text. Resolved from the theme, so no
#: literal colour appears in this module.
_ROLE_STYLE: Dict[str, str] = {
    "text_secondary": "text_secondary",
    "accent_text": "accent_text",
    "text_primary": "text_primary",
    "warning": "warning",
    "error": "error",
    "text_disabled": "text_disabled",
}


def _token(name: str, fallback: str) -> str:
    """Resolve a design token by name, degrading to a known-good value.

    ``cli.ui`` does not re-export every token, and an attribute that
    raises ``AttributeError`` inside a render is exactly how a message
    disappears. The theme is the authority; the fallback keeps a broken
    theme from taking the transcript with it.
    """
    value = getattr(ui, name.upper(), None)
    if isinstance(value, str) and value:
        return value
    try:
        from cli import theme as _theme

        tokens = _theme.textual_variables()
        candidate = tokens.get("neo-" + name.replace("_", "-"))
        if isinstance(candidate, str) and candidate:
            return candidate
    except Exception:
        pass
    return fallback


#: Resolved once, from the theme, so no literal colour is written here and
#: no missing attribute can raise inside a render.
TEXT_DISABLED = _token("text_disabled", ui.TEXT_SECONDARY)


def _role_style(spec: _sv.RoleSpec, *, bold: bool = False) -> str:
    """Resolve a role's token to a real style string, never raising."""
    base = _ROLE_STYLE.get(spec.style_token, "text_primary")
    style = (
        ui.TEXT_PRIMARY if base == "text_primary" else _token(base, ui.TEXT_SECONDARY)
    )
    if bold or spec.bold:
        style = f"bold {style}"
    if spec.italic:
        style = f"italic {style}"
    return style


def role_text(
    block: Any,
    settings: Any = None,
    *,
    ascii_only: bool = False,
    collapsed: Optional[bool] = None,
    show_timestamps: Optional[bool] = None,
    show_username: Optional[bool] = None,
    show_metadata: Optional[bool] = None,
) -> Text:
    """Render one :class:`streamview.RoleBlock` as a ``Text``.

    The toggles are read through key-presence: an absent setting means
    "nobody said", and the block's own defaults then apply. That is what
    lets a caller pass ``settings=None`` and still get a correct render,
    which is the difference between a reusable widget and one that only
    works when a specific session object happens to be around.
    """
    if not isinstance(block, _sv.RoleBlock):
        return Text("")
    spec = block.spec
    stamps = _toggle_on(settings, "timestamps", show_timestamps)
    authors = _toggle_on(settings, "username_visible", show_username)
    metadata = _toggle_on(settings, "assistant_metadata_visibility", show_metadata)
    details = _toggle_on(settings, "tool_details_visibility", None)
    conceal = _toggle_on(settings, "code_conceal", None)
    folded = block.collapsed if collapsed is None else bool(collapsed)

    out = Text(no_wrap=False, overflow="ellipsis")
    out.append(spec.prefix(ascii_only=ascii_only), style=_role_style(spec))
    out.append(block.label, style=_role_style(spec))
    if block.detail:
        out.append(" - ", style=ui.TEXT_SECONDARY)
        out.append(
            block.detail, style=ui.TEXT_PRIMARY if spec.bold else ui.TEXT_SECONDARY
        )
    if stamps and block.timestamp:
        out.append(f"  {block.timestamp}", style=TEXT_DISABLED)
    out.append("\n")
    if authors and block.username:
        out.append("  from " + block.username + "\n", style=ui.TEXT_SECONDARY)
    body = list(block.body)
    if conceal and len(body) > 6:
        body = [body[0], f"... {len(body) - 1} more line(s) concealed"]
    if block.role is _sv.Role.RESULT and details is False:
        body = []
    if folded:
        out.append("  " + (body[0] if body else "(folded)"), style=ui.TEXT_SECONDARY)
        out.append("\n")
        if len(body) > 1:
            out.append(f"  +{len(body) - 1} more lines (folded)\n", style=TEXT_DISABLED)
        return out
    for line in body:
        out.append("  " + line + "\n", style=ui.TEXT_SECONDARY)
    if metadata:
        for line in block.meta:
            out.append("  " + line + "\n", style=TEXT_DISABLED)
    if block.truncated:
        out.append("  ... output cut here\n", style=TEXT_DISABLED)
    return out
    for line in body:
        out.append("  " + line + "\n", style=ui.TEXT_SECONDARY)
    if metadata:
        for line in block.meta:
            out.append("  " + line + "\n", style=ui.TEXT_DISABLED)
    if block.truncated:
        out.append("  ... output cut here\n", style=ui.TEXT_DISABLED)
    return out


def _toggle_on(settings: Any, name: str, explicit: Optional[bool]) -> Optional[bool]:
    """Resolve one toggle, honouring an explicit override first.

    ``None`` means "nobody decided", which is different from ``False``:
    a caller that has no settings object must not silently switch a
    toggle off.
    """
    if explicit is not None:
        return bool(explicit)
    if settings is None:
        return None
    getter = getattr(settings, "is_true", None)
    if callable(getter):
        try:
            return bool(getter(name))
        except Exception:
            return None
    if isinstance(settings, Mapping):
        value = settings.get(name)
        return value if isinstance(value, bool) else None
    return None


def role_block_lines(blocks: Any, settings: Any = None, **kwargs: Any) -> List[Text]:
    """Render a list of blocks, dropping the ones with nothing in them."""
    rendered: List[Text] = []
    for block in list(blocks or []):
        if isinstance(block, _sv.RoleBlock) and block.empty:
            continue
        piece = role_text(block, settings, **kwargs)
        if piece.plain:
            rendered.append(piece)
    return rendered


def undo_text(notice: Any, settings: Any = None) -> Text:
    """Render an inline undo/redo receipt as a ``Text``.

    Left-bordered by CSS (the widget owns the border), and it reads
    exactly what the brief asks for: how many messages went back, the
    key that puts them back, the command that re-applies it, and every
    file with ``+N -M``.
    """
    if not isinstance(notice, _sv.UndoNotice):
        return Text("")
    out = Text(no_wrap=False, overflow="ellipsis")
    out.append(notice.headline + "\n", style=f"bold {ui.WARNING}")
    out.append(
        f"  restore: {notice.restore_key}  ·  {notice.redo_command} to apply again\n",
        style=ui.TEXT_SECONDARY,
    )
    listed = notice.files[: _sv.MAX_FILES_LISTED]
    for item in listed:
        out.append(f"  {item.plain()}\n", style=ui.TEXT_PRIMARY)
    extra = max(0, len(notice.files) - len(listed)) + max(
        0, int(notice.hidden_files or 0)
    )
    if extra > 0:
        out.append(
            f"  +{extra} more file(s) reverted (not listed)\n", style=TEXT_DISABLED
        )
    return out


def thinking_text(
    stream: Any, settings: Any = None, *, collapsed: bool = False
) -> Text:
    """Render the live thinking lane as a ``Text``.

    Dim, italic and indented - the shape says "this is the model
    thinking" before the colour is read. The cancellation note is
    rendered by the block itself, so a cancel that kept its partial text
    says so in the transcript rather than only in a log.
    """
    if not isinstance(stream, _sv.ThinkingStream):
        return Text("")
    block = stream.block(collapsed=collapsed)
    return role_text(block, settings)


def composer_hints(state: Any, settings: Any = None) -> Tuple[str, str]:
    """``(placeholder, hint)`` for the composer under a pending decision.

    When a permission or a question is pending the composer is disabled
    and the placeholder becomes the reason: the user is told what is
    waiting and that typing will not reach the run. Returning the
    placeholder rather than mutating a widget keeps this function testable
    on a host with no terminal.
    """
    if not isinstance(state, _sv.ComposerState):
        return ("", "")
    if not state.disabled:
        line = _toggles.hint_line(settings) if settings is not None else ""
        return ("ask, or / for commands", line)
    return (state.hint(), "the decision has to be made first")


class RoleBlockWidget(Static):
    """One role block in the transcript, banded by role.

    The widget is deliberately dumb: it renders whatever
    :class:`streamview.RoleBlock` it is handed, and it never composes a
    markup string. ``update_block`` is the only write path.
    """

    DEFAULT_CSS = """
    RoleBlockWidget {
        height: auto;
        padding: 0 1;
        text-style: none;
    }
    RoleBlockWidget.role-thinking { border-left: outer $neo-disabled; }
    RoleBlockWidget.role-action { border-left: outer $neo-accent; }
    RoleBlockWidget.role-result { border-left: outer $neo-border; }
    RoleBlockWidget.role-edit {
        border-left: outer $neo-highlight;
        background: $neo-panel;
    }
    RoleBlockWidget.role-needs-you {
        border-left: outer $neo-warning;
        background: $neo-panel;
        padding: 0 2;
    }
    RoleBlockWidget.role-failure {
        border-left: outer $neo-error;
        background: $neo-panel;
        padding: 0 2;
    }
    RoleBlockWidget.role-system { border-left: none; }
    """

    def __init__(
        self, block: Any = None, *, settings: Any = None, **kwargs: Any
    ) -> None:
        super().__init__("", **kwargs)
        self.settings = settings
        self._role = ""
        if block is not None:
            self.update_block(block)

    def _apply_role(self, role: Any) -> None:
        name = role.value if isinstance(role, _sv.Role) else str(role or "system")
        if name == self._role:
            return
        if self._role:
            try:
                self.remove_class(ROLE_CLASS.get(self._role, ""))
            except Exception:
                pass
        token = ROLE_CLASS.get(name, "role-system")
        self._role = name
        try:
            self.add_class(token)
        except Exception:
            # A widget mid-unmount can refuse a class write. The message
            # still renders; only the band is missing.
            pass

    def update_block(self, block: Any, **kwargs: Any) -> bool:
        """Render a block. Never raises, never deletes the message.

        Returns True when the widget now carries the block. A value that
        is not a :class:`streamview.RoleBlock` is refused outright and the
        widget is left as it was - clearing it would be the one way this
        could delete a message, so a bad caller must be a no-op rather
        than an erase.
        """
        if not isinstance(block, _sv.RoleBlock):
            return False
        try:
            self._apply_role(block.role)
            body = role_text(block, self.settings, **kwargs)
            if not body.plain:
                # A block that renders empty still keeps its own label on
                # screen: an empty render must not erase a message.
                fallback = str(getattr(block, "label", "") or "")
                body = Text(fallback, style=ui.TEXT_PRIMARY) if fallback else body
            self.update(body)
            return True
        except Exception:
            # Last resort: the characters, unstyled. A render failure is
            # never allowed to take the message with it.
            try:
                plain = "\n".join(block.plain_lines())
                self.update(Text(plain, style=ui.TEXT_PRIMARY))
            except Exception:
                pass
            return False


class UndoBlockWidget(Static):
    """The inline undo/redo receipt, where the change happened."""

    DEFAULT_CSS = """
    UndoBlockWidget {
        height: auto;
        padding: 0 2;
        border-left: outer $neo-warning;
        background: $neo-panel;
        display: none;
    }
    """

    def __init__(self, notice: Any = None, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._visible = False
        if notice is not None:
            self.update_notice(notice)

    def update_notice(self, notice: Any) -> bool:
        """Show a revert receipt inline. Never raises.

        A value that is not an :class:`streamview.UndoNotice` hides the
        receipt rather than leaving a stale one on screen: a revert
        block that describes a revert that did not happen is the same
        class of lie as a run card that reports the wrong status.
        """
        if not isinstance(notice, _sv.UndoNotice):
            self.clear_receipt()
            return False
        try:
            body = undo_text(notice, None)
            if not body.plain:
                self.clear_receipt()
                return False
            self.update(body)
            self.styles.display = "block"
            self._visible = True
            return True
        except Exception:
            return False

    def clear_receipt(self) -> None:
        """Hide the receipt. Never raises."""
        try:
            self._visible = False
            self.update(Text(""))
            self.styles.display = "none"
        except Exception:
            pass

    @property
    def visible(self) -> bool:
        return self._visible


def composer_widget_state(
    widget: Any,
    *,
    approval: Any = None,
    question: Any = None,
    steerable: bool = False,
) -> Tuple[bool, str]:
    """Disable/enable a composer widget in place. Returns ``(disabled, hint)``.

    The wrapper is the whole point: a caller that forgets to consult
    :func:`streamview.composer_state` still cannot leave the composer
    accepting input while a decision is pending, because this is the one
    function the mount calls.
    """
    state = _sv.composer_state(
        approval=approval, question=question, steerable=steerable
    )
    _placeholder, hint = composer_hints(state)
    if widget is None:
        return (state.disabled, hint)
    try:
        widget.disabled = state.disabled
    except Exception:
        pass
    try:
        widget.placeholder = state.hint() or "ask, or / for commands"
    except Exception:
        pass
    return (state.disabled, hint)


# ---------------------------------------------------------------------------
# VEX-PF-06 - onboarding surfaces (APPENDED; nothing above this marker is
# changed by that round)
#
# These are RENDERERS, not widgets. `EmptyState` above owns the first-launch
# hero and its layout is Prompt 01's; what this block adds is the sentence a
# panel prints when it has nothing to show, and the three commands a newcomer
# must be able to see on the first screen. `cli/tui.py` is Prompt 01's file
# and was not opened; the exact mount points are in `cli/AGENTS.md` under
# "Handoff to 01".
#
# Everything returns `rich.text.Text`, which has NO markup interpretation: a
# repository path, a connector label or a session issue containing `[` prints
# instead of deleting the message. A render failure must never delete a
# message, so a surface that wants markup uses `cli.onboarding.escape_lines`.
# ---------------------------------------------------------------------------

#: The widget id a shell that wants the affordance row on its first screen
#: should give it. Declared beside the payload it renders so the two cannot
#: drift, and it is NOT mounted by this round.
AFFORDANCE_WIDGET_ID = "neo-affordances"

#: The widget id for the empty-state sentence. One id for every state: the
#: state name is in the text, not in the mount tree, so a shell does not need
#: a widget per state and a new state costs no layout change.
EMPTY_STATE_WIDGET_ID = "neo-empty-state"


def empty_state_lines(state_id: str, *, width: int = 78, style: str = "") -> List[Text]:
    """The declared empty state for `state_id` as `rich.text.Text` lines.

    Never returns an empty list and never raises: a caller that cannot resolve
    a state gets the honest fallback sentence rather than a blank panel, which
    is the whole point of the function. The vocabulary and the sentences live
    in `cli.onboarding.EMPTY_STATES`; this is the Text-typed exit only.
    """
    try:
        from cli import onboarding

        plain = onboarding.empty_state_lines(state_id, width=int(width or 78))
    except Exception:
        plain = ["Nothing to show here.", "next: /help"]
    token = str(style or "") or ui.TEXT_SECONDARY
    return [Text(str(line), style=token) for line in plain]


def empty_state_report(state_id: str) -> dict:
    """The declared state as a plain dict, for a receipt or a `--json` panel.

    A shell that wants to decide its own layout needs the FIELDS (the sentence,
    the runnable action, the reason) rather than the rendered lines, and it
    needs them without importing `cli.onboarding`'s dataclass.
    """
    try:
        from cli import onboarding

        return dict(onboarding.empty_state(state_id).to_dict())
    except Exception:
        return {
            "id": "unknown",
            "what": "a surface has no declared empty state",
            "sentence": "Nothing to show here.",
            "action": "/help",
            "also": "",
            "why": "cli.onboarding is unavailable",
        }


def affordance_text_lines(*, width: int = 78, style: str = "") -> List[Text]:
    """The three newcomer affordances as `rich.text.Text` lines.

    undo, see-what-changed and stop-the-run, in the order a person needs them.
    The shortcut keys are read from `cli.commands.keyboard_shortcuts()`, so a
    renamed keybind cannot leave this row promising the old one, and the
    labels are short enough that the row is ONE line at 78 columns (measured:
    63 of 70 available, and it wraps to at most three rows on a narrow
    terminal rather than dropping an affordance).
    """
    try:
        from cli import onboarding

        plain = onboarding.affordance_lines(width=int(width or 78))
    except Exception:
        plain = ["/diff what changed · /undo put it back · /cancel stop the run"]
    token = str(style or "") or ui.TEXT_SECONDARY
    return [Text(str(line), style=token) for line in plain]


def affordance_receipt() -> dict:
    """What the affordance row is, for a panel that wants to build its own.

    `id` is the widget id to mount, `rows` is the already-typed text, and
    `commands` is the three runnable doors — which is what a shell needs in
    order to route a click or a key to the right handler without re-deriving
    which three controls these are.
    """
    rows = affordance_text_lines()
    commands = []
    try:
        from cli import onboarding

        commands = [aff.command for aff in onboarding.AFFORDANCES]
    except Exception:
        commands = ["/diff", "/undo", "/cancel"]
    return {
        "widget_id": AFFORDANCE_WIDGET_ID,
        "rows": rows,
        "commands": commands,
        "plain": [row.plain for row in rows],
    }


def onboarding_mount_points() -> dict:
    """What Prompt 01 has to mount, named. See `cli/AGENTS.md`.

    Kept in code rather than in a chat message because a mount point that
    lives in a handoff is a mount point that never gets wired, and this one
    is three lines.
    """
    return {
        AFFORDANCE_WIDGET_ID: "append affordance_text_lines() to the "
        "startup state, after the one actionable sentence",
        EMPTY_STATE_WIDGET_ID: "append empty_state_lines(<state>) wherever a "
        "panel currently renders nothing",
    }


# ---------------------------------------------------------------------------
# The aesthetic gate: reading what the shell ACTUALLY drew
# ---------------------------------------------------------------------------
#
# The gate in `tests/test_aesthetic_gate.py` is allowed to measure the
# rendered receipt, but it must not have to guess which WIDGET a row came
# from -- a screenshot has pixels, not a mount tree. These three functions
# are that bridge, and they live here because this module is the shell's
# component boundary: the surface map, the plain-text reader, and the
# measured layout are all facts about the components.
# ---------------------------------------------------------------------------

#: The mounted widget each LIVE surface is published through, in the order a
#: reader scans the terminal. The names are `cli.design.LIVE_SURFACES`, so a
#: surface added there without a widget here fails the gate rather than
#: silently going unmeasured.
LIVE_SURFACE_WIDGET_IDS: Dict[str, str] = {
    "header": "neo-header",
    "statusline": "neo-statusline",
    "sidebar": "neo-side",
    "context": "neo-context",
    "runline": "neo-runline",
}

#: Every surface's widget, including the ones that are not live state. A
#: duplicate-information check has to be able to say which surfaces it
#: deliberately ignored, and it can only do that if it can name them.
ALL_SURFACE_WIDGET_IDS: Dict[str, str] = {
    **LIVE_SURFACE_WIDGET_IDS,
    "transcript": "neo-body",
    "composer": "neo-inputwrap",
    "footer": "neo-hints",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain_text(value: Any) -> str:
    """Strip ANSI escapes from a rendered string, and nothing else.

    Deliberately NOT `cli.ui.strip_ansi`: that function redacts, and a
    redactor rewrites the very facts a duplicate-information check is
    comparing. This round already found a round that called `strip_ansi` to
    build the text it searched for, so the NOISE it produced reported the
    opposite of the truth. A reader for a layout audit strips escapes and
    leaves every character a user would see.
    """
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    return _ANSI_RE.sub("", text).replace("\u00a0", " ")


def _widget_text(widget: Any) -> str:
    """Whatever rendered text a widget can produce, or `""`.

    A `Static` exposes `.visual`; a `RichLog` and a `Container` do not, and
    asking a container for its own visual raises. So the reader tries the
    widget's own rendered form and then its CHILDREN, which is the only way
    to read what a rail published: `#neo-side` is a `Vertical` and every
    fact it shows lives three levels down.
    """
    for attribute in ("visual", "renderable"):
        try:
            value = getattr(widget, attribute, None)
        except Exception:
            value = None
        if value is None:
            continue
        try:
            text = plain_text(value)
        except Exception:
            continue
        if text.strip():
            return text
    collected: List[str] = []
    try:
        children = list(widget.children)
    except Exception:
        children = []
    for child in children:
        try:
            if (
                str(getattr(getattr(child, "styles", None), "display", "block"))
                == "none"
            ):
                continue
        except Exception:
            pass
        piece = _widget_text(child)
        if piece.strip():
            collected.append(piece)
    return "\n".join(collected)


def surface_text(app: Any, widget_id: str) -> str:
    """The plain text one mounted widget is currently showing.

    `str(widget.visual)` is the RENDERED content, not the markup, so a
    `[neo.accent]` tag is already resolved by the time this reads it. A
    widget that is not mounted, not rendered, or raises contributes the empty
    string, which is the honest answer: a hidden surface publishes no facts.
    """
    if app is None:
        return ""
    try:
        widget = app.query_one(f"#{widget_id}")
    except Exception:
        return ""
    try:
        styles = widget.styles
        if str(getattr(styles, "display", "block")) == "none":
            return ""
    except Exception:
        pass
    return _widget_text(widget)


def live_surfaces(app: Any) -> Dict[str, str]:
    """Every live surface's plain text, keyed by the declared surface name.

    The input to `cli.design.duplicate_report`. Reading the WIDGET rather
    than cropping the screenshot is deliberate: a duplicate is a claim about
    two named regions, and a receipt cannot name its own regions.
    """
    return {
        name: surface_text(app, widget_id)
        for name, widget_id in LIVE_SURFACE_WIDGET_IDS.items()
    }


def measured_layout(app: Any, width: int = 0, height: int = 0) -> _design.LayoutSpec:
    """The authority's layout with every region rectangle replaced by the
    rectangle the COMPOSITOR actually gave the widget.

    This is the distinction the whole aesthetic gate rests on. A layout the
    authority resolves is a claim; a layout read off the mounted tree is a
    measurement, and the two are only equal if the authority and the product
    agree. Before this existed, `cli/design.py` placed the plan rail at
    column 0 while `NeoApp.compose` mounted it after the transcript, and
    nothing compared the two -- so the one layout authority misdescribed the
    one product it governs, in the most basic way available.

    A region that is not mounted keeps the authority's rectangle with a zero
    size, which `Region.visible` reports as hidden rather than as a guess.
    """
    import dataclasses

    size_width = int(width or getattr(getattr(app, "size", None), "width", 0) or 0)
    size_height = int(height or getattr(getattr(app, "size", None), "height", 0) or 0)
    if size_width <= 0 or size_height <= 0:
        declared = _design.resolve_layout(1, 1)
        return declared
    authority = _design.resolve_layout(
        size_width, size_height, sidebar=_design.DEFAULT_SIDEBAR_MODE
    )
    regions = []
    for region in authority.regions:
        widget_id = _design.REGION_IDS.get(region.name, "")
        measured = None
        if widget_id:
            try:
                measured = app.query_one(f"#{widget_id}").region
            except Exception:
                measured = None
        if measured is None:
            regions.append(_design.Region(region.name, region.widget_id, 0, 0, 0, 0))
            continue
        regions.append(
            _design.Region(
                region.name,
                region.widget_id,
                int(measured.x),
                int(measured.y),
                int(measured.width),
                int(measured.height),
            )
        )
    return dataclasses.replace(authority, regions=tuple(regions))


def agreement_report(app: Any, width: int = 0, height: int = 0) -> Dict[str, Any]:
    """Where the layout authority and the mounted shell disagree.

    Empty is the pass condition. Every entry names the region, the column
    each side put it at, and the width they are at -- because "the design
    system is inconsistent" is not a finding anybody can act on and
    "the plan rail is declared at 0 and rendered at 58" is.

    A region that is COLLAPSED on either side is reported under `collapsed`
    rather than as a disagreement: a hidden widget's compositor rectangle is
    0x0 at the origin, which is not a claim about where the region belongs,
    and comparing it against a declared 42-wide rectangle would report the
    anti-clutter rule working as a layout error.
    """
    size_width = int(width or getattr(getattr(app, "size", None), "width", 0) or 0)
    size_height = int(height or getattr(getattr(app, "size", None), "height", 0) or 0)
    if size_width <= 0 or size_height <= 0:
        return {"width": size_width, "height": size_height, "findings": [], "ok": False}
    declared = _design.resolve_layout(
        size_width, size_height, sidebar=_design.DEFAULT_SIDEBAR_MODE
    )
    measured = measured_layout(app, size_width, size_height)
    findings: List[Dict[str, Any]] = []
    collapsed: List[str] = []
    for name in _design.REGIONS:
        try:
            want = declared.region(name)
            got = measured.region(name)
        except Exception:
            continue
        if not want.visible or got.width <= 0 or got.height <= 0:
            collapsed.append(name)
            continue
        if want.x != got.x or want.y != got.y:
            findings.append(
                {
                    "region": name,
                    "declared": {"x": want.x, "y": want.y, "width": want.width},
                    "measured": {"x": got.x, "y": got.y, "width": got.width},
                }
            )
    return {
        "width": size_width,
        "height": size_height,
        "placement": declared.rail_placement,
        "collapsed": collapsed,
        "findings": findings,
        "ok": not findings,
    }
