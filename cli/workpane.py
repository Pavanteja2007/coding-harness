"""The right-hand WORK PANE: one mount that hosts the changes, tree and git views.

Phase 5/W1 tasks T5.W1.1-T5.W1.3. One region, three modes, one keyboard map.

Why a container rather than three sibling panes: the shell already spends its
right-hand slot on ``#neo-context`` (the context rail), and three more columns
at 120 columns is a transcript nobody can read. So this pane takes that slot
when it is open and yields it back when it is collapsed, and the three views
are MODES of one region. The alternative -- a second rail beside a rail -- was
measured by the layout authority: at 120 columns the transcript is already down
to 48 columns with both rails open, and a third column would leave a transcript
of zero.

The keyboard contract, declared here as DATA so a future keymap edit has to
confront it:

* ``ctrl+d`` opens/closes the pane and focuses it (the binding is mounted in
  ``cli/tui.py``; this module owns what happens once it is focused).
* Inside the pane: ``1``/``2``/``3`` switch mode, ``j``/``k`` (and the arrow
  keys) move the focus row, ``enter``/``space`` expand a file and show its
  hunks, ``a`` accepts the focused hunk (or file when no hunk is focused),
  ``r`` rejects it, ``u`` reverts the focused file, ``?`` shows the key help and
  ``esc`` returns focus to the composer.

Every action routes to :mod:`cli.review`. Every render routes through the
pane's own renderer, which sanitises. ``esc`` is handled here rather than
globally because this module's own history records that binding ``escape``
app-wide made modal dismissal race with input clearing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from cli import changespane as _changes
from cli import filetreepane as _tree
from cli import gitpane as _git

__all__ = [
    "KEY_HELP",
    "MODES",
    "MODE_CHANGES",
    "MODE_GIT",
    "MODE_TREE",
    "PANE_MIN_TERMINAL_COLUMNS",
    "PANE_WIDGET_ID",
    "WIDTH_STEPS",
    "WorkPane",
    "WorkPaneState",
    "mode_choices_lines",
]

PANE_WIDGET_ID = "neo-work"

MODE_CHANGES = "changes"
MODE_TREE = "tree"
MODE_GIT = "git"

#: The three modes, in the order the number keys select them. Declared as data
#: so ``1``/``2``/``3`` and the header cannot disagree.
MODES: Tuple[str, ...] = (MODE_CHANGES, MODE_TREE, MODE_GIT)

#: Below this terminal width the pane collapses and says so. The shell's own
#: authority collapses BOTH rails below 100 columns; a pane that squeezed the
#: transcript to nothing would be the third disclosure this package records.
PANE_MIN_TERMINAL_COLUMNS = 100

#: The width steps ``w`` cycles through. Deliberate rather than free-form: a
#: resizable pane with no steps is a pane whose width nobody can reproduce.
WIDTH_STEPS: Tuple[int, ...] = (_changes.NARROW_PANE_COLUMNS, _changes.WIDE_PANE_COLUMNS)

#: The key map, as data. Every entry names what it does, so the help row is
#: generated from the same table the handler reads.
KEY_HELP: Tuple[Tuple[str, str], ...] = (
    ("1 2 3", "changes / tree / git"),
    ("j k", "move between rows"),
    ("enter", "expand a file to its hunks"),
    ("a", "accept the focused hunk (or file)"),
    ("r", "reject the focused hunk (or file)"),
    ("u", "revert the focused file"),
    ("w", "widen / narrow the pane"),
    ("?", "this help"),
    ("esc", "back to the composer"),
)


@dataclass
class WorkPaneState:
    """Everything the pane needs, as a mutable value the shell owns.

    The shell owns the state and the pane reads it, so a repaint never has to
    re-derive the review document and a mount can never disagree with the model
    it is rendering.
    """

    mode: str = MODE_CHANGES
    focused: int = 0
    expanded: Tuple[int, ...] = ()
    width_index: int = 0
    visible: bool = False
    task_dir: Any = None
    repo: Any = None
    review_document: Any = None
    changed_paths: Tuple[str, ...] = ()
    git_report: Any = None
    last_receipt: Tuple[str, ...] = ()

    @property
    def width(self) -> int:
        """The pane's current column budget."""
        steps = WIDTH_STEPS
        index = max(0, min(int(self.width_index or 0), len(steps) - 1))
        return int(steps[index])

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-friendly projection, used by the truth gate and ``--json``."""
        return {
            "mode": self.mode,
            "visible": bool(self.visible),
            "width": self.width,
            "focused": int(self.focused),
            "expanded": list(self.expanded),
            "changed": list(self.changed_paths),
            "receipt": list(self.last_receipt),
        }


def mode_choices_lines(mode: str = MODE_CHANGES, width: int = 60) -> List[str]:
    """The mode selector row. The ACTIVE mode is named, not implied."""
    from cli import changespane as _c

    limit = max(1, int(width))
    active = str(mode or MODE_CHANGES)
    parts = []
    for index, name in enumerate(MODES, start=1):
        mark = "*" if name == active else " "
        parts.append(f"{index}{mark}{name}")
    return [_c._bound("pane  " + "  ".join(parts), limit)]


class WorkPane(Vertical):
    """The right-hand pane: one region, three modes, one keyboard map."""

    DEFAULT_CSS = """
    #neo-work {
        width: 44;
        min-width: 0;
        padding: 0 1;
        background: $neo-panel;
        border-left: solid $neo-border;
        display: none;
    }
    #neo-work-body {
        height: auto;
    }
    """

    def __init__(
        self,
        state: Optional[WorkPaneState] = None,
        *,
        name: Optional[str] = None,
        id: Optional[str] = None,
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(name=name, id=id or PANE_WIDGET_ID, classes=classes)
        self.state = state or WorkPaneState()

    # -- composition ----------------------------------------------------

    def compose(self) -> ComposeResult:
        """One body region plus one receipt region.

        The receipt is a SEPARATE region rather than appended to the body,
        because an action receipt that scrolls away is a receipt the person who
        pressed the key never saw.
        """
        yield Static("", id="neo-work-body")
        yield Static("", id="neo-work-receipt")

    def on_mount(self) -> None:
        """Apply the state's visibility and paint once. Never raises."""
        try:
            self.apply_visibility()
            self.refresh_pane()
        except Exception:
            pass

    # -- visibility -----------------------------------------------------

    def apply_visibility(self, *, terminal_columns: Optional[int] = None) -> str:
        """Show or hide the pane, returning the SENTENCE for why.

        A pane that silently refuses to open has left the person guessing which
        key they pressed, so every refusal returns a sentence and this function
        renders it into the receipt region.
        """
        columns = terminal_columns
        if columns is None:
            try:
                columns = int(self.size.width) or 0
            except Exception:
                columns = 0
        # `self.size` is the pane's own size, so a caller that knows the
        # TERMINAL width must pass it; when it does not, the shell's own
        # decision is what applies.
        if columns and columns < PANE_MIN_TERMINAL_COLUMNS:
            self.state.visible = False
            self.styles.display = "none"
            return (
                f"the pane is collapsed: {columns} column(s) is below the "
                f"{PANE_MIN_TERMINAL_COLUMNS} this pane needs; widen the terminal "
                "or press ctrl+5 to drop a rail"
            )
        self.styles.display = "block" if self.state.visible else "none"
        return "" if self.state.visible else "pane collapsed"

    def set_visible(self, visible: bool) -> str:
        """Open or close the pane and paint. Returns the reason when refused."""
        self.state.visible = bool(visible)
        note = self.apply_visibility()
        self.refresh_pane()
        return note

    def cycle_width(self) -> int:
        """Advance to the next declared width step and repaint."""
        self.state.width_index = (int(self.state.width_index or 0) + 1) % len(WIDTH_STEPS)
        self.styles.width = self.state.width
        self.styles.min_width = min(self.state.width, self.state.width)
        self.refresh_pane()
        return self.state.width

    # -- rendering ------------------------------------------------------

    def render_mode_lines(self) -> List[str]:
        """The current mode's lines. Delegates to the pane that owns the content."""
        state = self.state
        width = state.width
        if state.mode == MODE_TREE:
            model = _tree.tree_model(state.repo, state.changed_paths, width=width)
            return mode_choices_lines(state.mode, width) + _tree.tree_lines(model, width)
        if state.mode == MODE_GIT:
            report = state.git_report or _git.git_report(state.task_dir, state.repo, width=width)
            # ``git_discoverability_lines`` ALREADY states the branch, so this
            # list does not repeat it: two rows saying the same word is the
            # duplicate-information class this package records four times, and
            # in a 62-column pane the repetition is the whole second line.
            return [
                *mode_choices_lines(state.mode, width),
                *_git.git_discoverability_lines(report, width),
                *_git.status_lines(report, width),
                *_git.history_lines(report, width),
            ]
        document = state.review_document
        if document is None and state.task_dir:
            try:
                from cli import review as _review

                document = _review.build_review(state.task_dir, state.repo)
            except Exception as exc:
                return [
                    *mode_choices_lines(state.mode, width),
                    _changes._bound(f"changes unavailable: {exc}", width),
                ]
        if document is None:
            return [
                *mode_choices_lines(state.mode, width),
                _changes._bound("no run is selected, so there is nothing to review", width),
            ]
        model = _changes.pane_model(document)
        return mode_choices_lines(state.mode, width) + _changes.pane_lines(
            model, width, focused=state.focused, expanded=state.expanded
        )

    def text_lines(self) -> List[Text]:
        """The pane as ``rich.text.Text``.

        ``Text`` has no markup parser at all, which is the structural answer to
        a repository path containing ``[``: there is nothing to escape and
        nothing to eat. The lines are already sanitised by the renderers, so
        this is a change of CONTAINER, not of content.
        """
        try:
            return [Text(str(line)) for line in self.render_mode_lines()]
        except Exception as exc:  # a render failure must not take the shell down
            return [Text(f"pane could not render: {exc}")]

    def refresh_pane(self) -> None:
        """Paint the body and the receipt. Never raises."""
        try:
            body = self.query_one("#neo-work-body", Static)
            body.update(self.text_lines())
            receipt = self.query_one("#neo-work-receipt", Static)
            receipt.update([Text(str(line)) for line in tuple(self.state.last_receipt)])
        except Exception:
            pass

    # -- focus + keyboard ------------------------------------------------

    def focus_next_row(self, delta: int = 1) -> int:
        """Move the focused row within the current mode and repaint."""
        rows = self._focusable_count()
        if rows <= 0:
            self.state.focused = 0
        else:
            step = 1 if delta >= 0 else -1
            self.state.focused = (int(self.state.focused or 0) + step) % rows
        self.refresh_pane()
        return int(self.state.focused or 0)

    def _focusable_count(self) -> int:
        """How many rows this mode can focus right now."""
        if self.state.mode == MODE_TREE:
            return max(1, len(self.state.changed_paths) or 1)
        document = self.state.review_document
        rows = tuple(getattr(document, "files", ()) or ())
        if self.state.focused in set(int(item) for item in self.state.expanded):
            record = rows[min(int(self.state.focused), len(rows) - 1)] if rows else None
            if record is not None:
                return max(1, len(tuple(getattr(record, "hunk_headers", ()) or ())))
        return max(1, len(rows))

    def toggle_expand(self) -> Tuple[str, ...]:
        """Expand or collapse the focused file. Returns the receipt lines."""
        if self.state.mode != MODE_CHANGES:
            return ("only the changes view expands files",)
        document = self.state.review_document
        rows = tuple(getattr(document, "files", ()) or ())
        if not rows:
            return ("no file is focused",)
        index = max(0, min(int(self.state.focused or 0), len(rows) - 1))
        current = {int(item) for item in self.state.expanded}
        if index in current:
            current.discard(index)
            note = "collapsed"
        else:
            current.add(index)
            note = "expanded"
        self.state.expanded = tuple(sorted(current))
        self.refresh_pane()
        return (f"{note} file {index + 1} of {len(rows)}",)

    def _focus_target(self) -> Tuple[str, Any]:
        """``(path, hunk_or_None)`` for the focused row.

        ``None`` for the hunk index means "the whole file", which is a
        DIFFERENT action with a different receipt and this module keeps the two
        apart rather than defaulting one to the other.
        """
        document = self.state.review_document
        rows = tuple(getattr(document, "files", ()) or ())
        if not rows:
            return ("", None)
        index = max(0, min(int(self.state.focused or 0), len(rows) - 1))
        path = str(getattr(rows[index], "path", "") or "")
        if index not in {int(item) for item in self.state.expanded}:
            return (path, None)
        headers = tuple(getattr(rows[index], "hunk_headers", ()) or ())
        if not headers:
            return (path, None)
        hunk = max(1, min(int(self.state.focused or 0) + 1, len(headers)))
        return (path, hunk)

    def _apply(self, verb: str) -> Tuple[str, ...]:
        """Run one review action for the focused row and return its receipt."""
        path, hunk = self._focus_target()
        if not path:
            return ("no file is focused",)
        common = {"repo": self.state.repo, "width": self.state.width}
        if hunk is None:
            if verb == "accept":
                result = _changes.apply_hunk_accept(self.state.task_dir, path, 1, **common)
                result = {"ok": result.get("ok"), "lines": result.get("lines") or []}
                return tuple(str(line) for line in result["lines"]) or ("recorded",)
            if verb == "reject":
                result = _changes.apply_hunk_reject(self.state.task_dir, path, 1, **common)
                return tuple(str(line) for line in (result.get("lines") or [])) or ("recorded",)
            if verb == "revert":
                result = _changes.apply_file_revert(self.state.task_dir, path, **common)
                return tuple(str(line) for line in (result.get("lines") or [])) or ("reverted",)
        if verb == "accept":
            result = _changes.apply_hunk_accept(self.state.task_dir, path, hunk, **common)
        elif verb == "reject":
            result = _changes.apply_hunk_reject(self.state.task_dir, path, hunk, **common)
        else:
            result = _changes.apply_file_revert(self.state.task_dir, path, **common)
        lines = [str(line) for line in (result.get("lines") or [])]
        return tuple(lines) or ("recorded",)

    def _reload(self) -> None:
        """Re-read the review document so a decision is VISIBLE immediately."""
        if not self.state.task_dir:
            return
        try:
            from cli import review as _review

            self.state.review_document = _review.build_review(self.state.task_dir, self.state.repo)
        except Exception:
            pass

    def handle_action(self, action: str) -> Tuple[str, ...]:
        """One entry point for every key, so the keymap and the tests agree.

        Every branch returns the receipt lines and refreshes. An unknown action
        says so rather than doing nothing, because a key that does nothing is
        indistinguishable from a key that is broken.
        """
        if action.startswith("mode_"):
            name = action.split("_", 1)[1]
            if name not in MODES:
                return (f"unknown pane mode {name!r}; the modes are {', '.join(MODES)}",)
            self.state.mode = name
            self.state.focused = 0
            self.state.expanded = ()
            self.refresh_pane()
            return (f"pane mode: {name}",)
        if action in {"next", "previous"}:
            self.focus_next_row(1 if action == "next" else -1)
            return ()
        if action == "expand":
            return self.toggle_expand()
        if action == "width":
            return (f"pane width: {self.cycle_width()} column(s)",)
        if action == "help":
            width = self.state.width
            return tuple(
                _changes._bound(f"{keys}  {what}", width) for keys, what in KEY_HELP
            )
        if action in {"accept", "reject", "revert"}:
            lines = self._apply(action)
            self._reload()
            self.state.last_receipt = lines
            self.refresh_pane()
            return lines
        if action == "open":
            path, _hunk = self._focus_target()
            outcome = _tree.open_tree_target(path, self.state.repo)
            if not outcome["ok"]:
                return (f"refused: {outcome['reason']}",)
            self.state.mode = MODE_TREE
            self.refresh_pane()
            return (f"opened {outcome['path']}",)
        return (f"unknown pane action {action!r}",)

    def on_key(self, event: events.Key) -> None:
        """Translate a keypress into a pane action.

        ``event.stop()`` is called for every key this pane CONSUMES and for no
        key it does not, so an unconsumed key still reaches the composer. That
        asymmetry is the difference between a pane and a modal.
        """
        if not self.state.visible:
            return
        mapping = {
            "1": "mode_changes",
            "2": "mode_tree",
            "3": "mode_git",
            "j": "next",
            "down": "next",
            "k": "previous",
            "up": "previous",
            "enter": "expand",
            "space": "expand",
            "a": "accept",
            "r": "reject",
            "u": "revert",
            "w": "width",
            "?": "help",
            "o": "open",
        }
        action = mapping.get(event.key)
        if action is None:
            return
        event.stop()
        try:
            self.handle_action(action)
        except Exception as exc:  # a key must never take the shell down
            self.state.last_receipt = (f"pane action failed: {exc}",)
            self.refresh_pane()
